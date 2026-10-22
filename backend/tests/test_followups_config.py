"""Step 8.2 follow-ups: new settings and the code that reads them, URL
credentials in logs, the health probe's renderer, the database bootstrap's
passwords and the PyYAML pin.

No test touches the network or a database: DNS and the socket layer under
the egress guard are stubbed, and the app is built with its lifespan skipped.
"""
from __future__ import annotations

import json
import logging
import pathlib
import re
import socket
import sys
import typing
from collections.abc import Iterator
from contextlib import asynccontextmanager
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.core.config import Settings, settings
from app.core.exceptions import ConnectorConfigError
from app.core.user_messages import bounded_redacted, public_error_message

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_connector_egress import Raw, _connector, _dns, _http  # noqa: E402

BACKEND = pathlib.Path(__file__).resolve().parents[1]


def _settings(**values: typing.Any) -> Settings:  # noqa: ANN401
    values.setdefault("app_env", "development")
    return Settings(_env_file=None, **values)


@pytest.fixture(autouse=True)
def _no_real_sockets(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("a real socket was opened")

    monkeypatch.setattr(socket, "create_connection", refuse)


# ── Defaults ────────────────────────────────────────────────────────────────
def test_new_setting_defaults() -> None:
    s = _settings()
    assert s.dev_log_sign_in_codes is False
    assert s.metrics_backfill_days == 90
    assert s.redis_url == ""
    assert s.published_link_days == 365
    assert s.social_daily_post_cap == 25
    assert s.connector_private_hosts_allowed == []
    assert s.max_request_body_bytes == 1_048_576


# ── DEV_LOG_SIGN_IN_CODES ───────────────────────────────────────────────────
def test_dev_log_sign_in_codes_is_read_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEV_LOG_SIGN_IN_CODES", "true")
    assert _settings().dev_log_sign_in_codes is True


@pytest.mark.parametrize("env", ["test", "staging"])
def test_dev_log_sign_in_codes_is_refused_outside_development(env: str) -> None:
    with pytest.raises(ValidationError) as caught:
        _settings(app_env=env, dev_log_sign_in_codes=True)
    assert "DEV_LOG_SIGN_IN_CODES" in str(caught.value)


def test_dev_log_sign_in_codes_is_refused_in_an_otherwise_valid_production() -> None:
    from test_settings_guardrails import _production

    _production()  # the baseline starts
    with pytest.raises(ValidationError) as caught:
        _production(dev_log_sign_in_codes=True)
    assert "DEV_LOG_SIGN_IN_CODES" in str(caught.value)


def test_email_reads_the_setting_directly(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services import email as email_service

    monkeypatch.setattr(settings, "app_env", "development")
    monkeypatch.setattr(settings, "dev_log_sign_in_codes", True)
    assert email_service._dev_code_logging() is True
    monkeypatch.setattr(settings, "dev_log_sign_in_codes", False)
    assert email_service._dev_code_logging() is False
    # Even if switched on at runtime, nothing is logged outside development.
    monkeypatch.setattr(settings, "dev_log_sign_in_codes", True)
    monkeypatch.setattr(settings, "app_env", "test")
    assert email_service._dev_code_logging() is False


def test_email_has_no_getattr_fallback_for_the_flag() -> None:
    source = (BACKEND / "app" / "services" / "email.py").read_text(encoding="utf-8")
    assert 'getattr(settings, "dev_log_sign_in_codes"' not in source


# ── REDIS_URL ───────────────────────────────────────────────────────────────
@pytest.mark.parametrize("url", ["redis://redis:6379/2", "rediss://:pw@cache.example:6380/0", ""])
def test_redis_url_accepts_redis_schemes(url: str) -> None:
    assert _settings(redis_url=url).redis_url == url


@pytest.mark.parametrize("url", ["http://redis:6379", "redis:6379", "postgresql://u:p@h/db"])
def test_redis_url_refuses_other_schemes(url: str) -> None:
    with pytest.raises(ValidationError):
        _settings(redis_url=url)


def test_redis_url_is_masked_in_repr() -> None:
    s = _settings(redis_url="redis://:topsecretpw@cache:6379/0")
    assert "topsecretpw" not in repr(s)


# ── Bounded numbers ─────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "field, bad",
    [
        ("published_link_days", 0),
        ("published_link_days", 5 * 365 + 1),
        ("social_daily_post_cap", 0),
        ("social_daily_post_cap", 501),
        ("metrics_backfill_days", 367),
        ("max_request_body_bytes", 10),
    ],
)
def test_bounded_settings_refuse_out_of_range(field: str, bad: int) -> None:
    with pytest.raises(ValidationError):
        _settings(**{field: bad})


# ── PUBLISHED_LINK_DAYS ─────────────────────────────────────────────────────
def _expires(url: str) -> int:
    return int(urlsplit(url).path.rstrip("/").split("/")[-2])


def test_media_links_use_the_published_link_days_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    import time

    from app.models.content import GeneratedImage
    from app.services import media_links

    monkeypatch.setattr(settings, "published_link_days", 10)
    image = GeneratedImage(id="img-1", tenant_id="t-1", data=b"x")
    expires = _expires(media_links.published_image_url(image))
    assert abs(expires - (time.time() + 10 * 86_400)) < 120
    # An explicit value still wins, within the module's bounds.
    explicit = _expires(media_links.published_image_url(image, days=2))
    assert abs(explicit - (time.time() + 2 * 86_400)) < 120


# ── CONNECTOR_PRIVATE_HOSTS_ALLOWED ─────────────────────────────────────────
def test_private_hosts_parse_hosts_and_cidrs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CONNECTOR_PRIVATE_HOSTS_ALLOWED", " WP.Intranet.Example. , 10.20.0.1/16,fd00::/8")
    assert _settings().connector_private_hosts_allowed == [
        "wp.intranet.example",
        "10.20.0.0/16",
        "fd00::/8",
    ]


@pytest.mark.parametrize("bad", ["0.0.0.0/0", "::/0", "http://wp.example", "*.intranet", "a b"])
def test_private_hosts_refuse_catch_alls_and_junk(bad: str) -> None:
    with pytest.raises(ValidationError):
        _settings(connector_private_hosts_allowed=bad)


def test_a_listed_private_cidr_is_reachable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "connector_private_hosts_allowed", ["10.1.2.0/24"])
    _dns(monkeypatch, {"shop.internal.example": ["10.1.2.3"]})
    views = [{"id": 1}]
    raw = Raw(_http(200, json.dumps(views).encode(), {"Content-Type": "application/json"}))
    connector = _connector("magento", raw, baseUrl="https://shop.internal.example", accessToken="t" * 12)
    assert connector.request("GET", "/rest/V1/store/storeViews") == views
    assert raw.connects[0]["host"] == "10.1.2.3"


