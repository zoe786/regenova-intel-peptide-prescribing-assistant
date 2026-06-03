from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from jose import jwt

from apps.api.config import Settings
from apps.api.security import resolve_chat_role


def _settings() -> Settings:
    return Settings(
        jwt_secret="test-jwt-secret-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
        admin_api_key="test-admin-key",
    )


def _bearer_for_role(role: str) -> HTTPAuthorizationCredentials:
    now = datetime.now(tz=timezone.utc)
    token = jwt.encode(
        {
            "sub": "user-123",
            "role": role,
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(hours=1)).timestamp()),
        },
        _settings().jwt_secret,
        algorithm="HS256",
    )
    return HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)


def test_resolve_chat_role_allows_missing_auth_in_non_production_callers() -> None:
    role = resolve_chat_role(settings=_settings(), bearer=None, x_admin_key=None)
    assert role is None


def test_resolve_chat_role_accepts_valid_admin_key() -> None:
    role = resolve_chat_role(
        settings=_settings(),
        bearer=None,
        x_admin_key="test-admin-key",
    )
    assert role == "admin"


def test_resolve_chat_role_rejects_invalid_admin_key() -> None:
    with pytest.raises(HTTPException) as exc:
        resolve_chat_role(
            settings=_settings(),
            bearer=None,
            x_admin_key="wrong",
        )
    assert exc.value.status_code == 401


def test_resolve_chat_role_accepts_valid_jwt_role() -> None:
    role = resolve_chat_role(
        settings=_settings(),
        bearer=_bearer_for_role("clinician"),
        x_admin_key=None,
    )
    assert role == "clinician"


def test_resolve_chat_role_rejects_jwt_with_invalid_role() -> None:
    with pytest.raises(HTTPException) as exc:
        resolve_chat_role(
            settings=_settings(),
            bearer=_bearer_for_role("anonymous"),
            x_admin_key=None,
        )
    assert exc.value.status_code == 403


def test_resolve_chat_role_rejects_invalid_jwt() -> None:
    with pytest.raises(HTTPException) as exc:
        resolve_chat_role(
            settings=_settings(),
            bearer=HTTPAuthorizationCredentials(
                scheme="Bearer",
                credentials="not-a-real-token",
            ),
            x_admin_key=None,
        )
    assert exc.value.status_code == 401
