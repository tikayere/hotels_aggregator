"""Shared FastAPI dependencies: DB session + authenticated traveler."""
from __future__ import annotations

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

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
