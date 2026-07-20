"""Traveler-facing accounts + public hotel detail (FR-B6)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_traveler
from app.db.models import BookingReference, Hotel, RoomTypeIndex, TravelerAccount
from app.db.session import get_session
from app.schemas.api import (
    BookingOut,
    HotelOut,
    TokenResponse,
    TravelerLogin,
    TravelerOut,
    TravelerRegister,
)
from app.security.auth import create_access_token, hash_password, verify_password

router = APIRouter(prefix="/api/v1", tags=["travelers"])


@router.post("/auth/register", response_model=TokenResponse, status_code=201)
async def register(payload: TravelerRegister, session: AsyncSession = Depends(get_session)) -> TokenResponse:
    existing = await session.scalar(select(TravelerAccount).where(TravelerAccount.email == payload.email))
    if existing is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="Email already registered")
    traveler = TravelerAccount(
        name=payload.name,
        email=payload.email,
        phone=payload.phone,
        hashed_password=hash_password(payload.password),
    )
    session.add(traveler)
    await session.commit()
    return TokenResponse(access_token=create_access_token(traveler.traveler_id, traveler.email))


@router.post("/auth/login", response_model=TokenResponse)
async def login(payload: TravelerLogin, session: AsyncSession = Depends(get_session)) -> TokenResponse:
    traveler = await session.scalar(select(TravelerAccount).where(TravelerAccount.email == payload.email))
    if traveler is None or not verify_password(payload.password, traveler.hashed_password):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")
    return TokenResponse(access_token=create_access_token(traveler.traveler_id, traveler.email))


@router.get("/me", response_model=TravelerOut)
async def me(traveler: TravelerAccount = Depends(get_current_traveler)) -> TravelerOut:
    return TravelerOut(
        traveler_id=traveler.traveler_id, name=traveler.name, email=traveler.email, phone=traveler.phone
    )


@router.get("/me/bookings", response_model=list[BookingOut])
async def my_bookings(
    traveler: TravelerAccount = Depends(get_current_traveler),
    session: AsyncSession = Depends(get_session),
) -> list[BookingOut]:
    rows = await session.scalars(
        select(BookingReference)
        .where(BookingReference.traveler_id == traveler.traveler_id)
        .order_by(BookingReference.created_at.desc())
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


@router.get("/hotels/{slug}", response_model=HotelOut)
async def hotel_detail(slug: str, session: AsyncSession = Depends(get_session)) -> HotelOut:
    hotel = await session.scalar(select(Hotel).where(Hotel.slug == slug))
    if hotel is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Hotel not found")
    return HotelOut(
        hotel_id=hotel.hotel_id,
        slug=hotel.slug,
        legal_name=hotel.legal_name,
        display_name=hotel.display_name,
        city=hotel.city,
        country=hotel.country,
        lat=hotel.lat,
        lon=hotel.lon,
        star_rating=hotel.star_rating,
        photos=hotel.photos,
        status=hotel.status.value,
    )
