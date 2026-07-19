"""Pydantic models for the inbound webhook envelope + per-event `data` payloads.

Matches hotels/openapi/aggregator-webhook-api.yaml exactly: a base envelope
(section 4.4) plus a discriminated union on `event_type` (section 4.7 catalog).
Unknown fields are ignored (contract section 4.3: consumers must ignore unknown
fields; additive changes are non-breaking).
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Literal, Union

from pydantic import BaseModel, Field


# --- per-event `data` payloads -------------------------------------------------

class RoomTypeData(BaseModel):
    room_type_id: str
    name: str
    description: str | None = None
    max_occupancy_adults: int
    max_occupancy_children: int = 0
    bed_config: str | None = None
    size_sqm: float | None = None
    amenities: list[str] = Field(default_factory=list)
    photos: list[str] = Field(default_factory=list)
    active: bool
    updated_at: datetime


class RoomTypeDeletedData(BaseModel):
    room_type_id: str


class NightlyAvailabilityData(BaseModel):
    room_type_id: str
    date: date
    rate_plan_code: str
    rooms_available: int
    price_minor: int
    currency: str
    # Optional, not required: an older hotel_erp deployment that hasn't
    # picked up this field yet must still validate here (contract section
    # 4.3 -- additive fields never require a version bump, and a consumer
    # must tolerate a sender that doesn't send one yet). None means
    # "unknown," not "false" -- see RateAvailabilityIndex.refundable.
    refundable: bool | None = None


class ReservationStatusData(BaseModel):
    reservation_id: str
    room_type_id: str


class SyncHeartbeatData(BaseModel):
    hotel_id: str
    server_time: datetime


class WaitlistAvailableData(BaseModel):
    """IDs and dates only, deliberately -- never the waiting traveler's
    contact info (contract section 4.7 note / section 5.6): this event exists
    so Service B can trigger its own traveler notification, it is not itself
    the notification."""

    room_type_id: str
    rate_plan_code: str
    check_in: date
    check_out: date


# --- envelope + discriminated union -------------------------------------------

class _EnvelopeBase(BaseModel):
    event_id: str
    hotel_id: str
    schema_version: str = "1.0"
    occurred_at: datetime


class RoomTypeUpsertEvent(_EnvelopeBase):
    event_type: Literal["room_type.created", "room_type.updated"]
    data: RoomTypeData


class RoomTypeDeletedEvent(_EnvelopeBase):
    event_type: Literal["room_type.deleted"]
    data: RoomTypeDeletedData


class AvailabilityChangedEvent(_EnvelopeBase):
    event_type: Literal["availability.changed"]
    data: NightlyAvailabilityData


class RateChangedEvent(_EnvelopeBase):
    event_type: Literal["rate.changed"]
    data: NightlyAvailabilityData


class ReservationStatusEvent(_EnvelopeBase):
    event_type: Literal["reservation.checked_in", "reservation.checked_out", "reservation.no_show"]
    data: ReservationStatusData


class SyncHeartbeatEvent(_EnvelopeBase):
    event_type: Literal["hotel.sync_heartbeat"]
    data: SyncHeartbeatData


class WaitlistAvailableEvent(_EnvelopeBase):
    event_type: Literal["waitlist.available"]
    data: WaitlistAvailableData


WebhookEvent = Annotated[
    Union[
        RoomTypeUpsertEvent,
        RoomTypeDeletedEvent,
        AvailabilityChangedEvent,
        RateChangedEvent,
        ReservationStatusEvent,
        WaitlistAvailableEvent,
        SyncHeartbeatEvent,
    ],
    Field(discriminator="event_type"),
]


class _MinimalEnvelope(BaseModel):
    """First-pass parse: just enough to route to a hotel + dedupe, before the
    HMAC secret for that hotel is known (contract 4.2 two-pass verification).
    """

    model_config = {"extra": "ignore"}

    event_id: str
    event_type: str
    hotel_id: str
