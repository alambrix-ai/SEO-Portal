"""Review fixes for the app shell, settings and database start-up (G2c).

Nothing here touches a real database: the role check runs against a fake
connection, the migration against a recording stand-in for ``alembic.op``,
and the health endpoint against a fake engine connection.
"""
from __future__ import annotations

import importlib.util
import logging
from collections.abc import Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel, Field

from app.core.config import settings
from app.db import audit_guard
from tests.conftest import requires_db

VERSIONS = Path(__file__).resolve().parents[1] / "alembic" / "versions"


@asynccontextmanager
async def _noop_lifespan(app):  # noqa: ANN001, ANN202
    yield


class _Result:
    def __init__(self, row: tuple | None = None, scalar: Any = None) -> None:  # noqa: ANN401
        self._row = row
        self._scalar = scalar

    def first(self) -> tuple | None:
        return self._row

    def scalar_one(self) -> Any:  # noqa: ANN401
        return self._scalar


class _HealthConn:
    """Stands in for ``engine.connect()`` in the health endpoint."""

    def __enter__(self) -> _HealthConn:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def execute(self, *_: object, **__: object) -> _Result:
        return _Result((1,), 1)


def _app(monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    from app.db import session
    from app.main import create_app

    monkeypatch.setattr(session.engine, "connect", lambda: _HealthConn())
    app = create_app()
    app.router.lifespan_context = _noop_lifespan
    return app


# ── HTTPS redirect, host check and /health ─────────────────────────────────


@pytest.fixture
def locked_down(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "force_https", True)
    monkeypatch.setattr(settings, "edge_tls_terminated", False)
    monkeypatch.setattr(settings, "trusted_hosts", ["api.synaprank.ai"])
    monkeypatch.setattr(settings, "trusted_proxies", [])


def test_health_is_exempt_from_the_redirect_and_the_host_check(
    locked_down: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    with TestClient(_app(monkeypatch), base_url="http://localhost:8000") as client:
        response = client.get("/health", follow_redirects=False)
    assert response.status_code == 200
    assert response.json()["database"] == "up"


def test_other_paths_still_get_the_host_check(locked_down: None, monkeypatch: pytest.MonkeyPatch) -> None:
    with TestClient(_app(monkeypatch), base_url="http://evil.example") as client:
        response = client.get("/", follow_redirects=False)
        lookalike = client.get("/health/../", follow_redirects=False)
        health_suffix = client.get("/healthz", follow_redirects=False)
    assert response.status_code == 400
    assert lookalike.status_code == 400
    assert health_suffix.status_code == 400


def test_other_paths_are_still_redirected(locked_down: None, monkeypatch: pytest.MonkeyPatch) -> None:
    with TestClient(_app(monkeypatch), base_url="http://api.synaprank.ai") as client:
        response = client.get("/", follow_redirects=False)
    assert response.status_code == 308
    assert response.headers["location"].startswith("https://api.synaprank.ai")


def test_a_bad_host_is_refused_before_any_redirect(locked_down: None, monkeypatch: pytest.MonkeyPatch) -> None:
    # The host check is outermost, so a forged Host never becomes a
    # redirect target.
    with TestClient(_app(monkeypatch), base_url="http://evil.example") as client:
        response = client.get("/api/v1/anything", follow_redirects=False)
    assert response.status_code == 400
    assert "evil.example" not in response.headers.get("location", "")


def test_edge_tls_terminated_skips_the_redirect(locked_down: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "edge_tls_terminated", True)
    with TestClient(_app(monkeypatch), base_url="http://api.synaprank.ai") as client:
        response = client.get("/", follow_redirects=False)
    assert response.status_code == 200
    # HSTS is still sent.
    assert "max-age=" in response.headers["strict-transport-security"]


def test_a_429_carries_the_security_headers_and_request_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "rate_limit_enabled", True)
    monkeypatch.setattr(settings, "api_rate_limit_per_minute", 1)
    with TestClient(_app(monkeypatch)) as client:
        assert client.get("/").status_code == 200
        refused = client.get("/")
    assert refused.status_code == 429
    assert refused.headers["X-Content-Type-Options"] == "nosniff"
    assert refused.headers["Content-Security-Policy"].startswith("default-src 'none'")
    assert refused.headers["X-Request-ID"]


def test_middleware_order_is_host_then_redirect_outermost(locked_down: None, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.api.middleware import (
        BodySizeLimitMiddleware,
        HostCheckMiddleware,
        HttpsRedirectMiddleware,
        IpAllowlistMiddleware,
        RateLimitMiddleware,
        RequestContextMiddleware,
        SecurityHeadersMiddleware,
    )

    order = [m.cls for m in _app(monkeypatch).user_middleware]
    assert order[0] is HostCheckMiddleware
    assert order[1] is HttpsRedirectMiddleware
    assert order.index(SecurityHeadersMiddleware) < order.index(RateLimitMiddleware)
    assert order.index(RequestContextMiddleware) < order.index(RateLimitMiddleware)
    assert order.index(BodySizeLimitMiddleware) < order.index(IpAllowlistMiddleware) or (
        order[-1] is IpAllowlistMiddleware
    )
    assert order[-1] is IpAllowlistMiddleware


# ── Error handlers ─────────────────────────────────────────────────────────


class _Payload(BaseModel):
    name: str = Field(min_length=3, max_length=10)
    count: int = Field(ge=1, le=5)
    mode: str = Field(pattern=r"^(a|b)$")


def test_validation_errors_are_plain_english_and_do_not_echo_input(monkeypatch: pytest.MonkeyPatch) -> None:
    app = _app(monkeypatch)

    def echo(payload: _Payload) -> dict:
        return {}

    app.add_api_route("/__test__/validate", echo, methods=["POST"])
    with TestClient(app) as client:
        response = client.post(
            "/__test__/validate", json={"name": "<script>x</script>", "count": 99, "mode": "zzz"}
        )
    assert response.status_code == 422
    fields = response.json()["fields"]
    assert fields == {
        "name": "Please keep this to 10 characters or fewer.",
        "count": "Please enter 5 or less.",
        "mode": "This doesn't look right. Please check the format.",
    }
    assert "<script>" not in response.text
    assert "String should" not in response.text


def test_a_missing_field_reads_as_a_request_to_fill_it_in(monkeypatch: pytest.MonkeyPatch) -> None:
    app = _app(monkeypatch)

    def echo(payload: _Payload) -> dict:
        return {}

    app.add_api_route("/__test__/validate", echo, methods=["POST"])
    with TestClient(app) as client:
        response = client.post("/__test__/validate", json={"count": "lots", "mode": "a"})
    fields = response.json()["fields"]
    assert fields["name"] == "Please fill this in."
    assert fields["count"] == "Please enter a whole number."


def test_plain_validation_message_keeps_our_own_messages() -> None:
    from app.main import plain_validation_message

    assert plain_validation_message({"type": "colour", "msg": "Please use a colour like #1a2b3c"}) == (
        "Please use a colour like #1a2b3c"
    )
    assert plain_validation_message({"type": "value_error", "msg": "Value error, Pick a date in the future"}) == (
        "Pick a date in the future"
    )
    long = plain_validation_message({"type": "value_error", "msg": "Value error, " + "x" * 500})
    assert len(long) == 200
    # A built-in type with no mapping never shows pydantic's wording.
    assert plain_validation_message({"type": "frozen_field", "msg": "Field is frozen"}) == "Please check this value."
    # A templated message without a usable limit falls back too.
    assert plain_validation_message({"type": "too_long", "msg": "x", "ctx": {}}) == "Please check this value."


def test_unhandled_errors_log_no_traceback_or_raw_text(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    app = _app(monkeypatch)

    def boom() -> None:
        raise RuntimeError("password=hunter2hunter2 token=abc")

    app.add_api_route("/__test__/boom", boom, methods=["GET"])
    with caplog.at_level(logging.ERROR, logger="app.main"):
        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.get("/__test__/boom", headers={"X-Request-ID": "req-boom1"})
    assert response.status_code == 500
    assert "hunter2" not in response.text
    records = [r for r in caplog.records if r.name == "app.main"]
    assert records, "the failure must still be logged"
    for record in records:
        assert record.exc_info is None
        assert "hunter2hunter2" not in record.getMessage()
    assert any("req-boom1" in r.getMessage() and "RuntimeError" in r.getMessage() for r in records)


# ── Renderer health flag ───────────────────────────────────────────────────


class _Chromium:
    def __init__(self, launched: bool, recently_failed: bool = False) -> None:
        self.launched = launched
        self.recently_failed = recently_failed


def test_a_stale_degraded_flag_resets_once_chromium_is_back(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services.creative import render

    monkeypatch.setattr(settings, "creative_renderer", "chromium")
    monkeypatch.setattr(settings, "app_env", "production")
    monkeypatch.setattr(render, "_degraded", True)
    monkeypatch.setattr(render, "_chromium", _Chromium(launched=True))
    with TestClient(_app(monkeypatch)) as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["renderer"] == "chromium"
    assert render._degraded is False  # noqa: SLF001


@pytest.mark.parametrize("chromium", [None, _Chromium(launched=False), _Chromium(launched=True, recently_failed=True)])
def test_a_real_degraded_state_is_still_reported(monkeypatch: pytest.MonkeyPatch, chromium: _Chromium | None) -> None:
    from app.services.creative import render

    monkeypatch.setattr(settings, "creative_renderer", "chromium")
    monkeypatch.setattr(settings, "app_env", "production")
    monkeypatch.setattr(render, "_degraded", True)
    monkeypatch.setattr(render, "_chromium", chromium)
    with TestClient(_app(monkeypatch)) as client:
        response = client.get("/health")
    assert response.status_code == 503
    assert response.json()["renderer"] == "degraded"
    assert render._degraded is True  # noqa: SLF001


# ── Database role check (fake connection only) ─────────────────────────────


class _RoleConn:
    """Answers the role query with a fixed row; records what was asked."""

    def __init__(self, row: tuple | None) -> None:
        self.row = row
        self.statements: list[str] = []

    def execute(self, statement: Any, *_: object) -> _Result:  # noqa: ANN401
        self.statements.append(str(statement))
        return _Result(self.row)


@pytest.fixture
def production_db(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "app_env", "production")
    monkeypatch.setattr(settings, "allow_privileged_db_role", False)


def test_an_ordinary_app_role_passes(production_db: None) -> None:
    from app.db.session import check_db_role

    conn = _RoleConn((False, False, False))
    check_db_role(conn)  # type: ignore[arg-type]
    assert "rolbypassrls" in conn.statements[0]
    assert "pg_has_role" in conn.statements[0]


@pytest.mark.parametrize(
    ("row", "reason"),
    [
        ((True, False, True), "superuser"),
        ((False, True, False), "BYPASSRLS"),
        ((False, False, True), "owns audit_log or users"),
    ],
)
def test_a_privileged_role_is_refused_in_production(production_db: None, row: tuple, reason: str) -> None:
    from app.db.session import check_db_role

    with pytest.raises(RuntimeError, match=reason) as caught:
        check_db_role(_RoleConn(row))  # type: ignore[arg-type]
    assert "ALLOW_PRIVILEGED_DB_ROLE" in str(caught.value)


def test_an_uninspectable_role_is_refused(production_db: None) -> None:
    from app.db.session import check_db_role

    with pytest.raises(RuntimeError, match="could not be inspected"):
        check_db_role(_RoleConn(None))  # type: ignore[arg-type]


def test_the_escape_hatch_skips_the_check(production_db: None, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.db.session import check_db_role

    monkeypatch.setattr(settings, "allow_privileged_db_role", True)
    conn = _RoleConn((True, True, True))
    check_db_role(conn)  # type: ignore[arg-type]
    assert conn.statements == []


def test_the_check_is_skipped_outside_production(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.db.session import check_db_role

    monkeypatch.setattr(settings, "app_env", "development")
    conn = _RoleConn((True, True, True))
    check_db_role(conn)  # type: ignore[arg-type]
    assert conn.statements == []


def test_check_connection_runs_the_role_check_first(production_db: None, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.db import session

    class _Conn(_RoleConn):
        def __enter__(self) -> _Conn:
            return self

        def __exit__(self, *_: object) -> None:
            return None

    conn = _Conn((True, False, False))

    class _Engine:
        def connect(self) -> _Conn:
            return conn

    monkeypatch.setattr(session, "engine", _Engine())
    with pytest.raises(RuntimeError, match="superuser"):
        session.check_connection()
    assert len(conn.statements) == 1  # nothing else ran on a refused role


# ── Engine options ─────────────────────────────────────────────────────────


def test_the_engine_hides_bound_parameters_and_pins_sslmode(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.db import session

    captured: dict[str, Any] = {}

    def fake_create_engine(url: str, **kwargs: Any) -> object:  # noqa: ANN401
        captured.update(kwargs, url=url)
        return object()

    monkeypatch.setattr(session, "create_engine", fake_create_engine)
    monkeypatch.setattr(settings, "database_url", "postgresql+psycopg://u:p@db.internal/app_test")
    monkeypatch.setattr(settings, "postgres_sslmode", "require")
    session._build_engine()  # noqa: SLF001
    assert captured["hide_parameters"] is True
    assert captured["connect_args"]["sslmode"] == "require"


def test_the_dsn_sslmode_wins_over_postgres_sslmode(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.db import session

    captured: dict[str, Any] = {}
    monkeypatch.setattr(session, "create_engine", lambda url, **kw: captured.update(kw) or object())
    monkeypatch.setattr(settings, "database_url", "postgresql+psycopg://u:p@db/app_test?sslmode=verify-full")
    monkeypatch.setattr(settings, "postgres_sslmode", "prefer")
    session._build_engine()  # noqa: SLF001
    assert captured["connect_args"]["sslmode"] == "verify-full"


# ── Migration 0023 (recording op, no database) ─────────────────────────────


def _load_migration(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"m_{name}", VERSIONS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _RecordingOp:
    def __init__(self) -> None:
        self.executed: list[str] = []

    def execute(self, statement: str) -> None:
        self.executed.append(statement)


def _run(direction: str, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    module = _load_migration("0023_audit_truncate_guard")
    recorder = _RecordingOp()
    monkeypatch.setattr(module, "op", recorder)
    getattr(module, direction)()
    return recorder.executed


def test_migration_0023_chain_and_ids() -> None:
    module = _load_migration("0023_audit_truncate_guard")
    previous = _load_migration("0022_creative_tokens_nullable")
    assert module.revision == "0023_audit_truncate_guard"
    assert module.down_revision == previous.revision == "0022_creative_tokens_nullable"
    assert len(module.revision) <= 32
    assert [p.name for p in VERSIONS.glob("0023_*.py")] == ["0023_audit_truncate_guard.py"]


def test_migration_0023_installs_what_the_guard_declares(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _run("upgrade", monkeypatch) == audit_guard.truncate_install_statements()
    assert _run("downgrade", monkeypatch) == audit_guard.truncate_uninstall_statements()


def test_the_truncate_trigger_is_a_statement_trigger_with_no_escape_hatch(monkeypatch: pytest.MonkeyPatch) -> None:
    trigger = next(s for s in _run("upgrade", monkeypatch) if s.startswith("CREATE TRIGGER"))
    assert "BEFORE TRUNCATE ON audit_log" in trigger
    assert "FOR EACH STATEMENT" in trigger
    assert "EXECUTE FUNCTION audit_log_append_only()" in trigger
    assert "current_setting" not in trigger


def test_the_guard_installs_both_triggers_and_removes_truncate_first() -> None:
    executed: list[str] = []

    class _Conn:
        def execute(self, statement: Any) -> None:  # noqa: ANN401
            executed.append(str(statement))

    audit_guard.install(_Conn())  # type: ignore[arg-type]
    assert executed == [*audit_guard.install_statements(), *audit_guard.truncate_install_statements()]
    executed.clear()
    audit_guard.uninstall(_Conn())  # type: ignore[arg-type]
    assert executed[0] == "DROP TRIGGER IF EXISTS trg_audit_log_no_truncate ON audit_log"
    assert executed[-1] == audit_guard.uninstall_statements()[-1]


@requires_db
def test_truncate_on_the_audit_log_raises(clean_db, org) -> None:  # noqa: ANN001
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError

    try:
        with pytest.raises(DBAPIError, match="append-only"):
            with clean_db.begin_nested():
                clean_db.execute(text("TRUNCATE audit_log"))
    finally:
        clean_db.rollback()


# ── Rate limiter store ─────────────────────────────────────────────────────


class _Peer:
    def __init__(self, app: Any) -> None:  # noqa: ANN401
        self.app = app
        self.host = "198.51.100.7"

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:  # noqa: ANN401
        if scope["type"] == "http":
            scope = {**scope, "client": (self.host, 1234)}
        await self.app(scope, receive, send)


@pytest.fixture
def small_limiter(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[TestClient, _Peer, list]]:
    from app.api.middleware import RateLimitMiddleware

    monkeypatch.setattr(settings, "rate_limit_enabled", True)
    monkeypatch.setattr(settings, "api_rate_limit_per_minute", 2)
    monkeypatch.setattr(settings, "trusted_proxies", [])
    instances: list[RateLimitMiddleware] = []

    class _Tracked(RateLimitMiddleware):
        def __init__(self, app: Any) -> None:  # noqa: ANN401
            super().__init__(app, max_keys=3)
            instances.append(self)

    app = FastAPI()
    app.add_middleware(_Tracked)

    @app.get("/api/v1/things")
    def things() -> dict:
        return {}

    peer = _Peer(app)
    with TestClient(peer) as client:
        yield client, peer, instances


def test_the_limiter_holds_at_most_max_keys(small_limiter) -> None:  # noqa: ANN001
    client, peer, instances = small_limiter
    for n in range(20):
        peer.host = f"198.51.100.{n + 1}"
        assert client.get("/api/v1/things").status_code == 200
    assert len(instances[0]._hits) <= 3  # noqa: SLF001


def test_the_limiter_still_refuses_a_busy_address(small_limiter) -> None:  # noqa: ANN001
    client, peer, _ = small_limiter
    peer.host = "203.0.113.9"
    assert client.get("/api/v1/things").status_code == 200
    assert client.get("/api/v1/things").status_code == 200
    assert client.get("/api/v1/things").status_code == 429


def test_idle_keys_fall_off_and_recent_ones_stay(small_limiter, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ANN001
    from app.api import middleware

    client, peer, instances = small_limiter
    clock = [1000.0]
    monkeypatch.setattr(middleware.time, "monotonic", lambda: clock[0])
    peer.host = "198.51.100.50"
    client.get("/api/v1/things")
    clock[0] += 120  # past the window
    peer.host = "198.51.100.51"
    client.get("/api/v1/things")
    hits = instances[0]._hits  # noqa: SLF001
    assert any("198.51.100.51" in key for key in hits)
    assert not any("198.51.100.50" in key for key in hits)


def test_a_refused_request_creates_no_new_keys(small_limiter) -> None:  # noqa: ANN001
    client, peer, instances = small_limiter
    peer.host = "203.0.113.10"
    for _ in range(2):
        client.get("/api/v1/things")
    before = dict(instances[0]._hits)  # noqa: SLF001
    assert client.get("/api/v1/things").status_code == 429
    assert set(instances[0]._hits) == set(before)  # noqa: SLF001


def test_csp_reports_have_a_strict_per_address_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.api.middleware import RateLimitMiddleware

    monkeypatch.setattr(settings, "rate_limit_enabled", True)
    monkeypatch.setattr(settings, "api_rate_limit_per_minute", 1000)
    monkeypatch.setattr(settings, "trusted_proxies", [])
    monkeypatch.setattr(RateLimitMiddleware, "CSP_REPORT_PER_MINUTE", 2)
    app = FastAPI()
    app.add_middleware(RateLimitMiddleware)

    @app.post("/api/v1/csp-report")
    def report() -> dict:
        return {}

    @app.get("/api/v1/things")
    def things() -> dict:
        return {}

    peer = _Peer(app)
    with TestClient(peer) as client:
        assert client.post("/api/v1/csp-report").status_code == 200
        assert client.post("/api/v1/csp-report").status_code == 200
        assert client.post("/api/v1/csp-report").status_code == 429
        # The general budget is untouched; another address has its own.
        assert client.get("/api/v1/things").status_code == 200
        peer.host = "198.51.100.99"
        assert client.post("/api/v1/csp-report").status_code == 200


def test_csp_report_matching_is_exact() -> None:
    from app.api.middleware import RateLimitMiddleware

    match = RateLimitMiddleware._is_csp_report  # noqa: SLF001
    assert match("POST", "/api/v1/csp-report")
    assert match("POST", "/api/v1/csp-report/")
    assert not match("GET", "/api/v1/csp-report")
    assert not match("POST", "/api/v1/csp-report-x")
    assert not match("POST", "/csp-report")
