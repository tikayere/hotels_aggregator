"""Shared FastAPI dependencies: DB session + authenticated traveler/admin."""
from __future__ import annotations

import hmac

from fastapi import Depends, Header, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.models import TravelerAccount
from app.db.session import get_session
from app.security.auth import decode_access_token

bearer_scheme = HTTPBearer(auto_error=False)


async def get_current_traveler(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    session: AsyncSession = Depends(get_session),
) -> TravelerAccount:
    if credentials is None or not credentials.credentials:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Missing bearer token")
    traveler_id = decode_access_token(credentials.credentials)
    if traveler_id is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired token")
    traveler = await session.get(TravelerAccount, traveler_id)
    if traveler is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Unknown traveler")
    return traveler


async def require_admin(x_admin_api_key: str | None = Header(default=None)) -> None:
    """Guards the Hotel Registry admin routes (FR-B1) -- these create/rotate a
    hotel's stored API key and webhook secret, so an unauthenticated caller
    here can fully take over a hotel's sync relationship with this platform.
    A dedicated header (not the traveler Authorization: Bearer scheme) so an
    admin key and a traveler JWT can never be confused for one another.
    """
    if not x_admin_api_key or not hmac.compare_digest(x_admin_api_key, settings.admin_api_key):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Missing or invalid admin API key")
