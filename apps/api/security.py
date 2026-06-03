"""Authentication helpers for API routes."""

from __future__ import annotations

import logging
from typing import Annotated, Literal

from fastapi import Depends, Header, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt

from apps.api.config import Settings, get_settings

logger = logging.getLogger(__name__)

Role = Literal["researcher", "clinician", "admin"]
_bearer = HTTPBearer(auto_error=False)


def resolve_chat_role(
    settings: Settings,
    bearer: HTTPAuthorizationCredentials | None,
    x_admin_key: str | None,
) -> Role | None:
    """Resolve role from admin key or JWT bearer token."""
    if x_admin_key:
        if x_admin_key == settings.admin_api_key:
            return "admin"
        raise HTTPException(
            status_code=401,
            detail={"type": "auth_failed", "title": "Invalid admin API key"},
        )

    if not bearer:
        return None

    if bearer.scheme.lower() != "bearer":
        raise HTTPException(
            status_code=401,
            detail={"type": "auth_failed", "title": "Unsupported auth scheme"},
        )

    try:
        payload = jwt.decode(
            bearer.credentials,
            settings.jwt_secret,
            algorithms=["HS256"],
            options={"verify_exp": True},
        )
    except JWTError:
        raise HTTPException(
            status_code=401,
            detail={"type": "auth_failed", "title": "Invalid or expired bearer token"},
        ) from None

    role = payload.get("role")
    if role not in ("researcher", "clinician", "admin"):
        raise HTTPException(
            status_code=403,
            detail={"type": "access_denied", "title": "JWT role claim is missing or invalid"},
        )
    return role


def get_chat_role(
    bearer: Annotated[
        HTTPAuthorizationCredentials | None,
        Depends(_bearer),
    ] = None,
    x_admin_key: Annotated[str | None, Header(alias="X-Admin-Key")] = None,
    settings: Settings = Depends(get_settings),
) -> Role | None:
    """Dependency that returns authenticated role for chat requests."""
    return resolve_chat_role(settings=settings, bearer=bearer, x_admin_key=x_admin_key)
