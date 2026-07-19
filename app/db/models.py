"""SQLAlchemy 2.0 models for the Central Hospitality Platform (Service B).

Matches hotels/phase_2_service_contracts.md section 3.4 field-for-field.
PostgreSQL-specific types (UUID, JSONB) are used deliberately -- this
service is specified as Postgres-only (section 3.3), not portable to
other engines without changes.

Mirrors the sibling bus/aggregator/app/db/models.py structure and naming
conventions; the two are independent codebases (different domains, no
shared package) but kept stylistically consistent on purpose.
"""
from __future__ import annotations

import enum
import uuid
from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Enum as SAEnum,
    Float,
    ForeignKey,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


class HotelStatus(str, enum.Enum):
    onboarding = "onboarding"
    active = "active"
    suspended = "suspended"


class Hotel(Base):
    __tablename__ = "hotel"

    hotel_id: Mapped[uuid.UUID] = _uuid_pk()
    slug: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    legal_name: Mapped[str] = mapped_column(String(255), nullable=False)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    city: Mapped[str] = mapped_column(String(128), nullable=False)
    country: Mapped[str] = mapped_column(String(2), nullable=False)  # ISO-3166 alpha-2
    lat: Mapped[float | None] = mapped_column(Float)
    lon: Mapped[float | None] = mapped_column(Float)
    star_rating: Mapped[int | None] = mapped_column(SmallInteger)
    status: Mapped[HotelStatus] = mapped_column(
        SAEnum(HotelStatus, name="hotel_status"), nullable=False, default=HotelStatus.onboarding
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    credential: Mapped["HotelCredential"] = relationship(
        back_populates="hotel", uselist=False, cascade="all, delete-orphan"
    )
    sync_health: Mapped["SyncHealth"] = relationship(
        back_populates="hotel", uselist=False, cascade="all, delete-orphan"
    )


class HotelCredential(Base):
    """The only secrets needed to call this hotel's ERP (contract section 4.2).

    api_key_encrypted / webhook_secret_encrypted hold ciphertext, encrypted at
    the application layer -- never plaintext, never logged.
    """

    __tablename__ = "hotel_credential"

    hotel_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hotel.hotel_id", ondelete="CASCADE"), primary_key=True
    )
    api_base_url: Mapped[str] = mapped_column(String(512), nullable=False)
    api_key_encrypted: Mapped[str] = mapped_column(String(1024), nullable=False)
    oauth_client_id: Mapped[str | None] = mapped_column(String(255))
    webhook_secret_encrypted: Mapped[str] = mapped_column(String(1024), nullable=False)
    hold_ttl_seconds_default: Mapped[int] = mapped_column(Integer, nullable=False, default=300)

    hotel: Mapped["Hotel"] = relationship(back_populates="credential")


class RoomTypeIndex(Base):
    """Denormalized copy of a hotel's room type, keyed by the namespaced global
    ID (contract section 4.1, principles 6-7). Only the Sync Engine (webhook
    consumer / reconciliation poller) may write to this table. No specific
    room number ever appears here -- rooms are never sold at that granularity
    (contract section 4.1 principle 3).
    """

    __tablename__ = "room_type_index"

    global_room_type_id: Mapped[str] = mapped_column(String(255), primary_key=True)  # "{hotel_slug}.{code}"
    hotel_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hotel.hotel_id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    max_occupancy_adults: Mapped[int] = mapped_column(Integer, nullable=False)
    max_occupancy_children: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    amenities: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    photos: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class SyncSource(str, enum.Enum):
    webhook = "webhook"
    reconciliation = "reconciliation"


