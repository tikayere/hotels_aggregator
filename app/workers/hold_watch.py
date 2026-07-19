"""Local hold-TTL watcher (contract section 4.8).

Independently expires any booking_reference still in 'held' whose
hold_expires_at has passed, regardless of whether Service A's own expiry
webhook/sweeper has fired -- Service B must not rely solely on a callback.
Runs on a short cron (~30s) via arq.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import update

from app.db.models import BookingReference, BookingReferenceStatus
from app.db.session import AsyncSessionLocal

logger = logging.getLogger("aggregator.hold_watch")


async def expire_stale_holds(ctx: dict) -> None:
    now = datetime.now(timezone.utc)
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            update(BookingReference)
            .where(
                BookingReference.status == BookingReferenceStatus.held,
                BookingReference.hold_expires_at <= now,
            )
            .values(status=BookingReferenceStatus.expired)
            .returning(BookingReference.agg_booking_id)
        )
        expired = result.fetchall()
        await session.commit()
    if expired:
        logger.info("Expired %d stale hold(s): %s", len(expired), [str(r[0]) for r in expired])
