"""Traveler authentication: bcrypt password hashing + JWT issue/verify (FR-B6).

Guest identity documents are never handled here or anywhere in this service
(NFR-B6) -- only a traveler's own marketplace account credentials.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import bcrypt
from jose import JWTError, jwt

from app.config import settings


def hash_password(plaintext: str) -> str:
    return bcrypt.hashpw(plaintext.encode(), bcrypt.gensalt()).decode()


def verify_password(plaintext: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plaintext.encode(), hashed.encode())
    except (ValueError, TypeError):
        return False


def create_access_token(traveler_id: uuid.UUID, email: str, *, scope: str = "traveler") -> str:
    """`scope` distinguishes a traveler token from a hotel-portal token
    (create_hotel_access_token below) so one can never be replayed as the
    other even though both are plain bearer JWTs signed with the same key --
    get_current_traveler / get_current_hotel_user each check it explicitly.
    """
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(traveler_id),
        "email": email,
        "scope": scope,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=settings.jwt_expiry_minutes)).timestamp()),
    }
    return jwt.encode(payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)


def create_hotel_access_token(hotel_user_id: uuid.UUID, email: str) -> str:
    return create_access_token(hotel_user_id, email, scope="hotel_portal")


def _decode_subject(token: str, *, expected_scope: str) -> uuid.UUID | None:
    try:
        payload = jwt.decode(token, settings.jwt_secret_key, algorithms=[settings.jwt_algorithm])
    except JWTError:
        return None
    # Tokens issued before `scope` existed have none; treat those as "traveler"
    # (the only scope that existed at the time) rather than rejecting them.
    if payload.get("scope", "traveler") != expected_scope:
        return None
    sub = payload.get("sub")
    if not sub:
        return None
    try:
        return uuid.UUID(sub)
    except (ValueError, TypeError):
        return None


def decode_access_token(token: str) -> uuid.UUID | None:
    """Return the traveler_id encoded in a valid traveler-scoped token, else None."""
    return _decode_subject(token, expected_scope="traveler")


def decode_hotel_access_token(token: str) -> uuid.UUID | None:
    """Return the hotel_user_id encoded in a valid hotel-portal-scoped token, else None."""
    return _decode_subject(token, expected_scope="hotel_portal")
