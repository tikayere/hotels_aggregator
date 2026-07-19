"""Booking Orchestration endpoints (FR-B4, contract section 4.8)."""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_traveler
from app.clients.hotel_erp_client import HotelERPError
from app.db.models import BookingReference, TravelerAccount
from app.db.session import get_session
from app.schemas.api import BookingOut, CancelResponse, ConfirmRequest, HoldRequest
from app.services.orchestrator import BookingOrchestrator, OrchestratorError

router = APIRouter(prefix="/api/v1/bookings", tags=["bookings"])


def _to_out(b: BookingReference) -> BookingOut:
    return BookingOut(
        agg_booking_id=b.agg_booking_id,
        hotel_id=b.hotel_id,
        global_room_type_id=b.global_room_type_id,
        hotel_hold_id=b.hotel_hold_id,
        hotel_reservation_id=b.hotel_reservation_id,
        check_in=b.check_in,
        check_out=b.check_out,
        status=b.status.value,
        hold_expires_at=b.hold_expires_at,
        created_at=b.created_at,
    )


def _handle(exc: Exception) -> HTTPException:
    if isinstance(exc, OrchestratorError):
        return HTTPException(exc.status, detail={"code": exc.code, "message": exc.message})
    if isinstance(exc, HotelERPError):
        return HTTPException(exc.status, detail={"code": exc.code, "message": exc.message, "details": exc.details})
    return HTTPException(500, detail={"code": "INTERNAL_ERROR", "message": str(exc)})


@router.post("/hold", response_model=BookingOut, status_code=201)
async def create_hold(
    payload: HoldRequest,
    traveler: TravelerAccount = Depends(get_current_traveler),
    session: AsyncSession = Depends(get_session),
) -> BookingOut:
    orch = BookingOrchestrator(session)
    try:
        booking = await orch.hold(
            traveler_id=traveler.traveler_id,
            global_room_type_id=payload.global_room_type_id,
            rate_plan_code=payload.rate_plan_code,
            check_in=payload.check_in,
            check_out=payload.check_out,
            rooms_requested=payload.rooms_requested,
            adults=payload.adults,
            children=payload.children,
        )
    except (OrchestratorError, HotelERPError) as exc:
        raise _handle(exc)
    return _to_out(booking)


@router.post("/{agg_booking_id}/confirm", response_model=BookingOut)
async def confirm(
    agg_booking_id: uuid.UUID,
    payload: ConfirmRequest,
    traveler: TravelerAccount = Depends(get_current_traveler),
    session: AsyncSession = Depends(get_session),
) -> BookingOut:
    orch = BookingOrchestrator(session)
    try:
        booking = await orch.confirm(
            agg_booking_id=agg_booking_id,
            traveler_id=traveler.traveler_id,
            payment_method_token=payload.payment_method_token,
            guests=[g.model_dump(exclude_none=True) for g in payload.guests],
        )
    except (OrchestratorError, HotelERPError) as exc:
        raise _handle(exc)
    return _to_out(booking)


@router.post("/{agg_booking_id}/release", response_model=BookingOut)
async def release(
    agg_booking_id: uuid.UUID,
    traveler: TravelerAccount = Depends(get_current_traveler),
    session: AsyncSession = Depends(get_session),
) -> BookingOut:
    orch = BookingOrchestrator(session)
    try:
        booking = await orch.release(agg_booking_id=agg_booking_id, traveler_id=traveler.traveler_id)
    except (OrchestratorError, HotelERPError) as exc:
        raise _handle(exc)
    return _to_out(booking)


@router.post("/{agg_booking_id}/cancel", response_model=CancelResponse)
async def cancel(
    agg_booking_id: uuid.UUID,
    traveler: TravelerAccount = Depends(get_current_traveler),
    session: AsyncSession = Depends(get_session),
) -> CancelResponse:
    orch = BookingOrchestrator(session)
    try:
        booking, refund_pct = await orch.cancel(agg_booking_id=agg_booking_id, traveler_id=traveler.traveler_id)
    except (OrchestratorError, HotelERPError) as exc:
        raise _handle(exc)
    return CancelResponse(booking=_to_out(booking), refund_percentage=refund_pct)
