"""What an error response says, and what the log keeps.

Vendor wording ("403 Forbidden", "model not found") is softened into plain
English, but only for errors that came from a vendor. Messages the product
wrote itself — "Workspace not found", the role-access toasts — used to be run
through the same softener and came out as a sentence about a missing service
or model. They now reach the client as written.

None of these tests need a database: the app is built with its lifespan
skipped and the health probe's connection is replaced.
"""
from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.exceptions import (
    AppError,
    ConnectorConfigError,
    ConnectorError,
    ForbiddenError,
    NotFoundError,
)
from app.core.user_messages import (
    bounded_redacted,
    is_vendor_origin,
    public_error_message,
)

SECRET = "sk-live-THISISASECRET0123456789"


@asynccontextmanager
async def _noop_lifespan(app):  # noqa: ANN001, ANN202
    yield


def _app_raising(exc: BaseException) -> FastAPI:
    from app.main import create_app

    app = create_app()
    app.router.lifespan_context = _noop_lifespan

    def boom() -> None:
        raise exc

    app.add_api_route("/__test__/boom", boom, methods=["GET"])
    return app


def _detail(exc: BaseException) -> tuple[int, dict]:
    with TestClient(_app_raising(exc), raise_server_exceptions=False) as client:
        response = client.get("/__test__/boom")
    return response.status_code, response.json()


def _rbac_messages() -> tuple[str, str, str]:
    """The three ForbiddenError messages raised by the real RBAC helpers."""
    from app.api.deps import CurrentUser
    from app.core.rbac import Access, Module, Role, access_for

    pair = next(
        ((role, module) for role in Role for module in Module
         if access_for(role, module) is Access.NONE),
        None,
    )
    view_only = next(
        ((role, module) for role in Role for module in Module
         if access_for(role, module) is Access.VIEW),
        None,
    )
    assert pair and view_only, "RBAC matrix changed; pick new fixtures"

    def raised(user: CurrentUser, module: Module, write: bool) -> str:
        with pytest.raises(ForbiddenError) as caught:
            (user.require_write if write else user.require_view)(module)
        return caught.value.message

    def make(role: Role, enabled: frozenset[str] | None) -> CurrentUser:
        return CurrentUser(
            SimpleNamespace(role=role.value),  # type: ignore[arg-type]
            SimpleNamespace(id="org"),  # type: ignore[arg-type]
            {},
            enabled_modules=enabled,
        )

    no_access = raised(make(pair[0], None), pair[1], write=False)
    disabled = raised(make(pair[0], frozenset()), pair[1], write=False)
    read_only = raised(make(view_only[0], None), view_only[1], write=True)
    return no_access, disabled, read_only


# ── Domain messages reach the client as written ─────────────────────────────


def test_workspace_not_found_is_shown_verbatim() -> None:
    status, body = _detail(NotFoundError("Workspace not found"))
    assert status == 404
    assert body == {"code": "not_found", "detail": "Workspace not found", "fields": {}}


def test_image_not_found_is_shown_verbatim() -> None:
    assert _detail(NotFoundError("Image not found"))[1]["detail"] == "Image not found"


def test_rbac_permission_messages_are_shown_verbatim() -> None:
    no_access, disabled, read_only = _rbac_messages()
    # The guard that matters: this one contains "permission", which the old
    # handler rewrote into "Access was refused. Check that this account...".
    assert "permission" in no_access
    for message in (no_access, disabled, read_only):
        status, body = _detail(ForbiddenError(message))
        assert status == 403
        assert body["detail"] == message


def test_a_domain_message_mentioning_403_is_not_rewritten() -> None:
    message = "Only 403 pages were checked this week."
    assert _detail(AppError(message))[1]["detail"] == message


# ── Vendor wording is still softened ────────────────────────────────────────


def test_a_vendor_error_is_still_softened() -> None:
    status, body = _detail(ConnectorError("Acme API returned 403 Forbidden"))
    assert status == 502
    assert body["code"] == "connector_error"
    assert body["detail"] == "Access was refused. Check that this account has permission."


def test_assistant_upstream_errors_are_softened_by_code() -> None:
    from app.services.assistant_errors import AssistantUpstreamError

    exc = AssistantUpstreamError("upstream said: model claude-x not found")
    assert is_vendor_origin(exc)
    assert _detail(exc)[1]["detail"].startswith("That service or model could not be found")


def test_llm_errors_count_as_vendor_origin() -> None:
    from app.llm.base import LLMError

    assert is_vendor_origin(LLMError("429 rate limit"))
    assert public_error_message(LLMError("429 rate limit")).startswith(
        "This service is temporarily limiting requests"
    )
    assert is_vendor_origin(ConnectorConfigError("x"))
    assert not is_vendor_origin(NotFoundError("x"))
    assert not is_vendor_origin(ValueError("x"))


# ── Abuse cases: secrets and stack-shaped text never reach the client ───────


