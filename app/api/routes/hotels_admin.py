"""Hotel Registry / Onboarding (FR-B1).

Registers a Hotel + HotelCredential (api_key / webhook_secret encrypted at rest
via the Fernet helper before storage -- never plaintext), and exposes each
hotel's SyncHealth. `router` (the /admin/hotels routes: create/list/update/
delete a hotel and its credentials) is operator-only, gated by `require_admin`
-- these routes can fully take over a hotel's sync relationship with this
platform, so they must never be reachable without the admin key. `public_router`
(GET .../sync-health) is deliberately left open: it exposes no secrets, and a
hotel's own ops team or a status page may reasonably want to read it
unauthenticated.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_admin
from app.db.models import Hotel, HotelCredential, HotelStatus, SyncHealth
from app.db.session import get_session
from app.schemas.api import HotelCreate, HotelOut, HotelUpdate, SyncHealthOut
from app.security.crypto import encrypt_secret

router = APIRouter(prefix="/api/v1/admin/hotels", tags=["hotels-admin"], dependencies=[Depends(require_admin)])
public_router = APIRouter(prefix="/api/v1/hotels", tags=["hotels-admin"])


def _to_out(h: Hotel) -> HotelOut:
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
        status=h.status.value,
    )


@router.post("", response_model=HotelOut, status_code=201)
async def create_hotel(payload: HotelCreate, session: AsyncSession = Depends(get_session)) -> HotelOut:
    existing = await session.scalar(select(Hotel).where(Hotel.slug == payload.slug))
    if existing is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="Slug already registered")

    hotel = Hotel(
        slug=payload.slug,
        legal_name=payload.legal_name,
        display_name=payload.display_name,
        city=payload.city,
        country=payload.country.upper(),
        lat=payload.lat,
        lon=payload.lon,
        star_rating=payload.star_rating,
        status=HotelStatus.active,
    )
    session.add(hotel)
    await session.flush()

    credential = HotelCredential(
        hotel_id=hotel.hotel_id,
        api_base_url=payload.api_base_url,
        api_key_encrypted=encrypt_secret(payload.api_key),
        webhook_secret_encrypted=encrypt_secret(payload.webhook_secret),
        oauth_client_id=payload.oauth_client_id,
        hold_ttl_seconds_default=payload.hold_ttl_seconds_default,
    )
    session.add(credential)
    session.add(SyncHealth(hotel_id=hotel.hotel_id))
    await session.commit()
    return _to_out(hotel)


@router.get("", response_model=list[HotelOut])
async def list_hotels(session: AsyncSession = Depends(get_session)) -> list[HotelOut]:
    rows = await session.scalars(select(Hotel).order_by(Hotel.created_at.desc()))
    return [_to_out(h) for h in rows]


@router.patch("/{slug}", response_model=HotelOut)
async def update_hotel(slug: str, payload: HotelUpdate, session: AsyncSession = Depends(get_session)) -> HotelOut:
    hotel = await session.scalar(select(Hotel).where(Hotel.slug == slug))
    if hotel is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Hotel not found")

    for field in ("legal_name", "display_name", "city", "lat", "lon", "star_rating"):
        value = getattr(payload, field)
        if value is not None:
            setattr(hotel, field, value)
    if payload.country is not None:
        hotel.country = payload.country.upper()
    if payload.status is not None:
        try:
            hotel.status = HotelStatus(payload.status)
        except ValueError:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Invalid status")

    cred = await session.get(HotelCredential, hotel.hotel_id)
    if cred is not None:
        if payload.api_base_url is not None:
            cred.api_base_url = payload.api_base_url
        if payload.api_key is not None:
            cred.api_key_encrypted = encrypt_secret(payload.api_key)
        if payload.webhook_secret is not None:
            cred.webhook_secret_encrypted = encrypt_secret(payload.webhook_secret)
        if payload.hold_ttl_seconds_default is not None:
            cred.hold_ttl_seconds_default = payload.hold_ttl_seconds_default

    await session.commit()
    return _to_out(hotel)


@router.delete("/{slug}", status_code=204)
async def delete_hotel(slug: str, session: AsyncSession = Depends(get_session)) -> None:
    hotel = await session.scalar(select(Hotel).where(Hotel.slug == slug))
    if hotel is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Hotel not found")
    await session.delete(hotel)
    await session.commit()


@public_router.get("/{slug}/sync-health", response_model=SyncHealthOut)
async def sync_health(slug: str, session: AsyncSession = Depends(get_session)) -> SyncHealthOut:
    hotel = await session.scalar(select(Hotel).where(Hotel.slug == slug))
    if hotel is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Hotel not found")
    sh = await session.get(SyncHealth, hotel.hotel_id)
    if sh is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="No sync health record")
    return SyncHealthOut(
        hotel_id=sh.hotel_id,
        last_event_received_at=sh.last_event_received_at,
        last_reconciliation_at=sh.last_reconciliation_at,
        consecutive_failures=sh.consecutive_failures,
        status=sh.status.value,
    )
