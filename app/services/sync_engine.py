"""Sync Engine -- applies a verified, deduped webhook event to the read model.

Dispatches by event_type into room_type_index / rate_availability_index /
sync_health (contract section 4.7), then mirrors the change into OpenSearch.
Only this module and the reconciliation poller ever write these cache tables
(NFR-B3: availability is never authored by a traveler-facing path).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    Hotel,
    RateAvailabilityIndex,
    RoomTypeIndex,
    SyncHealth,
    SyncHealthStatus,
    SyncSource,
)
from app.schemas.webhooks import (
    AvailabilityChangedEvent,
    RateChangedEvent,
    ReservationStatusEvent,
    RoomTypeDeletedEvent,
    RoomTypeUpsertEvent,
    SyncHeartbeatEvent,
)
from app.search.indexer import index_availability, update_room_type_docs

logger = logging.getLogger("aggregator.sync_engine")


async def _touch_sync_health(session: AsyncSession, hotel_id, *, reset_failures: bool = False) -> None:
    now = datetime.now(timezone.utc)
    values = {"hotel_id": hotel_id, "last_event_received_at": now}
    set_ = {"last_event_received_at": now}
    if reset_failures:
        values.update(consecutive_failures=0, status=SyncHealthStatus.healthy)
        set_.update(consecutive_failures=0, status=SyncHealthStatus.healthy)
    stmt = pg_insert(SyncHealth).values(**values).on_conflict_do_update(
        index_elements=[SyncHealth.hotel_id], set_=set_
    )
    await session.execute(stmt)


async def _upsert_room_type(session: AsyncSession, hotel: Hotel, event: RoomTypeUpsertEvent) -> None:
    d = event.data
    values = {
        "global_room_type_id": d.room_type_id,
        "hotel_id": hotel.hotel_id,
        "name": d.name,
        "description": d.description,
        "max_occupancy_adults": d.max_occupancy_adults,
        "max_occupancy_children": d.max_occupancy_children,
        "amenities": d.amenities,
        "photos": d.photos,
        "active": d.active,
        "updated_at": d.updated_at,
    }
    update_cols = {k: v for k, v in values.items() if k not in ("global_room_type_id", "hotel_id")}
    stmt = pg_insert(RoomTypeIndex).values(**values).on_conflict_do_update(
        index_elements=[RoomTypeIndex.global_room_type_id], set_=update_cols
    )
    await session.execute(stmt)
    await session.commit()

    await update_room_type_docs(
        d.room_type_id,
        active=d.active,
        room_type_fields={
            "room_type_name": d.name,
            "description": d.description,
            "max_occupancy_adults": d.max_occupancy_adults,
            "max_occupancy_children": d.max_occupancy_children,
            "amenities": d.amenities or [],
            "photos": d.photos or [],
        },
    )


async def _delete_room_type(session: AsyncSession, event: RoomTypeDeletedEvent) -> None:
    rt = await session.get(RoomTypeIndex, event.data.room_type_id)
    if rt is not None:
        rt.active = False
        await session.commit()
        await update_room_type_docs(event.data.room_type_id, active=False, room_type_fields={})


async def _upsert_availability(session: AsyncSession, event: AvailabilityChangedEvent | RateChangedEvent) -> None:
    d = event.data
    # The FK to room_type_index requires the room type to exist first. If an
    # availability event arrives before its room_type.created (possible under
    # at-least-once, out-of-order delivery), skip persisting -- reconciliation
    # will backfill it. Never invent a room type from an availability payload.
    exists = await session.scalar(
        select(RoomTypeIndex.global_room_type_id).where(RoomTypeIndex.global_room_type_id == d.room_type_id)
    )
    if exists is None:
        logger.info("Availability for unknown room type %s -- skipping until backfill", d.room_type_id)
        return

    now = datetime.now(timezone.utc)
    values = {
        "global_room_type_id": d.room_type_id,
        "date": d.date,
        "rate_plan_code": d.rate_plan_code,
        "price_minor": d.price_minor,
        "currency": d.currency,
        "rooms_available_cached": d.rooms_available,
        "updated_at": now,
        "source": SyncSource.webhook,
    }
    update_cols = {
        "price_minor": d.price_minor,
        "currency": d.currency,
        "rooms_available_cached": d.rooms_available,
        "updated_at": now,
        "source": SyncSource.webhook,
    }
    stmt = pg_insert(RateAvailabilityIndex).values(**values).on_conflict_do_update(
        index_elements=[
            RateAvailabilityIndex.global_room_type_id,
            RateAvailabilityIndex.date,
            RateAvailabilityIndex.rate_plan_code,
        ],
        set_=update_cols,
    )
    await session.execute(stmt)
    await session.commit()

    await index_availability(
        session,
        global_room_type_id=d.room_type_id,
        date_str=d.date.isoformat(),
        rate_plan_code=d.rate_plan_code,
        price_minor=d.price_minor,
        currency=d.currency,
        rooms_available=d.rooms_available,
        updated_at=now.isoformat(),
    )


async def dispatch_event(session: AsyncSession, hotel: Hotel, event) -> None:
    """Apply one validated event. `hotel` is the resolved Hotel row (from the
    event's hotel_id slug). Caller has already deduped via webhook_inbox.
    """
    if isinstance(event, RoomTypeUpsertEvent):
        await _upsert_room_type(session, hotel, event)
    elif isinstance(event, RoomTypeDeletedEvent):
        await _delete_room_type(session, event)
    elif isinstance(event, (AvailabilityChangedEvent, RateChangedEvent)):
        await _upsert_availability(session, event)
    elif isinstance(event, ReservationStatusEvent):
        # IDs only, no persistent booking-correctness state (contract 4.7 note):
        # these exist for traveler notifications / analytics.
        pass
    elif isinstance(event, SyncHeartbeatEvent):
        await _touch_sync_health(session, hotel.hotel_id, reset_failures=True)
        await session.commit()
        return
    else:  # pragma: no cover - discriminated union is exhaustive
        logger.warning("Unhandled event type: %r", getattr(event, "event_type", "?"))

    await _touch_sync_health(session, hotel.hotel_id)
    await session.commit()