@pytest.mark.parametrize(
    "exc",
    [
        NotFoundError(f"Workspace {SECRET} not found"),
        ConnectorError(f"Bad key {SECRET}"),
        AppError("Traceback (most recent call last): ValueError: boom"),
        AppError('{"error": {"message": "raw vendor body"}}'),
    ],
)
def test_secret_or_stack_shaped_messages_are_replaced(exc: AppError) -> None:
    body = _detail(exc)[1]
    assert SECRET not in body["detail"]
    assert "Traceback" not in body["detail"]
    assert "raw vendor body" not in body["detail"]
    assert body["detail"]


def test_an_unexpected_error_is_generic_and_carries_the_request_id() -> None:
    with TestClient(_app_raising(RuntimeError(SECRET)), raise_server_exceptions=False) as client:
        response = client.get("/__test__/boom", headers={"X-Request-ID": "req-abc123"})
    assert response.status_code == 500
    body = response.json()
    assert SECRET not in response.text
    assert body["fields"] == {"request_id": "req-abc123"}


def test_public_error_message_for_plain_exceptions() -> None:
    assert public_error_message(ValueError("Pick a date in the future.")) == (
        "Pick a date in the future."
    )
    assert public_error_message(RuntimeError(SECRET), fallback="Nope.") == "Nope."
    assert public_error_message(RuntimeError("x" * 400), fallback="Nope.") == "Nope."
    assert public_error_message(RuntimeError(""), fallback="Nope.") == "Nope."


# ── Log helpers ─────────────────────────────────────────────────────────────


def test_bounded_redacted_masks_and_caps() -> None:
    raw = (
        f"POST https://user:hunter2@api.example.com/v1?token=abc123 failed "
        f"key={SECRET} Authorization: Bearer abcdefghijklmnopqrstuvwxyz "
        f"for jane@example.com\n\n" + "y" * 1000
    )
    out = bounded_redacted(raw, limit=200)
    assert len(out) <= 200
    assert "\n" not in out
    for leaked in (SECRET, "hunter2", "abc123", "abcdefghijklmnop", "jane@example.com"):
        assert leaked not in out
    assert "[redacted]" in out


def test_log_failure_writes_no_raw_secret(caplog: pytest.LogCaptureFixture) -> None:
    from app.core.logging import log_failure

    logger = logging.getLogger("tests.error_hygiene")
    with caplog.at_level(logging.WARNING, logger="tests.error_hygiene"):
        log_failure(logger, "Push failed", RuntimeError(f"denied {SECRET}"), org_id="org-1")
    text = caplog.text
    assert SECRET not in text
    assert "Push failed: RuntimeError" in text
    assert "org_id=org-1" in text
    assert "[h=" in text
    assert caplog.records[0].exc_info is None


def test_log_records_carry_the_request_id() -> None:
    from app.core.logging import (
        _RequestIdFilter,
        bind_request_id,
        current_request_id,
        reset_request_id,
    )

    record = logging.LogRecord("t", logging.INFO, __file__, 1, "m", None, None)
    token = bind_request_id("req-42")
    try:
        assert current_request_id() == "req-42"
        _RequestIdFilter().filter(record)
    finally:
        reset_request_id(token)
    assert record.request_id == "req-42"
    assert current_request_id() == "-"


def test_the_request_id_is_bound_while_a_request_is_served() -> None:
    from app.core.logging import current_request_id
    from app.main import create_app

    app = create_app()
    app.router.lifespan_context = _noop_lifespan
    seen: list[str] = []

    async def probe() -> dict:
        seen.append(current_request_id())
        return {}

    app.add_api_route("/__test__/rid", probe, methods=["GET"])
    with TestClient(app) as client:
        client.get("/__test__/rid", headers={"X-Request-ID": "req-bound"})
    assert seen == ["req-bound"]


# ── /health ─────────────────────────────────────────────────────────────────


class _Conn:
    def __enter__(self) -> _Conn:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def execute(self, *_: object) -> None:
        return None


@pytest.fixture
def health_client() -> Iterator[TestClient]:
    from app.main import create_app

    app = create_app()
    app.router.lifespan_context = _noop_lifespan
    with TestClient(app) as client:
        yield client


def test_health_is_ok_and_reports_the_fleet(
    health_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.agents.base.registry import agent_slugs
    from app.connectors.base.registry import connector_slugs
    from app.db import session

    monkeypatch.setattr(session.engine, "connect", lambda: _Conn())
    response = health_client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["database"] == "up"
    assert body["agents_registered"] == len(agent_slugs())
    assert body["connectors_registered"] == len(connector_slugs())


def test_health_is_503_when_the_database_is_down(
    health_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.db import session

    def refuse() -> _Conn:
        raise ConnectionError(f"could not connect: password={SECRET}")

    monkeypatch.setattr(session.engine, "connect", refuse)
    response = health_client.get("/health")
    assert response.status_code == 503
    assert response.json()["status"] == "degraded"
    assert response.json()["database"] == "down"
    assert SECRET not in response.text


def test_health_says_nothing_about_the_build(
    health_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.core.config import settings
    from app.db import session
    from app.main import VERSION

    monkeypatch.setattr(session.engine, "connect", lambda: _Conn())
    response = health_client.get("/health")
    body = response.json()
    for key in ("app", "environment", "version", "model"):
        assert key not in body
    assert VERSION not in response.text
    assert settings.app_name not in response.text