def test_a_listed_private_host_is_reachable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "connector_private_hosts_allowed", ["wp.intranet.example"])
    _dns(monkeypatch, {"wp.intranet.example": ["192.168.5.5"]})
    raw = Raw(_http(200, b'{"name": "Intranet"}'))
    connector = _connector(
        "wordpress", raw, siteUrl="https://wp.intranet.example", username="u", applicationPassword="p" * 12
    )
    assert connector.request("GET", "/wp-json") == {"name": "Intranet"}


def test_an_unlisted_private_address_is_still_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "connector_private_hosts_allowed", ["10.1.2.0/24"])
    _dns(monkeypatch, {"other.internal.example": ["10.9.9.9"]})
    raw = Raw()
    connector = _connector("magento", raw, baseUrl="https://other.internal.example", accessToken="t" * 12)
    with pytest.raises(ConnectorConfigError) as caught:
        connector.request("GET", "/rest/V1/store/storeViews")
    assert caught.value.details["reason"] == "blocked"
    assert raw.connects == []


def test_metadata_stays_blocked_even_when_its_range_is_listed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "connector_private_hosts_allowed", ["169.254.0.0/16", "metadata.internal"])
    _dns(monkeypatch, {"metadata.internal": ["169.254.169.254"]})
    raw = Raw()
    connector = _connector("custom_api", raw, webhookUrl="https://metadata.internal", secret="s" * 16)
    with pytest.raises(ConnectorConfigError) as caught:
        connector.request("GET", "/latest/meta-data/")
    assert caught.value.details["reason"] == "blocked"
    assert raw.connects == []


