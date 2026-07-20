"""Payment status endpoint + Stripe webhook receiver (FR-B5).

The Orchestrator confirms/declines a payment synchronously inside the
confirm flow (app/services/orchestrator.py's PaymentService.create_and_capture,
app/services/payment.py's StripePaymentGateway) -- that's enough for the
happy path, a card that doesn't need 3D Secure. The webhook below is
defense in depth, not the primary confirmation path: Stripe's own guidance
is to still listen for webhooks as the source of truth regardless, since a
network failure between this service and Stripe *after* Stripe already
succeeded/failed would otherwise leave payment_transaction permanently out
of sync with what Stripe actually did. Deliberately narrow -- only the two
event types that can change a payment's status *after* the synchronous
call already returned are handled; everything else is accepted (200) and
ignored, same forward-compatibility posture as the hotel webhook receiver
in app/main.py (contract 4.3's "consumers must tolerate unknown event
types" applies just as much to a gateway's own event catalog growing).
"""
from __future__ import annotations

import logging
import uuid

import stripe
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_traveler
from app.config import settings
from app.db.models import BookingReference, PaymentStatus, PaymentTransaction, TravelerAccount
from app.db.session import get_session

logger = logging.getLogger("aggregator.payments")

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


@router.post("/webhooks/stripe")
async def receive_stripe_webhook(request: Request, session: AsyncSession = Depends(get_session)) -> Response:
    raw_body = await request.body()
    signature = request.headers.get("Stripe-Signature")

    if not settings.stripe_webhook_secret:
        # Mirrors get_payment_gateway()'s own dev fallback: no secret
        # configured means Stripe isn't actually wired up (MockPaymentGateway
        # is active instead), so there's nothing real to verify against yet.
        return Response(status_code=200)

    try:
        event = stripe.Webhook.construct_event(raw_body, signature, settings.stripe_webhook_secret)
    except (ValueError, stripe.error.SignatureVerificationError) as exc:
        logger.warning("Stripe webhook signature verification failed: %s", exc)
        return Response(status_code=401)

    data = event["data"]["object"]
    event_type = event["type"]

    if event_type == "payment_intent.payment_failed":
        await _mark_payment_status(session, gateway_ref=data["id"], status=PaymentStatus.failed)
    elif event_type == "charge.refunded":
        payment_intent_id = data.get("payment_intent")
        if payment_intent_id:
            await _mark_payment_status(session, gateway_ref=payment_intent_id, status=PaymentStatus.refunded)
    else:
        logger.info("Ignoring unhandled Stripe event type %r", event_type)

    return Response(status_code=200)


async def _mark_payment_status(session: AsyncSession, *, gateway_ref: str, status: PaymentStatus) -> None:
    payment = await session.scalar(select(PaymentTransaction).where(PaymentTransaction.gateway_ref == gateway_ref))
    if payment is None:
        logger.warning("Stripe webhook referenced unknown gateway_ref=%s", gateway_ref)
        return
    payment.status = status
    await session.commit()