class RateAvailabilityIndex(Base):
    """One row per (room type, night, rate plan) -- the search-index-backing
    cache for both pricing and availability. `rooms_available_cached` is
    exactly that -- a cache; the real per-night check happens at hold time
    against Service A (contract NFR-B3).
    """

    __tablename__ = "rate_availability_index"

    global_room_type_id: Mapped[str] = mapped_column(
        String(255), ForeignKey("room_type_index.global_room_type_id", ondelete="CASCADE"), primary_key=True
    )
    date: Mapped[date] = mapped_column(Date, primary_key=True)
    rate_plan_code: Mapped[str] = mapped_column(String(32), primary_key=True)
    price_minor: Mapped[int] = mapped_column(Integer, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    rooms_available_cached: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    source: Mapped[SyncSource] = mapped_column(SAEnum(SyncSource, name="sync_source"), nullable=False)


class TravelerAccount(Base):
    __tablename__ = "traveler_account"

    traveler_id: Mapped[uuid.UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    phone: Mapped[str | None] = mapped_column(String(32))
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class BookingReferenceStatus(str, enum.Enum):
    pending_payment = "pending_payment"
    held = "held"
    confirmed = "confirmed"
    cancelled = "cancelled"
    expired = "expired"


class BookingReference(Base):
    """The Aggregator's own ledger entry for a reservation. hotel_hold_id /
    hotel_reservation_id are opaque strings owned by Service A (contract
    section 4.4) -- never parsed, never assumed to have any particular format.
    Guest identity documents are never stored here or anywhere in this
    service (contract section 5.6, NFR-B6) -- only what FastAPI needs to
    drive the booking flow and what the payment gateway needs.
    """

    __tablename__ = "booking_reference"

    agg_booking_id: Mapped[uuid.UUID] = _uuid_pk()
    traveler_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("traveler_account.traveler_id"), nullable=False, index=True
    )
    hotel_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hotel.hotel_id"), nullable=False, index=True
    )
    hotel_hold_id: Mapped[str] = mapped_column(String(255), nullable=False)
    hotel_reservation_id: Mapped[str | None] = mapped_column(String(255))
    global_room_type_id: Mapped[str] = mapped_column(
        String(255), ForeignKey("room_type_index.global_room_type_id"), nullable=False, index=True
    )
    check_in: Mapped[date] = mapped_column(Date, nullable=False)
    check_out: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[BookingReferenceStatus] = mapped_column(
        SAEnum(BookingReferenceStatus, name="booking_reference_status"),
        nullable=False,
        default=BookingReferenceStatus.pending_payment,
    )
    # use_alter=True breaks the circular FK with payment_transaction.agg_booking_id
    # (a payment always belongs to a booking; a booking optionally points back at
    # its successful payment) -- without it, neither table could be created first.
    payment_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("payment_transaction.payment_id", use_alter=True, name="fk_booking_reference_payment_id"),
    )
    # Local mirror of Service A's hold TTL so this service can independently
    # expire a booking_reference even if Service A's expiry webhook is late
    # or lost (contract section 4.8, "Failure/edge handling").
    hold_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint("hotel_id", "hotel_hold_id", name="uq_booking_reference_hotel_hold"),
    )


class PaymentStatus(str, enum.Enum):
    pending = "pending"
    succeeded = "succeeded"
    failed = "failed"
    refunded = "refunded"


class PaymentTransaction(Base):
    __tablename__ = "payment_transaction"

    payment_id: Mapped[uuid.UUID] = _uuid_pk()
    agg_booking_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("booking_reference.agg_booking_id"), nullable=False, index=True
    )
    amount_minor: Mapped[int] = mapped_column(Integer, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    gateway_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[PaymentStatus] = mapped_column(
        SAEnum(PaymentStatus, name="payment_status"), nullable=False, default=PaymentStatus.pending
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Review(Base):
    """Post-stay guest review, entirely Service B-owned (contract section 5.7,
    FR-B7) -- deliberately not synced from a hotel's internal CRM/complaints
    module.
    """

    __tablename__ = "review"

    review_id: Mapped[uuid.UUID] = _uuid_pk()
    agg_booking_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("booking_reference.agg_booking_id"), nullable=False, index=True
    )
    hotel_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hotel.hotel_id"), nullable=False, index=True
    )
    rating: Mapped[int] = mapped_column(SmallInteger, nullable=False)  # 1-5
    comment: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint("agg_booking_id", name="uq_review_one_per_booking"),
    )


class WebhookInbox(Base):
    """Dedupe log for the at-least-once delivery guarantee (contract section
    4.7). A row existing for event_id means "already processed" -- the
    webhook receiver must check/insert into this table before applying any
    event, and return 200 either way (contract section 4.6).
    """

    __tablename__ = "webhook_inbox"

    event_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    hotel_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hotel.hotel_id"), nullable=False, index=True
    )
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SyncHealthStatus(str, enum.Enum):
    healthy = "healthy"
    degraded = "degraded"
    down = "down"


class SyncHealth(Base):
    __tablename__ = "sync_health"

    hotel_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hotel.hotel_id", ondelete="CASCADE"), primary_key=True
    )
    last_event_received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_reconciliation_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    consecutive_failures: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[SyncHealthStatus] = mapped_column(
        SAEnum(SyncHealthStatus, name="sync_health_status"), nullable=False, default=SyncHealthStatus.healthy
    )

    hotel: Mapped["Hotel"] = relationship(back_populates="sync_health")
