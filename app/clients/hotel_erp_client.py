"""Async HTTP client for a single Hotel ERP (Service A), per
hotels/openapi/hotel-erp-api.yaml.

Wraps every endpoint, injects bearer auth from the hotel's decrypted API key,
auto-generates an Idempotency-Key (section 4.10) for the four mutating calls
when the caller does not supply one, and maps non-2xx responses to typed
exceptions carrying the contract section 4.9 error `code` so the orchestrator
can branch on e.g. ROOMS_UNAVAILABLE vs a generic failure.

Every Hotel ERP is treated as untrusted/unreliable (NFR-B2): a per-call timeout
is always applied and transport failures raise HotelERPUnavailable.
"""
from __future__ import annotations

import uuid
from typing import Any

import httpx

from app.config import settings
from app.db.models import HotelCredential
from app.security.crypto import decrypt_secret


# --- typed exceptions ----------------------------------------------------------

class HotelERPError(Exception):
    """Base for any non-2xx contract error. `code` is the section 4.9 code."""

    def __init__(self, *, status: int, code: str, message: str, details: Any = None, trace_id: str | None = None):
        self.status = status
        self.code = code
        self.message = message
        self.details = details or {}
        self.trace_id = trace_id
        super().__init__(f"{code}: {message}")


class HotelERPUnavailable(HotelERPError):
    """Transport-level failure (timeout, connection refused, 5xx). Retryable."""

    def __init__(self, message: str, status: int = 503):
        super().__init__(status=status, code="HOTEL_UNAVAILABLE", message=message)


class RoomsUnavailable(HotelERPError):
    pass


class HoldExpired(HotelERPError):
    pass


class IdempotencyKeyConflict(HotelERPError):
    pass


class NotFound(HotelERPError):
    pass


class CancellationNotAllowed(HotelERPError):
    pass


class Unauthorized(HotelERPError):
    pass


class RateLimited(HotelERPError):
    pass


_CODE_EXCEPTIONS: dict[str, type[HotelERPError]] = {
    "ROOMS_UNAVAILABLE": RoomsUnavailable,
    "HOLD_EXPIRED": HoldExpired,
    "IDEMPOTENCY_KEY_CONFLICT": IdempotencyKeyConflict,
    "NOT_FOUND": NotFound,
    "CANCELLATION_NOT_ALLOWED": CancellationNotAllowed,
    "UNAUTHORIZED": Unauthorized,
    "RATE_LIMITED": RateLimited,
}


class HotelERPClient:
    def __init__(self, api_base_url: str, api_key: str, *, timeout: float | None = None):
        self._base_url = api_base_url.rstrip("/")
        self._api_key = api_key
        self._timeout = timeout if timeout is not None else settings.hotel_call_timeout_seconds

    @classmethod
    def from_credential(cls, credential: HotelCredential, *, timeout: float | None = None) -> "HotelERPClient":
        return cls(credential.api_base_url, decrypt_secret(credential.api_key_encrypted), timeout=timeout)

    # -- internals --------------------------------------------------------------

    def _headers(self, idempotency_key: str | None) -> dict[str, str]:
        headers = {"Authorization": f"Bearer {self._api_key}", "Accept": "application/json"}
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        return headers

    def _raise_for_error(self, response: httpx.Response) -> None:
        if response.is_success:
            return
        code = "INTERNAL_ERROR"
        message = f"HTTP {response.status_code}"
        details: Any = {}
        trace_id = None
        try:
            body = response.json()
            err = body.get("error", {}) if isinstance(body, dict) else {}
            code = err.get("code", code)
            message = err.get("message", message)
            details = err.get("details", {})
            trace_id = err.get("trace_id")
        except Exception:  # noqa: BLE001 - error body may not be JSON
            pass
        if response.status_code >= 500:
            raise HotelERPUnavailable(message, status=response.status_code)
        exc_cls = _CODE_EXCEPTIONS.get(code, HotelERPError)
        raise exc_cls(status=response.status_code, code=code, message=message, details=details, trace_id=trace_id)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        json: dict | None = None,
        idempotency_key: str | None = None,
    ) -> Any:
        url = f"{self._base_url}{path}"
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.request(
                    method, url, params=params, json=json, headers=self._headers(idempotency_key)
                )
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise HotelERPUnavailable(f"{type(exc).__name__} calling {url}") from exc
        self._raise_for_error(response)
        if response.status_code == 204 or not response.content:
            return None
        return response.json()

    @staticmethod
    def _idem(key: str | None) -> str:
        return key or str(uuid.uuid4())

    # -- read endpoints ---------------------------------------------------------

    async def health(self) -> dict:
        return await self._request("GET", "/health")

    async def list_room_types(
        self, *, updated_since: str | None = None, cursor: str | None = None, limit: int = 50
    ) -> dict:
        params: dict[str, Any] = {"limit": limit}
        if updated_since:
            params["updated_since"] = updated_since
        if cursor:
            params["cursor"] = cursor
        return await self._request("GET", "/room-types", params=params)

    async def get_room_type(self, room_type_id: str) -> dict:
        return await self._request("GET", f"/room-types/{room_type_id}")

    async def get_availability(
        self,
        *,
        room_type_id: str,
        check_in: str,
        check_out: str,
        rooms: int = 1,
        adults: int = 1,
        children: int = 0,
    ) -> dict:
        params = {
            "room_type_id": room_type_id,
            "check_in": check_in,
            "check_out": check_out,
            "rooms": rooms,
            "adults": adults,
            "children": children,
        }
        return await self._request("GET", "/availability", params=params)

    async def get_reservation(self, reservation_id: str) -> dict:
        return await self._request("GET", f"/reservations/{reservation_id}")

    # -- mutating endpoints (Idempotency-Key required, auto-generated) -----------

    async def create_hold(self, body: dict, *, idempotency_key: str | None = None) -> dict:
        return await self._request(
            "POST", "/reservations/hold", json=body, idempotency_key=self._idem(idempotency_key)
        )

    async def confirm_hold(self, hold_id: str, body: dict, *, idempotency_key: str | None = None) -> dict:
        return await self._request(
            "POST", f"/reservations/{hold_id}/confirm", json=body, idempotency_key=self._idem(idempotency_key)
        )

    async def release_hold(self, hold_id: str, *, idempotency_key: str | None = None) -> dict:
        return await self._request(
            "POST", f"/reservations/{hold_id}/release", idempotency_key=self._idem(idempotency_key)
        )

    async def cancel_reservation(self, reservation_id: str, *, idempotency_key: str | None = None) -> dict:
        return await self._request(
            "POST", f"/reservations/{reservation_id}/cancel", idempotency_key=self._idem(idempotency_key)
        )
