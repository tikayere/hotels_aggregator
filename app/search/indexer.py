"""Search-index upsert helpers, called from every read-model write path.

Both the webhook consumer (app/services/sync_engine.py) and the reconciliation
poller (app/workers/reconciliation.py) call these after committing to Postgres,
so OpenSearch mirrors room_type_index / rate_availability_index. Failures here
are logged and swallowed: the search index is a cache (NFR-B3), never allowed to
break the authoritative Postgres write.
"""
from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Hotel, RoomTypeIndex
from app.search.opensearch_client import INDEX_NAME, get_client

logger = logging.getLogger("aggregator.indexer")

# Fallback only: rate-plan-code substrings that mark a non-refundable rate,
# used exclusively when a payload doesn't carry a real `refundable` value
# (an older hotel_erp deployment that predates that field). Once every
# connected hotel is on a version that sends it, this and _infer_refundable
# below can be deleted -- the real value from RateAvailabilityIndex.refundable
# should always be preferred over this guess.
_NONREFUNDABLE_MARKERS = ("NONREF", "NON-REF", "NR", "NONREFUNDABLE", "ADVANCE", "SAVER")


def _infer_refundable(rate_plan_code: str) -> bool:
    upper = rate_plan_code.upper()
    return not any(marker in upper for marker in _NONREFUNDABLE_MARKERS)


def _doc_id(global_room_type_id: str, date_str: str, rate_plan_code: str) -> str:
    return f"{global_room_type_id}|{date_str}|{rate_plan_code}"


async def index_availability(
    session: AsyncSession,
    *,
    global_room_type_id: str,
    date_str: str,
    rate_plan_code: str,
    price_minor: int,
    currency: str,
    rooms_available: int,
    updated_at: str,
    refundable: bool | None = None,
) -> None:
    """Upsert one (room type, night, rate plan) search document, denormalizing
    the room type + hotel attributes needed for search filters.
    """
    stmt = (
        select(RoomTypeIndex, Hotel)
        .join(Hotel, Hotel.hotel_id == RoomTypeIndex.hotel_id)
        .where(RoomTypeIndex.global_room_type_id == global_room_type_id)
    )
    row = (await session.execute(stmt)).first()
    if row is None:
        # Availability for a room type we have not indexed yet -- skip; the
        # room_type.created event or reconciliation backfill will bring it in,
        # and the availability row is safely persisted in Postgres regardless.
        return
    rt, hotel = row

    doc = {
        "global_room_type_id": global_room_type_id,
        "hotel_id": str(hotel.hotel_id),
        "hotel_slug": hotel.slug,
        "hotel_display_name": hotel.display_name,
        "city": hotel.city,
        "country": hotel.country,
        "star_rating": hotel.star_rating,
        "room_type_name": rt.name,
        "description": rt.description,
        "max_occupancy_adults": rt.max_occupancy_adults,
        "max_occupancy_children": rt.max_occupancy_children,
        "amenities": rt.amenities or [],
        "photos": rt.photos or [],
        "active": rt.active,
        "date": date_str,
        "rate_plan_code": rate_plan_code,
        "refundable": refundable if refundable is not None else _infer_refundable(rate_plan_code),
        "price_minor": price_minor,
        "currency": currency,
        "rooms_available": rooms_available,
        "updated_at": updated_at,
    }
    try:
        await get_client().index(
            index=INDEX_NAME,
            id=_doc_id(global_room_type_id, date_str, rate_plan_code),
            body=doc,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("OpenSearch index_availability failed for %s: %s", global_room_type_id, exc)


async def update_room_type_docs(global_room_type_id: str, *, active: bool, room_type_fields: dict) -> None:
    """Patch the denormalized room-type attributes on every availability doc for
    a room type after a room_type.created/updated/deleted event.
    """
    params = {"active": active, **room_type_fields}
    inline = "; ".join(f"ctx._source.{k} = params.{k}" for k in params)
    try:
        await get_client().update_by_query(
            index=INDEX_NAME,
            body={
                "query": {"term": {"global_room_type_id": global_room_type_id}},
                "script": {"source": inline, "lang": "painless", "params": params},
            },
            conflicts="proceed",
            refresh=True,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("OpenSearch update_room_type_docs failed for %s: %s", global_room_type_id, exc)
