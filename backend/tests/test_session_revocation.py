"""Access tokens die with their session, not only at expiry.

An access token carries ``sid``, the refresh-token row it was minted with.
``get_current_user`` accepts it only while that row's family still has a live
refresh token: rotation keeps the session going, while logout, reuse
detection and sign-out-everywhere end it at once.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi.security import HTTPAuthorizationCredentials

from app.core.exceptions import AuthError
from app.core.security import create_access_token
from tests.conftest import requires_db


# ── Without a database ─────────────────────────────────────────────────────


@pytest.mark.parametrize("sid", [None, "", "not-a-uuid", "1; DROP TABLE refresh_tokens"])
def test_token_without_a_valid_sid_is_refused_before_any_query(sid: str | None) -> None:
    from app.api.deps import get_current_user

    token, _ = create_access_token(
        user_id="5b0c3b5e-2f0e-4e51-9d1b-1a2b3c4d5e6f",
        tenant_id="0f7e3c1a-6a8b-4c9d-8e7f-112233445566",
        role="admin",
        session_id=sid,  # type: ignore[arg-type]
    )
    db = MagicMock()
    with pytest.raises(AuthError):
        get_current_user(
            db, HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)
        )
    db.execute.assert_not_called()
    db.get.assert_not_called()


def test_dead_session_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """The liveness check runs, and a dead family means 401."""
    from app.api import deps

    user = SimpleNamespace(
        is_active=True, tenant_id="0f7e3c1a-6a8b-4c9d-8e7f-112233445566", role="admin"
    )
    db = MagicMock()
    db.get.return_value = user
    db.execute.return_value.first.return_value = None
    monkeypatch.setattr(deps, "bind_tenant", lambda *_: None)

    token, _ = create_access_token(
        user_id="5b0c3b5e-2f0e-4e51-9d1b-1a2b3c4d5e6f",
        tenant_id=user.tenant_id,
        role="admin",
        session_id="9a8b7c6d-1111-2222-3333-444455556666",
    )
    with pytest.raises(AuthError) as caught:
        deps.get_current_user(
            db, HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)
        )
    assert "signed out" in caught.value.message
    db.execute.assert_called_once()


# ── Against the database ───────────────────────────────────────────────────


def _register_and_sign_in(client) -> dict:  # noqa: ANN001
    from tests.test_api_flows import register, sign_in

    register(client)
    login = sign_in(client)
    assert login.status_code == 200, login.text
    return login.json()["tokens"]


def _me(client, access: str) -> int:  # noqa: ANN001
    return client.get(
        "/api/v1/auth/me", headers={"Authorization": f"Bearer {access}"}
    ).status_code


@requires_db
def test_token_stops_working_after_logout(client, clean_db) -> None:  # noqa: ANN001
    tokens = _register_and_sign_in(client)
    assert _me(client, tokens["access_token"]) == 200

    out = client.post("/api/v1/auth/logout", json={"refresh_token": tokens["refresh_token"]})
    assert out.status_code == 200

    assert _me(client, tokens["access_token"]) == 401


@requires_db
def test_token_keeps_working_after_rotation(client, clean_db) -> None:  # noqa: ANN001
    tokens = _register_and_sign_in(client)
    rotated = client.post("/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert rotated.status_code == 200

    # The old access token's row is spent, but its family lives on.
    assert _me(client, tokens["access_token"]) == 200
    assert _me(client, rotated.json()["tokens"]["access_token"]) == 200


@requires_db
def test_reuse_detection_kills_every_access_token_in_the_family(client, clean_db) -> None:  # noqa: ANN001
    tokens = _register_and_sign_in(client)
    rotated = client.post("/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert rotated.status_code == 200

    reused = client.post("/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert reused.status_code == 401

    assert _me(client, tokens["access_token"]) == 401
    assert _me(client, rotated.json()["tokens"]["access_token"]) == 401


@requires_db
def test_sign_out_everywhere_ends_the_calling_session(client, clean_db) -> None:  # noqa: ANN001
    tokens = _register_and_sign_in(client)
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}
    assert client.post("/api/v1/auth/sign-out-everywhere", headers=headers).status_code == 200
    assert _me(client, tokens["access_token"]) == 401


@requires_db
def test_logging_out_one_session_leaves_another(client, clean_db) -> None:  # noqa: ANN001
    from tests.test_api_flows import sign_in

    first = _register_and_sign_in(client)
    second = sign_in(client).json()["tokens"]

    client.post("/api/v1/auth/logout", json={"refresh_token": first["refresh_token"]})
    assert _me(client, first["access_token"]) == 401
    assert _me(client, second["access_token"]) == 200


@requires_db
def test_validly_signed_token_with_an_unknown_sid_is_refused(client, clean_db) -> None:  # noqa: ANN001
    import jwt

    tokens = _register_and_sign_in(client)
    claims = jwt.decode(tokens["access_token"], options={"verify_signature": False})
    forged, _ = create_access_token(
        user_id=claims["sub"],
        tenant_id=claims["tid"],
        role=claims["role"],
        session_id="9a8b7c6d-1111-2222-3333-444455556666",
    )
    assert _me(client, forged) == 401