def test_vendor_fixed_connectors_ignore_the_list(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core import egress

    monkeypatch.setattr(settings, "connector_private_hosts_allowed", ["10.0.0.0/8"])
    connector = _connector("openai", Raw())
    assert connector.egress_guarded is False
    connector._http()
    assert connector._egress_allow is egress.NO_ALLOWLIST


# ── bounded_redacted: URL credentials ───────────────────────────────────────
@pytest.mark.parametrize(
    "text, secret, kept",
    [
        ("connect failed redis://:s3cretPW@cache.local:6379/0 refused", "s3cretPW", "cache.local:6379"),
        ("could not open postgresql://appuser:dbpass99@db.host/automarket", "dbpass99", "db.host/automarket"),
        ("dsn=postgresql+psycopg://u:p@ss@h:5432/db", "p@ss", "h:5432/db"),
        ("fetch https://ghp_tokenvalue123@github.com/x", "ghp_tokenvalue123", "github.com/x"),
        ("amqp://guest:guestpw@rabbit//", "guestpw", "rabbit"),
    ],
)
def test_bounded_redacted_masks_url_credentials(text: str, secret: str, kept: str) -> None:
    out = bounded_redacted(text)
    assert secret not in out
    assert "[redacted]@" in out
    assert kept in out


def test_bounded_redacted_masks_the_short_forms_from_the_follow_up() -> None:
    assert bounded_redacted("redis://:pw@host") == "redis://[redacted]@host"
    assert bounded_redacted("postgresql://u:p@h/db") == "postgresql://[redacted]@h/db"


def test_bounded_redacted_leaves_plain_urls_alone() -> None:
    assert bounded_redacted("GET https://example.com/a/b failed") == "GET https://example.com/a/b failed"


def test_a_password_only_url_is_not_shown_to_users() -> None:
    assert "pw123456" not in public_error_message(ValueError("redis://:pw123456@cache:6379"))


# ── main.py: logging and /health ────────────────────────────────────────────
@asynccontextmanager
async def _noop_lifespan(app):  # noqa: ANN001, ANN202
    yield


class _Conn:
    def __enter__(self) -> _Conn:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def execute(self, *_: object) -> None:
        return None


@pytest.fixture
def app_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    from app.db import session
    from app.main import create_app

    monkeypatch.setattr(session.engine, "connect", lambda: _Conn())
    app = create_app()
    app.router.lifespan_context = _noop_lifespan
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client


def _renderer(monkeypatch: pytest.MonkeyPatch, state: str) -> None:
    from app.services.creative import render

    monkeypatch.setattr(render, "renderer_status", lambda: state)


def test_health_reports_the_renderer(app_client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _renderer(monkeypatch, "chromium")
    response = app_client.get("/health")
    assert response.status_code == 200
    assert response.json()["renderer"] == "chromium"
    assert response.json()["status"] == "ok"


def test_health_is_503_when_the_renderer_is_degraded_in_production(
    app_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _renderer(monkeypatch, "degraded")
    monkeypatch.setattr(settings, "app_env", "production")
    response = app_client.get("/health")
    assert response.status_code == 503
    assert response.json()["status"] == "degraded"
    assert response.json()["renderer"] == "degraded"


def test_a_degraded_renderer_is_only_reported_outside_production(
    app_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _renderer(monkeypatch, "degraded")
    response = app_client.get("/health")
    assert response.status_code == 200
    assert response.json()["renderer"] == "degraded"


def test_the_crypto_handler_logs_no_raw_exception_text(
    app_client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    from app.core.crypto import CryptoError

    def boom() -> None:
        raise CryptoError("unwrap failed for key password=hunter2hunter2")

    app_client.app.add_api_route("/__test__/crypto", boom, methods=["GET"])  # type: ignore[attr-defined]
    with caplog.at_level(logging.ERROR):
        response = app_client.get("/__test__/crypto")
    assert response.status_code == 500
    assert "hunter2" not in response.text
    assert "hunter2" not in caplog.text
    assert "Crypto failure" in caplog.text and "CryptoError" in caplog.text


def test_main_uses_log_failure_for_encryption_errors() -> None:
    source = (BACKEND / "app" / "main.py").read_text(encoding="utf-8")
    assert 'log.error("Encryption misconfigured: %s", exc)' not in source
    assert 'log_failure(log, "Encryption misconfigured", exc' in source
    assert not re.search(r'log\.error\("Crypto failure[^"]*", [^)]*\bexc\)', source)


# ── bootstrap.sql ───────────────────────────────────────────────────────────
def test_bootstrap_takes_passwords_from_psql_variables() -> None:
    sql = (BACKEND / "scripts" / "bootstrap.sql").read_text(encoding="utf-8")
    assert "change-me" not in sql
    assert "PASSWORD %L', :'owner_password'" in sql
    assert "PASSWORD %L', :'app_password'" in sql
    assert '-v owner_password="$AUTOMARKET_OWNER_PASSWORD"' in sql
    assert '-v app_password="$AUTOMARKET_APP_PASSWORD"' in sql
    # Refuses to run without them, before any role is created.
    first_create = sql.index("SELECT format('CREATE ROLE")
    assert sql.index("\\if :{?owner_password}") < first_create
    assert sql.index("\\if :{?app_password}") < first_create
    assert sql.index("SET log_statement = 'none';") < sql.index("length(:'owner_password')")
    # No literal password anywhere.
    assert not re.search(r"PASSWORD\s+'", sql)


# ── requirements-dev.txt ────────────────────────────────────────────────────
def test_pyyaml_is_pinned_to_the_installed_version() -> None:
    from importlib.metadata import version

    lines = (BACKEND / "requirements-dev.txt").read_text(encoding="utf-8").splitlines()
    pins = [line for line in lines if line.lower().startswith("pyyaml")]
    assert pins == [f"PyYAML=={version('PyYAML')}"]


# ── .env.example ────────────────────────────────────────────────────────────
def test_env_example_documents_the_new_settings() -> None:
    text = (BACKEND / ".env.example").read_text(encoding="utf-8")
    assert "# DEV_LOG_SIGN_IN_CODES=true" in text
    assert not re.search(r"^DEV_LOG_SIGN_IN_CODES=", text, re.M)
    for line in (
        "METRICS_BACKFILL_DAYS=90",
        "REDIS_URL=",
        "PUBLISHED_LINK_DAYS=365",
        "SOCIAL_DAILY_POST_CAP=25",
        "CONNECTOR_PRIVATE_HOSTS_ALLOWED=",
        "MAX_REQUEST_BODY_BYTES=1048576",
    ):
        assert re.search(rf"^{re.escape(line)}$", text, re.M), line


def test_env_example_values_load(monkeypatch: pytest.MonkeyPatch) -> None:
    """The documented values must parse with the real validators."""
    text = (BACKEND / ".env.example").read_text(encoding="utf-8")
    keys = (
        "METRICS_BACKFILL_DAYS", "REDIS_URL", "PUBLISHED_LINK_DAYS",
        "SOCIAL_DAILY_POST_CAP", "CONNECTOR_PRIVATE_HOSTS_ALLOWED", "MAX_REQUEST_BODY_BYTES",
    )
    for key in keys:
        match = re.search(rf"^{key}=(.*)$", text, re.M)
        assert match is not None
        monkeypatch.setenv(key, match.group(1))
    s = _settings()
    assert s.metrics_backfill_days == 90 and s.published_link_days == 365
    assert s.social_daily_post_cap == 25 and s.max_request_body_bytes == 1_048_576
    assert s.connector_private_hosts_allowed == [] and s.redis_url == ""


# ── Allow-listed private hosts stay https-only ──────────────────────────────
def test_plain_http_to_an_allow_listed_private_host_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "connector_private_hosts_allowed", ["intranet-host"])
    _dns(monkeypatch, {"intranet-host": ["10.4.4.4"]})
    raw = Raw()
    connector = _connector("custom_api", raw, webhookUrl="http://intranet-host", secret="s" * 16)
    with pytest.raises(ConnectorConfigError) as caught:
        connector.request("GET", "/health")
    assert caught.value.details["reason"] == "scheme"
    assert raw.connects == []


def test_https_to_an_allow_listed_private_host_works(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "connector_private_hosts_allowed", ["intranet-host"])
    _dns(monkeypatch, {"intranet-host": ["10.4.4.4"]})
    raw = Raw(_http(200, b'{"ok": true}'))
    connector = _connector("custom_api", raw, webhookUrl="https://intranet-host", secret="s" * 16)
    assert connector.request("GET", "/health") == {"ok": True}
    assert raw.connects[0]["host"] == "10.4.4.4" and raw.connects[0]["port"] == 443
    assert raw.connects[0]["sni"] == "intranet-host"


def test_a_redirect_to_plain_http_on_an_allow_listed_host_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "connector_private_hosts_allowed", ["intranet-host"])
    _dns(monkeypatch, {"intranet-host": ["10.4.4.4"]})
    raw = Raw(_http(302, b"", {"Location": "http://intranet-host/health"}))
    connector = _connector("custom_api", raw, webhookUrl="https://intranet-host", secret="s" * 16)
    with pytest.raises(ConnectorConfigError) as caught:
        connector.request("GET", "/health")
    assert caught.value.details["reason"] == "scheme"
    assert len(raw.connects) == 1


def test_link_local_metadata_stays_blocked_when_allow_listed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "connector_private_hosts_allowed", ["169.254.169.254/32", "meta-host"])
    _dns(monkeypatch, {"meta-host": ["169.254.169.254"]})
    raw = Raw()
    connector = _connector("magento", raw, baseUrl="https://meta-host", accessToken="t" * 12)
    with pytest.raises(ConnectorConfigError) as caught:
        connector.request("GET", "/latest/meta-data/")
    assert caught.value.details["reason"] == "blocked"
    assert raw.connects == []


def test_the_site_crawler_does_not_use_the_private_list(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "connector_private_hosts_allowed", ["intranet-host"])
    _dns(monkeypatch, {"intranet-host": ["10.4.4.4"]})
    raw = Raw()
    connector = _connector("site_crawler", raw, siteUrl="https://intranet-host", discovery="crawl")
    with pytest.raises(ConnectorConfigError) as caught:
        connector.request("GET", "/", expect_json=False)
    assert caught.value.details["reason"] == "blocked"
    assert raw.connects == []


# ── SOCIAL_DAILY_POST_CAP ───────────────────────────────────────────────────
def test_the_daily_cap_defaults_to_the_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services import assets

    monkeypatch.setattr(settings, "social_daily_post_cap", 7)
    assert assets.daily_social_cap(SimpleNamespace(settings={})) == 7
    assert assets.daily_social_cap(SimpleNamespace(settings=None)) == 7


def test_the_workspace_cap_overrides_the_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services import assets

    monkeypatch.setattr(settings, "social_daily_post_cap", 7)
    org = SimpleNamespace(settings={assets.DAILY_CAP_SETTING: 3})
    assert assets.daily_social_cap(org) == 3
    # A junk override falls back to the setting; a huge one is bounded.
    assert assets.daily_social_cap(SimpleNamespace(settings={assets.DAILY_CAP_SETTING: "9"})) == 7
    assert assets.daily_social_cap(SimpleNamespace(settings={assets.DAILY_CAP_SETTING: 10**6})) == (
        assets.MAX_DAILY_SOCIAL_POSTS
    )


# ── REDIS_URL in tasks ──────────────────────────────────────────────────────
def test_tasks_read_redis_url_from_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.orchestration import tasks

    monkeypatch.setattr(settings, "redis_url", "redis://cache:6379/2")
    monkeypatch.setattr(settings, "celery_broker_url", "redis://broker:6379/0")
    assert tasks._redis_url() == "redis://cache:6379/2"


def test_tasks_fall_back_to_a_redis_broker(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.orchestration import tasks

    monkeypatch.setattr(settings, "redis_url", "")
    monkeypatch.setattr(settings, "celery_broker_url", "redis://broker:6379/0")
    assert tasks._redis_url() == "redis://broker:6379/0"
    monkeypatch.setattr(settings, "celery_broker_url", "amqp://guest@rabbit//")
    assert tasks._redis_url() == ""
