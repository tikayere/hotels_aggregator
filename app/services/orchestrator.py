"""Booking Orchestrator -- drives the contract section 4.8 state machine against
an arbitrary Hotel ERP using only the section 4.5 API (no hotel-specific code).

Flow: hold -> pay -> confirm (or release). Cancellation of a confirmed booking
is policy-gated by Service A and refunded by the Payment Service; Service A never
touches payment (section 4.8 / decision 5.1). Global room-type IDs are already
namespaced ({hotel_slug}.{local_id}), so the same ID Service B indexes is the
room_type_id sent back to Service A -- no translation needed.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import date, datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import cache
from app.clients.hotel_erp_client import HotelERPClient, HotelERPUnavailable
from app.db.models import (
    BookingReference,
    BookingReferenceStatus,
    Hotel,
    HotelCredential,
    PaymentStatus,
    PaymentTransaction,
    RoomTypeIndex,
)
from app.services.payment import PaymentService

logger = logging.getLogger("aggregator.orchestrator")

CONFIRM_MAX_ATTEMPTS = 3
CONFIRM_RETRY_BACKOFF_SECONDS = 0.5


class OrchestratorError(Exception):
    def __init__(self, code: str, message: str, status: int = 409):
        self.code = code
        self.message = message
        self.status = status
        super().__init__(message)


def _parse_dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class BookingOrchestrator:
    def __init__(self, session: AsyncSession, payment_service: PaymentService | None = None):
        self.session = session
        self.payment = payment_service or PaymentService()

    async def _resolve_hotel(self, global_room_type_id: str) -> tuple[Hotel, HotelCredential]:
        rt = await self.session.get(RoomTypeIndex, global_room_type_id)
        if rt is None:
            raise OrchestratorError("NOT_FOUND", f"Unknown room type {global_room_type_id}", status=404)
        hotel = await self.session.get(Hotel, rt.hotel_id)
        cred = await self.session.get(HotelCredential, rt.hotel_id)
        if hotel is None or cred is None:
            raise OrchestratorError("NOT_FOUND", "Hotel or credential not configured", status=404)
        return hotel, cred

    async def _get_booking(self, agg_booking_id: uuid.UUID, traveler_id: uuid.UUID) -> BookingReference:
        booking = await self.session.get(BookingReference, agg_booking_id)
        if booking is None or booking.traveler_id != traveler_id:
            raise OrchestratorError("NOT_FOUND", "Booking not found", status=404)
        return booking

    # -- hold -------------------------------------------------------------------

    async def hold(
        self,
        *,
        traveler_id: uuid.UUID,
        global_room_type_id: str,
        rate_plan_code: str,
        check_in: date,
        check_out: date,
        rooms_requested: int,
        adults: int,
        children: int,
    ) -> BookingReference:
        hotel, cred = await self._resolve_hotel(global_room_type_id)
        client = HotelERPClient.from_credential(cred)

        body = {
            "room_type_id": global_room_type_id,
            "rate_plan_code": rate_plan_code,
            "check_in": check_in.isoformat(),
            "check_out": check_out.isoformat(),
            "rooms_requested": rooms_requested,
            "occupancy": {"adults": adults, "children": children},
            "requested_by": "aggregator",
        }
        # NFR-B3: authoritative availability check happens here against Service A.
        resp = await client.create_hold(body)

        expires_at = _parse_dt(resp["expires_at"])
        booking = BookingReference(
            traveler_id=traveler_id,
            hotel_id=hotel.hotel_id,
            hotel_hold_id=resp["hold_id"],
            global_room_type_id=global_room_type_id,
            check_in=check_in,
            check_out=check_out,
            status=BookingReferenceStatus.held,
            hold_expires_at=expires_at,
        )
        self.session.add(booking)
        await self.session.commit()

        ttl = max(int((expires_at - datetime.now(timezone.utc)).total_seconds()), 60)
        await cache.store_hold_quote(
            str(booking.agg_booking_id),
            {
                "total_amount_minor": resp["total_amount_minor"],
                "currency": resp["currency"],
                "rate_plan_code": rate_plan_code,
            },
            ttl_seconds=ttl,
        )
        return booking

    # -- confirm (payment happens here, then Service A /confirm) -----------------

    async def confirm(
        self,
        *,
        agg_booking_id: uuid.UUID,
        traveler_id: uuid.UUID,
        payment_method_token: str,
        guests: list[dict],
    ) -> BookingReference:
        booking = await self._get_booking(agg_booking_id, traveler_id)
        if booking.status == BookingReferenceStatus.confirmed:
            return booking  # idempotent from the traveler's perspective
        if booking.status != BookingReferenceStatus.held:
            raise OrchestratorError("HOLD_EXPIRED", f"Booking is {booking.status.value}, cannot confirm", 409)
        if booking.hold_expires_at <= datetime.now(timezone.utc):
            booking.status = BookingReferenceStatus.expired
            await self.session.commit()
            raise OrchestratorError("HOLD_EXPIRED", "Hold has expired", 409)

        quote = await cache.get_hold_quote(str(agg_booking_id))
        if quote is None:
            raise OrchestratorError("HOLD_EXPIRED", "Hold quote no longer available", 409)

        # --- Payment step (FR-B5): only confirm the hold once payment succeeds --
        payment = await self.payment.create_and_capture(
            self.session,
            booking=booking,
            amount_minor=quote["total_amount_minor"],
            currency=quote["currency"],
            payment_method_token=payment_method_token,
        )
        if payment.status != PaymentStatus.succeeded:
            # Payment failed -> release the hold, mirror cancelled state.
            await self._release_on_hotel(booking)
            booking.status = BookingReferenceStatus.cancelled
            await self.session.commit()
            raise OrchestratorError("PAYMENT_FAILED", "Payment was declined", 402)

        # --- Confirm against Service A with a STABLE idempotency key (4.8) -------
        idem_key = f"confirm-{agg_booking_id}"
        _, cred = await self._resolve_hotel(booking.global_room_type_id)
        client = HotelERPClient.from_credential(cred)
        confirm_body = {"payment_reference": payment.gateway_ref, "guests": guests}

        last_exc: Exception | None = None
        for attempt in range(1, CONFIRM_MAX_ATTEMPTS + 1):
            try:
                resp = await client.confirm_hold(booking.hotel_hold_id, confirm_body, idempotency_key=idem_key)
                booking.status = BookingReferenceStatus.confirmed
                booking.hotel_reservation_id = resp["reservation_id"]
                await self.session.commit()
                return booking
            except HotelERPUnavailable as exc:
                last_exc = exc
                if booking.hold_expires_at <= datetime.now(timezone.utc):
                    break
                logger.warning("confirm attempt %d for %s failed (unavailable), retrying", attempt, agg_booking_id)
                await asyncio.sleep(CONFIRM_RETRY_BACKOFF_SECONDS)

        # Unresolved: payment captured but hold could not be confirmed and the
        # TTL window is (or is nearly) gone. Do NOT silently fail -- log a
        # distinct BOOKING_CONFIRM_UNRESOLVED marker for manual reconciliation /
        # refund. Leave status 'held' (distinct from confirmed/cancelled) so the
        # ledger flags it for follow-up (contract section 4.8).
        logger.error(
            "BOOKING_CONFIRM_UNRESOLVED agg_booking_id=%s hotel_hold_id=%s payment_id=%s: %s",
            agg_booking_id, booking.hotel_hold_id, payment.payment_id, last_exc,
        )
        raise OrchestratorError(
            "BOOKING_CONFIRM_UNRESOLVED",
            "Payment captured but the hold could not be confirmed with the hotel; escalated for reconciliation.",
            502,
        )

    # -- release ----------------------------------------------------------------

    async def _release_on_hotel(self, booking: BookingReference) -> None:
        try:
            _, cred = await self._resolve_hotel(booking.global_room_type_id)
            client = HotelERPClient.from_credential(cred)
            await client.release_hold(booking.hotel_hold_id, idempotency_key=f"release-{booking.agg_booking_id}")
        except HotelERPUnavailable as exc:
            # Service A will expire the hold on its own TTL anyway; log and move on.
            logger.warning("release for %s could not reach hotel: %s", booking.agg_booking_id, exc)

    async def release(self, *, agg_booking_id: uuid.UUID, traveler_id: uuid.UUID) -> BookingReference:
        booking = await self._get_booking(agg_booking_id, traveler_id)
        if booking.status in (BookingReferenceStatus.cancelled, BookingReferenceStatus.expired):
            return booking
        if booking.status != BookingReferenceStatus.held:
            raise OrchestratorError("CONFLICT", f"Cannot release a {booking.status.value} booking", 409)
        await self._release_on_hotel(booking)
        booking.status = BookingReferenceStatus.cancelled
        await self.session.commit()
        return booking

    # -- cancel a confirmed reservation (policy-gated + refund) ------------------

    async def cancel(self, *, agg_booking_id: uuid.UUID, traveler_id: uuid.UUID) -> tuple[BookingReference, float]:
        booking = await self._get_booking(agg_booking_id, traveler_id)
        if booking.status != BookingReferenceStatus.confirmed or not booking.hotel_reservation_id:
            raise OrchestratorError("CONFLICT", "Only a confirmed reservation can be cancelled", 409)

        _, cred = await self._resolve_hotel(booking.global_room_type_id)
        client = HotelERPClient.from_credential(cred)
        resp = await client.cancel_reservation(
            booking.hotel_reservation_id, idempotency_key=f"cancel-{agg_booking_id}"
        )
        refund_percentage = float(resp.get("refund_percentage", 0) or 0)

        # Execute the refund on Service B's side (Service A never touches payment).
        payment = await self.session.scalar(
            select(PaymentTransaction).where(
                PaymentTransaction.agg_booking_id == agg_booking_id,
                PaymentTransaction.status == PaymentStatus.succeeded,
            )
        )
        if payment is not None and refund_percentage > 0:
            refund_amount = int(round(payment.amount_minor * refund_percentage / 100.0))
            await self.payment.refund(self.session, payment=payment, amount_minor=refund_amount)

        booking.status = BookingReferenceStatus.cancelled
        await self.session.commit()
        return booking, refund_percentage
