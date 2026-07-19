"""Payment status endpoint (FR-B5).

In this build the mock gateway captures synchronously inside the confirm flow
(app/services/orchestrator.py), so there is no external gateway webhook to
receive. This router exposes read-only payment status for a booking. When a real
gateway with asynchronous capture is introduced, its signed webhook handler
belongs here -- and only app/services/payment.py otherwise changes.
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_traveler
from app.db.models import BookingReference, PaymentTransaction, TravelerAccount
from app.db.session import get_session
from pydantic import BaseModel

router = APIRouter(prefix="/api/v1/payments", tags=["payments"])


class PaymentOut(BaseModel):
    payment_id: uuid.UUID
    agg_booking_id: uuid.UUID
    amount_minor: int
    currency: str
    gateway_ref: str
    status: str


@router.get("/{agg_booking_id}", response_model=list[PaymentOut])
async def payment_status(
    agg_booking_id: uuid.UUID,
    traveler: TravelerAccount = Depends(get_current_traveler),
    session: AsyncSession = Depends(get_session),
) -> list[PaymentOut]:
    booking = await session.get(BookingReference, agg_booking_id)
    if booking is None or booking.traveler_id != traveler.traveler_id:
        raise HTTPException(404, detail="Booking not found")
    rows = await session.scalars(
        select(PaymentTransaction).where(PaymentTransaction.agg_booking_id == agg_booking_id)
    )
    return [
        PaymentOut(
            payment_id=p.payment_id,
            agg_booking_id=p.agg_booking_id,
            amount_minor=p.amount_minor,
            currency=p.currency,
            gateway_ref=p.gateway_ref,
            status=p.status.value,
        )
        for p in rows
    ]
