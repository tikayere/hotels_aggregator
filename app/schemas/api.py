"""Request/response models for the traveler- and admin-facing REST API."""
from __future__ import annotations

import uuid
from datetime import date, datetime

from pydantic import BaseModel, EmailStr, Field


# --- travelers ----------------------------------------------------------------

class TravelerRegister(BaseModel):
    name: str
    email: EmailStr
    phone: str | None = None
    password: str = Field(min_length=8)


class TravelerLogin(BaseModel):
    email: EmailStr
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class TravelerOut(BaseModel):
    traveler_id: uuid.UUID
    name: str
    email: EmailStr
    phone: str | None = None


# --- hotel onboarding (admin) -------------------------------------------------

class HotelCreate(BaseModel):
    slug: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,62}$")
    legal_name: str
    display_name: str
    city: str
    country: str = Field(min_length=2, max_length=2)
    lat: float | None = None
    lon: float | None = None
    star_rating: int | None = Field(default=None, ge=1, le=5)
    api_base_url: str
    api_key: str
    webhook_secret: str
    oauth_client_id: str | None = None
    hold_ttl_seconds_default: int = 300


class HotelUpdate(BaseModel):
    legal_name: str | None = None
    display_name: str | None = None
    city: str | None = None
    country: str | None = Field(default=None, min_length=2, max_length=2)
    lat: float | None = None
    lon: float | None = None
    star_rating: int | None = Field(default=None, ge=1, le=5)
    status: str | None = None
    api_base_url: str | None = None
    api_key: str | None = None
    webhook_secret: str | None = None
    hold_ttl_seconds_default: int | None = None


class HotelOut(BaseModel):
    hotel_id: uuid.UUID
    slug: str
    legal_name: str
    display_name: str
    city: str
    country: str
    lat: float | None = None
    lon: float | None = None
    star_rating: int | None = None
    status: str


class SyncHealthOut(BaseModel):
    hotel_id: uuid.UUID
    last_event_received_at: datetime | None = None
    last_reconciliation_at: datetime | None = None
    consecutive_failures: int
    status: str


# --- bookings -----------------------------------------------------------------

class HoldRequest(BaseModel):
    global_room_type_id: str
    rate_plan_code: str
    check_in: date
    check_out: date
    rooms_requested: int = Field(ge=1, default=1)
    adults: int = Field(ge=1, default=1)
    children: int = Field(ge=0, default=0)


class BookingOut(BaseModel):
    agg_booking_id: uuid.UUID
    hotel_id: uuid.UUID
    global_room_type_id: str
    hotel_hold_id: str
    hotel_reservation_id: str | None = None
    check_in: date
    check_out: date
    status: str
    hold_expires_at: datetime | None = None
    created_at: datetime | None = None


class GuestIn(BaseModel):
    """Minimal guest data allowed across the boundary (contract 4.1.8 / 5.6):
    name + contact only. Passport / national ID are NEVER accepted here.
    """

    name: str
    phone: str | None = None
    email: EmailStr | None = None


class ConfirmRequest(BaseModel):
    payment_method_token: str
    guests: list[GuestIn] = Field(min_length=1)


class CancelResponse(BaseModel):
    booking: BookingOut
    refund_percentage: float


# --- reviews ------------------------------------------------------------------

class ReviewCreate(BaseModel):
    agg_booking_id: uuid.UUID
    rating: int = Field(ge=1, le=5)
    comment: str | None = None


class ReviewOut(BaseModel):
    review_id: uuid.UUID
    agg_booking_id: uuid.UUID
    hotel_id: uuid.UUID
    rating: int
    comment: str | None = None
    created_at: datetime | None = None


# --- search -------------------------------------------------------------------

class SearchResultItem(BaseModel):
    global_room_type_id: str
    hotel_slug: str
    hotel_display_name: str
    city: str
    country: str
    star_rating: int | None = None
    room_type_name: str
    amenities: list[str] = []
    photos: list[str] = []
    total_price_minor: int
    currency: str
    nights: int
    rate_plan_code: str
    refundable: bool
    min_rooms_available: int


class SearchResponse(BaseModel):
    results: list[SearchResultItem]
    count: int
