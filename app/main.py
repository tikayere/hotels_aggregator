"""Central Hospitality Platform (Service B) -- FastAPI entrypoint.

Wires the full service: /health, the signature-verified + deduped + dispatched
webhook receiver (contract sections 4.6/4.7), and every traveler-, admin- and
partner-facing router. Background jobs run in a separate arq process
(app/workers/arq_worker.py), not here.

Run locally: `uvicorn app.main:app --reload --port 8000`
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import TypeAdapter, ValidationError
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.routes import bookings, hotel_portal, hotels_admin, payments, reviews, search, travelers
from app.config import settings
from app.db.models import Hotel, HotelCredential, WebhookInbox
from app.db.session import get_session
from app.schemas.webhooks import WebhookEvent, _MinimalEnvelope
from app.search.opensearch_client import close as close_search
from app.search.opensearch_client import ensure_index
from app.security.crypto import decrypt_secret
from app.services.sync_engine import dispatch_event
from app.webhooks.verify import WebhookAuthError, verify_webhook_signature

logger = logging.getLogger("aggregator.main")

app = FastAPI(title="Central Hospitality Platform", version="1.0")

# Portal/web/mobile are separate origins from this API (contract:
# openapi/aggregator-client-api.yaml) -- browsers enforce CORS, native mobile
# doesn't, so this only actually matters for the portal and any future
# traveler web app, but there's no harm enabling it uniformly. Wide open by
# default (`*`) since every route here is either public read data or guarded
# by its own bearer/admin-key check, not by request origin; tighten via
# cors_allowed_origins if a production deployment wants to restrict it to
# known client domains.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_allowed_origins,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

_webhook_validator = TypeAdapter(WebhookEvent)


@app.on_event("startup")
async def _startup() -> None:
    # Best-effort search index bootstrap; never blocks app boot if OpenSearch is
    # not yet reachable (ensure_index swallows/logs its own errors).
    await ensure_index()


@app.on_event("shutdown")
async def _shutdown() -> None:
    await close_search()


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "api_version": "1.0"}


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"error": {"code": code, "message": message, "trace_id": str(uuid.uuid4())}},
    )


_STATUS_CODES = {
    400: "VALIDATION_ERROR",
    401: "UNAUTHORIZED",
    403: "FORBIDDEN",
    404: "NOT_FOUND",
    409: "CONFLICT",
    422: "VALIDATION_ERROR",
    500: "INTERNAL_ERROR",
    503: "SERVICE_UNAVAILABLE",
}


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    """Every client-facing route in this service raises FastAPI's own
    HTTPException, historically with `detail` as either a plain string (most
    routes) or a {"code", "message"} dict (bookings.py, forwarding
    OrchestratorError/HotelERPError as-is). That split meant two different
    JSON shapes depending which endpoint failed -- a real problem for a
    client contract meant to be built against once. This normalizes both
    into the one envelope openapi/aggregator-client-api.yaml documents,
    matching what the webhook receiver's own _error() already returns.
    """
    if isinstance(exc.detail, dict) and "code" in exc.detail:
        code = str(exc.detail["code"])
        message = str(exc.detail.get("message", ""))
    else:
        code = _STATUS_CODES.get(exc.status_code, "ERROR")
        message = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
    return _error(exc.status_code, code, message)


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    first = exc.errors()[0] if exc.errors() else {}
    field = ".".join(str(p) for p in first.get("loc", []) if p != "body")
    message = f"{field}: {first.get('msg')}" if field else (first.get("msg") or "Validation failed")
    return _error(422, "VALIDATION_ERROR", message)


@app.post("/api/v1/webhooks/events")
async def receive_webhook_event(
    request: Request, session: AsyncSession = Depends(get_session)
) -> Response:
    raw_body = await request.body()

    # -- Pass 1: parse just enough to find the hotel + dedupe key ---------------
    try:
        envelope = _MinimalEnvelope.model_validate_json(raw_body)
    except ValidationError:
        return _error(400, "VALIDATION_ERROR", "Malformed webhook envelope")

    # Resolve the hotel by slug (event.hotel_id is the hotel slug, contract
    # 4.1.7) and its per-hotel webhook secret. Fall back to the dev shared
    # secret only until a Hotel row exists (keeps tests/dev runnable).
    hotel = await session.scalar(select(Hotel).where(Hotel.slug == envelope.hotel_id))
    secret = settings.dev_shared_webhook_secret
    if hotel is not None:
        cred = await session.get(HotelCredential, hotel.hotel_id)
        if cred is not None:
            secret = decrypt_secret(cred.webhook_secret_encrypted)

    # -- Pass 2: verify HMAC over the raw body with that secret -----------------
    try:
        verify_webhook_signature(
            secret=secret,
            raw_body=raw_body,
            signature_header=request.headers.get(settings.webhook_signature_header),
            timestamp_header=request.headers.get(settings.webhook_timestamp_header),
        )
    except WebhookAuthError as e:
        status = 408 if e.code == "REPLAY_WINDOW_EXCEEDED" else 401
        return _error(status, e.code, e.message)

    if hotel is None:
        # Verified via dev fallback but no registered hotel to attribute the
        # event to -- nothing to persist/dispatch. Accept (200) and log.
        logger.warning("Webhook for unregistered hotel slug %r accepted but not applied", envelope.hotel_id)
        return Response(status_code=200)

    # -- Validate the full discriminated-union body -----------------------------
    try:
        event = _webhook_validator.validate_json(raw_body)
    except ValidationError as e:
        # Contract section 4.3: "non-breaking additive changes (new optional
        # field, new event type) do not require a version bump; consumers
        # must ignore unknown fields/event types." A discriminator mismatch
        # (event_type not in WebhookEvent's Union -- Pydantic's
        # 'union_tag_invalid' error) means exactly that: a hotel_erp running
        # a newer version than this Aggregator sent an event type this build
        # doesn't know how to apply yet. That must be a no-op 200, not a
        # rejection -- otherwise this receiver breaks forward compatibility
        # for every future event type, not just today's. Any other
        # validation error (a *recognized* event_type with a malformed body)
        # is still a genuine 400.
        if any(err.get("type") == "union_tag_invalid" for err in e.errors()):
            logger.info("Ignoring unrecognized event_type %r from %r (forward-compat, contract 4.3)",
                        envelope.event_type, envelope.hotel_id)
            return Response(status_code=200)
        return _error(400, "VALIDATION_ERROR", "Webhook body failed schema validation")

    try:
        event_uuid = uuid.UUID(envelope.event_id)
    except (ValueError, TypeError):
        return _error(400, "VALIDATION_ERROR", "event_id is not a UUID")

    # -- Dedupe (NFR-B4): INSERT ... ON CONFLICT DO NOTHING ---------------------
    result = await session.execute(
        pg_insert(WebhookInbox)
        .values(event_id=event_uuid, hotel_id=hotel.hotel_id, event_type=envelope.event_type)
        .on_conflict_do_nothing(index_elements=[WebhookInbox.event_id])
    )
    await session.commit()
    if result.rowcount == 0:
        # Already processed this event_id -- idempotent no-op, still 200.
        return Response(status_code=200)

    # -- Dispatch + mark processed ----------------------------------------------
    try:
        await dispatch_event(session, hotel, event)
    except Exception:  # noqa: BLE001
        # Leave the inbox row without processed_at so a later reconcile / manual
        # replay can notice it; surface a 500 so Service A retries (at-least-once).
        logger.exception("Failed to dispatch webhook %s", envelope.event_id)
        return _error(500, "INTERNAL_ERROR", "Failed to apply event")

    inbox = await session.get(WebhookInbox, event_uuid)
    if inbox is not None:
        inbox.processed_at = datetime.now(timezone.utc)
        await session.commit()

    return Response(status_code=200)


# --- Routers ------------------------------------------------------------------
app.include_router(travelers.router)
app.include_router(bookings.router)
app.include_router(payments.router)
app.include_router(search.router)
app.include_router(reviews.router)
app.include_router(hotels_admin.router)
app.include_router(hotels_admin.public_router)
app.include_router(hotel_portal.router)
