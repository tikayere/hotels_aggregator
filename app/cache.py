"""Redis helper (contract section 3.3: caching / queues / hold-TTL tracking).

Used to stash the per-booking hold quote (total amount + currency) captured from
Service A's hold response, so the payment step can charge the authoritative
quoted amount without re-trusting the client or adding columns to the ledger.
Keyed by agg_booking_id with a TTL bounded by the hold's own expiry.
"""
from __future__ import annotations

import json

import redis.asyncio as aioredis

from app.config import settings

_redis: aioredis.Redis | None = None


def get_redis() -> aioredis.Redis:
    global _redis
    if _redis is None:
        _redis = aioredis.from_url(settings.redis_url, encoding="utf-8", decode_responses=True)
    return _redis


def _quote_key(agg_booking_id: str) -> str:
    return f"hold_quote:{agg_booking_id}"


async def store_hold_quote(agg_booking_id: str, quote: dict, ttl_seconds: int) -> None:
    await get_redis().set(_quote_key(agg_booking_id), json.dumps(quote), ex=max(ttl_seconds, 60))


async def get_hold_quote(agg_booking_id: str) -> dict | None:
    raw = await get_redis().get(_quote_key(agg_booking_id))
    return json.loads(raw) if raw else None


async def close() -> None:
    global _redis
    if _redis is not None:
        await _redis.aclose()
        _redis = None
