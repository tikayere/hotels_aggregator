"""HMAC-SHA256 verification for inbound webhooks (contract section 4.2).

Used by the webhook receiver (POST /api/v1/webhooks/events, section 4.6)
before the request body is trusted. Call verify_webhook_signature() first,
in the route handler, before touching the parsed body at all. Logic is
identical to the sibling bus project's version; only the header names
differ (X-Hotel-* here vs X-Bus-* there).
"""
from __future__ import annotations

import hashlib
import hmac
import time

REPLAY_WINDOW_SECONDS = 300


class WebhookAuthError(Exception):
    """code is one of the contract's section 4.6/4.9 error codes; the FastAPI
    route maps UNAUTHORIZED -> 401 and REPLAY_WINDOW_EXCEEDED -> 408.
    """

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(message)


def verify_webhook_signature(
    secret: str,
    raw_body: bytes,
    signature_header: str | None,
    timestamp_header: str | None,
) -> None:
    """Raises WebhookAuthError on any failure. Returns None on success."""
    if not signature_header or not signature_header.startswith("sha256="):
        raise WebhookAuthError("UNAUTHORIZED", "Missing or malformed signature header")

    try:
        timestamp = int(timestamp_header)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise WebhookAuthError("UNAUTHORIZED", "Missing or malformed timestamp header")

    if abs(int(time.time()) - timestamp) > REPLAY_WINDOW_SECONDS:
        raise WebhookAuthError("REPLAY_WINDOW_EXCEEDED", "Timestamp outside the replay window")

    expected_digest = hmac.new(secret.encode(), f"{timestamp}.".encode() + raw_body, hashlib.sha256).hexdigest()
    provided_digest = signature_header.removeprefix("sha256=")

    # Constant-time comparison -- a naive `==` here would leak timing
    # information an attacker could exploit to forge a valid signature
    # byte-by-byte against a live endpoint.
    if not hmac.compare_digest(expected_digest, provided_digest):
        raise WebhookAuthError("UNAUTHORIZED", "Signature does not match")
