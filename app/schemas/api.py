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


class TravelerUpdate(BaseModel):
    """All fields optional -- send only what changed. Email is editable here
    (unlike a hotel's slug/legal_name on the portal side) since nothing else
    in the contract references a traveler by email; changing it just means
    that's the address used for the next login. Password is deliberately
    NOT here -- see PasswordChange, which requires the current password."""

    name: str | None = None
    email: EmailStr | None = None
    phone: str | None = None


class PasswordChange(BaseModel):
    current_password: str
    new_password: str = Field(min_length=8)


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
    photos: list[str] = []
    status: str


class SyncHealthOut(BaseModel):
    hotel_id: uuid.UUID
    last_event_received_at: datetime | None = None
    last_reconciliation_at: datetime | None = None
    consecutive_failures: int
    status: str


# --- hotel portal (hotel-user auth + self-service) -----------------------------

class HotelUserCreate(BaseModel):
    """Used by the operator-only POST /admin/hotels/{slug}/users -- a hotel's
    portal login is provisioned, not self-registered (see HotelUser model)."""

    name: str
    email: EmailStr
    password: str = Field(min_length=8)
    role: str = "manager"


class HotelUserOut(BaseModel):
    hotel_user_id: uuid.UUID
    hotel_id: uuid.UUID
    name: str
    email: EmailStr
    role: str


class HotelPortalLogin(BaseModel):
    email: EmailStr
    password: str


class HotelProfileUpdate(BaseModel):
    """Fields a hotel's own portal login may edit about itself. Deliberately
    excludes slug/legal_name/country/status and every HotelCredential field
    (api_base_url/api_key/webhook_secret) -- those stay operator-only
    (hotels_admin.py), since a compromised portal login must never be able to
    redirect where this platform sends webhooks or calls back into the ERP.
    """

    display_name: str | None = None
    city: str | None = None
    lat: float | None = None
    lon: float | None = None
    star_rating: int | None = Field(default=None, ge=1, le=5)
    photos: list[str] | None = None


class PromotionCreate(BaseModel):
    title: str
    description: str | None = None
    discount_percentage: float = Field(gt=0, le=100)
    starts_on: date
    ends_on: date


class PromotionUpdate(BaseModel):
    title: str | None = None
    description: str | None = None
    discount_percentage: float | None = Field(default=None, gt=0, le=100)
    starts_on: date | None = None
    ends_on: date | None = None
    active: bool | None = None


class PromotionOut(BaseModel):
    promotion_id: uuid.UUID
    hotel_id: uuid.UUID
    title: str
    description: str | None = None
    discount_percentage: float
    starts_on: date
    ends_on: date
    active: bool


class RoomTypeOut(BaseModel):
    """Read-only mirror of a hotel's room type -- editing happens in the ERP
    (NFR-B3: this service is never authoritative for inventory)."""

    global_room_type_id: str
    name: str
    description: str | None = None
    max_occupancy_adults: int
    max_occupancy_children: int
    amenities: list[str] = []
    photos: list[str] = []
    active: bool


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
