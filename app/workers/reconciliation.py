"""Reconciliation poller (contract section 3.3 / NFR-B8, decision 5.10).

Drift-corrects the read model against each hotel's authoritative API:
  * `reconcile_room_types`  -- GET /room-types?updated_since=<last_reconciliation_at>
    (cursor-paginated) to backfill / correct room_type_index.
  * `full_availability_resync` -- nightly full pull of a forward horizon of
    availability to correct rate_availability_index (source='reconciliation').

Every hotel is treated as untrusted/unreliable (NFR-B2): a per-call timeout is
applied, and a per-hotel circuit breaker (Redis-backed cooldown) skips a hotel
after N consecutive failures and marks sync_health.status='down'.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app import cache
from app.clients.hotel_erp_client import HotelERPClient, HotelERPError
from app.config import settings
from app.db.models import (
    Hotel,
    HotelCredential,
    HotelStatus,
    RateAvailabilityIndex,
    RoomTypeIndex,
    SyncHealth,
    SyncHealthStatus,
    SyncSource,
)
from app.db.session import AsyncSessionLocal
from app.search.indexer import index_availability

logger = logging.getLogger("aggregator.reconciliation")

AVAILABILITY_HORIZON_NIGHTS = 30
_CIRCUIT_KEY = "circuit:{hotel_id}"


def _parse_dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


async def _circuit_open(hotel_id) -> bool:
    return bool(await cache.get_redis().get(_CIRCUIT_KEY.format(hotel_id=hotel_id)))


async def _record_failure(session: AsyncSession, hotel_id) -> None:
    # Takes hotel_id, not a SyncHealth instance -- every caller invokes this
    # right after `await session.rollback()`, which expires every object the
    # session was tracking (including whatever SyncHealth it already had in
    # hand). Mutating that expired instance directly triggers an implicit
    # lazy-load, which asyncpg's async session can't service outside an
    # explicit await (raises sqlalchemy.exc.MissingGreenlet) -- confirmed
    # live: a real HotelERPError previously crashed this exact handler
    # instead of recording the failure, defeating the point of the
    # untrusted-ERP handling this function exists for (NFR-B2). Re-fetching
    # fresh here removes the whole class of "caller passed a stale ORM
    # object post-rollback" bug, not just this one call site.
    sh = await session.get(SyncHealth, hotel_id)
    if sh is None:
        return
    sh.consecutive_failures += 1
    if sh.consecutive_failures >= settings.circuit_breaker_failure_threshold:
        sh.status = SyncHealthStatus.down
        await cache.get_redis().set(
            _CIRCUIT_KEY.format(hotel_id=sh.hotel_id),
            "1",
            ex=settings.circuit_breaker_cooldown_seconds,
        )
    else:
        sh.status = SyncHealthStatus.degraded
    await session.commit()


async def _record_success(session: AsyncSession, sh: SyncHealth) -> None:
    sh.consecutive_failures = 0
    sh.status = SyncHealthStatus.healthy
    sh.last_reconciliation_at = datetime.now(timezone.utc)
    await session.commit()
    await cache.get_redis().delete(_CIRCUIT_KEY.format(hotel_id=sh.hotel_id))


async def _get_sync_health(session: AsyncSession, hotel_id) -> SyncHealth:
    sh = await session.get(SyncHealth, hotel_id)
    if sh is None:
        sh = SyncHealth(hotel_id=hotel_id)
        session.add(sh)
        await session.commit()
    return sh


async def _upsert_room_type_from_api(session: AsyncSession, hotel: Hotel, rt: dict) -> None:
    values = {
        "global_room_type_id": rt["room_type_id"],
        "hotel_id": hotel.hotel_id,
        "name": rt["name"],
        "description": rt.get("description"),
        "max_occupancy_adults": rt["max_occupancy_adults"],
        "max_occupancy_children": rt.get("max_occupancy_children", 0),
        "amenities": rt.get("amenities", []),
        "photos": rt.get("photos", []),
        "active": rt.get("active", True),
        "updated_at": _parse_dt(rt["updated_at"]),
    }
    update_cols = {k: v for k, v in values.items() if k not in ("global_room_type_id", "hotel_id")}
    stmt = pg_insert(RoomTypeIndex).values(**values).on_conflict_do_update(
        index_elements=[RoomTypeIndex.global_room_type_id], set_=update_cols
    )
    await session.execute(stmt)


async def _reconcile_one_hotel_room_types(session: AsyncSession, hotel: Hotel, cred: HotelCredential) -> None:
    # Captured as plain values, not read off `hotel`/`sh` again after a
    # rollback below -- see _record_failure's docstring-comment for why.
    hotel_id, hotel_slug = hotel.hotel_id, hotel.slug
    sh = await _get_sync_health(session, hotel_id)
    if await _circuit_open(hotel_id):
        logger.info("Circuit open for %s, skipping room-type reconcile", hotel_slug)
        return
    client = HotelERPClient.from_credential(cred)
    updated_since = sh.last_reconciliation_at.isoformat() if sh.last_reconciliation_at else None
    cursor: str | None = None
    try:
        while True:
            page = await client.list_room_types(updated_since=updated_since, cursor=cursor, limit=100)
            for rt in page.get("data", []):
                await _upsert_room_type_from_api(session, hotel, rt)
            await session.commit()
            cursor = page.get("next_cursor")
            if not cursor:
                break
        await _record_success(session, sh)
        logger.info("Reconciled room types for %s", hotel_slug)
    except HotelERPError as exc:
        logger.warning("Room-type reconcile failed for %s: %s", hotel_slug, exc)
        await session.rollback()
        await _record_failure(session, hotel_id)


async def _resync_one_hotel_availability(session: AsyncSession, hotel: Hotel, cred: HotelCredential) -> None:
    # Captured as plain values, not read off `hotel`/`sh` again after a
    # rollback below -- see _record_failure's docstring-comment for why.
    hotel_id, hotel_slug = hotel.hotel_id, hotel.slug
    if await _circuit_open(hotel_id):
        return
    sh = await _get_sync_health(session, hotel_id)
    client = HotelERPClient.from_credential(cred)
    today = date.today()
    check_out = today + timedelta(days=AVAILABILITY_HORIZON_NIGHTS)

    room_types = list(
        await session.scalars(select(RoomTypeIndex).where(RoomTypeIndex.hotel_id == hotel_id))
    )
    now = datetime.now(timezone.utc)
    try:
        for rt in room_types:
            quote = await client.get_availability(
                room_type_id=rt.global_room_type_id,
                check_in=today.isoformat(),
                check_out=check_out.isoformat(),
            )
            for line in quote.get("quotes", []):
                rate_plan_code = line["rate_plan_code"]
                # .get(), not [...]: an older hotel_erp deployment predating
                # AvailabilityQuoteLine.refundable (contract section 4.4)
                # won't send this field yet -- None is "unknown," handled the
                # same way sync_engine.py's webhook path handles it.
                refundable = line.get("refundable")
                for night in line.get("nightly_rates", []):
                    values = {
                        "global_room_type_id": rt.global_room_type_id,
                        "date": date.fromisoformat(night["date"]),
                        "rate_plan_code": rate_plan_code,
                        "price_minor": night["price_minor"],
                        "currency": night["currency"],
                        "rooms_available_cached": night["rooms_available"],
                        "refundable": refundable,
                        "updated_at": now,
                        "source": SyncSource.reconciliation,
                    }
                    update_cols = {k: v for k, v in values.items() if k not in (
                        "global_room_type_id", "date", "rate_plan_code")}
                    stmt = pg_insert(RateAvailabilityIndex).values(**values).on_conflict_do_update(
                        index_elements=[
                            RateAvailabilityIndex.global_room_type_id,
                            RateAvailabilityIndex.date,
                            RateAvailabilityIndex.rate_plan_code,
                        ],
                        set_=update_cols,
                    )
                    await session.execute(stmt)
                    await index_availability(
                        session,
                        global_room_type_id=rt.global_room_type_id,
                        date_str=night["date"],
                        rate_plan_code=rate_plan_code,
                        price_minor=night["price_minor"],
                        currency=night["currency"],
                        rooms_available=night["rooms_available"],
                        refundable=refundable,
                        updated_at=now.isoformat(),
                    )
            await session.commit()
        await _record_success(session, sh)
        logger.info("Full availability resync done for %s", hotel_slug)
    except HotelERPError as exc:
        logger.warning("Availability resync failed for %s: %s", hotel_slug, exc)
        await session.rollback()
        await _record_failure(session, hotel_id)


async def _active_hotels(session: AsyncSession) -> list[tuple[Hotel, HotelCredential]]:
    rows = await session.execute(
        select(Hotel, HotelCredential)
        .join(HotelCredential, HotelCredential.hotel_id == Hotel.hotel_id)
        .where(Hotel.status == HotelStatus.active)
    )
    return list(rows.all())


# --- arq task entrypoints ------------------------------------------------------

async def reconcile_room_types(ctx: dict) -> None:
    async with AsyncSessionLocal() as session:
        for hotel, cred in await _active_hotels(session):
            await _reconcile_one_hotel_room_types(session, hotel, cred)


async def full_availability_resync(ctx: dict) -> None:
    async with AsyncSessionLocal() as session:
        for hotel, cred in await _active_hotels(session):
            await _resync_one_hotel_availability(session, hotel, cred)
