"""Hotel Portal (FR-B1 self-service side): a hotel's own staff, logged in as a
HotelUser, managing their own listing. Every route here is scoped by
`hotel_user.hotel_id` -- never by a hotel_id/slug taken from the request --
so a compromised or malicious portal login can only ever touch its own
hotel's data, never another hotel's.

Deliberately read-only for anything the ERP owns (room types, rates,
availability, bookings): NFR-B3 says this service is never authoritative for
a hotel's inventory, and that applies just as much to what a portal UI is
allowed to write as it does to the Sync Engine. What this router *does* let a
hotel edit -- display_name/city/star_rating/photos, promotions -- has no ERP
equivalent; it's Aggregator-owned marketing data layered on top of the ERP's
own system-of-record fields.
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_hotel_user
from app.db.models import (
    BookingReference,
    Hotel,
    HotelUser,
    Promotion,
    RoomTypeIndex,
    SyncHealth,
)
from app.db.session import get_session
from app.schemas.api import (
    BookingOut,
    HotelOut,
    HotelPortalLogin,
    HotelProfileUpdate,
    HotelUserOut,
    PromotionCreate,
    PromotionOut,
    PromotionUpdate,
    RoomTypeOut,
    SyncHealthOut,
    TokenResponse,
)
from app.security.auth import create_hotel_access_token, verify_password

router = APIRouter(prefix="/api/v1/hotel", tags=["hotel-portal"])


def _hotel_out(h: Hotel) -> HotelOut:
    return HotelOut(
        hotel_id=h.hotel_id,
        slug=h.slug,
        legal_name=h.legal_name,
        display_name=h.display_name,
        city=h.city,
        country=h.country,
        lat=h.lat,
        lon=h.lon,
        star_rating=h.star_rating,
        photos=h.photos,
        status=h.status.value,
    )


def _promotion_out(p: Promotion) -> PromotionOut:
    return PromotionOut(
        promotion_id=p.promotion_id,
        hotel_id=p.hotel_id,
        title=p.title,
        description=p.description,
        discount_percentage=p.discount_percentage,
        starts_on=p.starts_on,
        ends_on=p.ends_on,
        active=p.active,
    )


@router.post("/auth/login", response_model=TokenResponse)
async def login(payload: HotelPortalLogin, session: AsyncSession = Depends(get_session)) -> TokenResponse:
    user = await session.scalar(select(HotelUser).where(HotelUser.email == payload.email))
    if user is None or not verify_password(payload.password, user.hashed_password):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")
    return TokenResponse(access_token=create_hotel_access_token(user.hotel_user_id, user.email))


@router.get("/me", response_model=HotelUserOut)
async def me(user: HotelUser = Depends(get_current_hotel_user)) -> HotelUserOut:
    return HotelUserOut(
        hotel_user_id=user.hotel_user_id, hotel_id=user.hotel_id, name=user.name, email=user.email, role=user.role.value
    )


@router.get("/me/hotel", response_model=HotelOut)
async def my_hotel(
    user: HotelUser = Depends(get_current_hotel_user), session: AsyncSession = Depends(get_session)
) -> HotelOut:
    hotel = await session.get(Hotel, user.hotel_id)
    if hotel is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Hotel not found")
    return _hotel_out(hotel)


@router.patch("/me/hotel", response_model=HotelOut)
async def update_my_hotel(
    payload: HotelProfileUpdate,
    user: HotelUser = Depends(get_current_hotel_user),
    session: AsyncSession = Depends(get_session),
) -> HotelOut:
    hotel = await session.get(Hotel, user.hotel_id)
    if hotel is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Hotel not found")
    for field in ("display_name", "city", "lat", "lon", "star_rating", "photos"):
        value = getattr(payload, field)
        if value is not None:
            setattr(hotel, field, value)
    await session.commit()
    return _hotel_out(hotel)


@router.get("/me/sync-health", response_model=SyncHealthOut)
async def my_sync_health(
    user: HotelUser = Depends(get_current_hotel_user), session: AsyncSession = Depends(get_session)
) -> SyncHealthOut:
    sh = await session.get(SyncHealth, user.hotel_id)
    if sh is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="No sync health record")
    return SyncHealthOut(
        hotel_id=sh.hotel_id,
        last_event_received_at=sh.last_event_received_at,
        last_reconciliation_at=sh.last_reconciliation_at,
        consecutive_failures=sh.consecutive_failures,
        status=sh.status.value,
    )


@router.get("/me/room-types", response_model=list[RoomTypeOut])
async def my_room_types(
    user: HotelUser = Depends(get_current_hotel_user), session: AsyncSession = Depends(get_session)
) -> list[RoomTypeOut]:
    rows = await session.scalars(
        select(RoomTypeIndex).where(RoomTypeIndex.hotel_id == user.hotel_id).order_by(RoomTypeIndex.name)
    )
    return [
        RoomTypeOut(
            global_room_type_id=r.global_room_type_id,
            name=r.name,
            description=r.description,
            max_occupancy_adults=r.max_occupancy_adults,
            max_occupancy_children=r.max_occupancy_children,
            amenities=r.amenities,
            photos=r.photos,
            active=r.active,
        )
        for r in rows
    ]


@router.get("/me/bookings", response_model=list[BookingOut])
async def my_bookings(
    user: HotelUser = Depends(get_current_hotel_user), session: AsyncSession = Depends(get_session)
) -> list[BookingOut]:
    rows = await session.scalars(
        select(BookingReference)
        .where(BookingReference.hotel_id == user.hotel_id)
        .order_by(BookingReference.created_at.desc())
        .limit(200)
    )
    return [
        BookingOut(
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
        for b in rows
    ]


@router.get("/me/promotions", response_model=list[PromotionOut])
async def list_promotions(
    user: HotelUser = Depends(get_current_hotel_user), session: AsyncSession = Depends(get_session)
) -> list[PromotionOut]:
    rows = await session.scalars(
        select(Promotion).where(Promotion.hotel_id == user.hotel_id).order_by(Promotion.starts_on.desc())
    )
    return [_promotion_out(p) for p in rows]


@router.post("/me/promotions", response_model=PromotionOut, status_code=201)
async def create_promotion(
    payload: PromotionCreate,
    user: HotelUser = Depends(get_current_hotel_user),
    session: AsyncSession = Depends(get_session),
) -> PromotionOut:
    if payload.ends_on < payload.starts_on:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="ends_on must not be before starts_on")
    promo = Promotion(
        hotel_id=user.hotel_id,
        title=payload.title,
        description=payload.description,
        discount_percentage=payload.discount_percentage,
        starts_on=payload.starts_on,
        ends_on=payload.ends_on,
    )
    session.add(promo)
    await session.commit()
    return _promotion_out(promo)


async def _get_own_promotion(promotion_id: uuid.UUID, user: HotelUser, session: AsyncSession) -> Promotion:
    promo = await session.get(Promotion, promotion_id)
    if promo is None or promo.hotel_id != user.hotel_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Promotion not found")
    return promo


@router.patch("/me/promotions/{promotion_id}", response_model=PromotionOut)
async def update_promotion(
    promotion_id: uuid.UUID,
    payload: PromotionUpdate,
    user: HotelUser = Depends(get_current_hotel_user),
    session: AsyncSession = Depends(get_session),
) -> PromotionOut:
    promo = await _get_own_promotion(promotion_id, user, session)
    for field in ("title", "description", "discount_percentage", "starts_on", "ends_on", "active"):
        value = getattr(payload, field)
        if value is not None:
            setattr(promo, field, value)
    if promo.ends_on < promo.starts_on:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="ends_on must not be before starts_on")
    await session.commit()
    return _promotion_out(promo)


@router.delete("/me/promotions/{promotion_id}", status_code=204)
async def delete_promotion(
    promotion_id: uuid.UUID,
    user: HotelUser = Depends(get_current_hotel_user),
    session: AsyncSession = Depends(get_session),
) -> None:
    promo = await _get_own_promotion(promotion_id, user, session)
    await session.delete(promo)
    await session.commit()
