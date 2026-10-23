"""Settings added for Creative Studio and hardening: defaults, parsing, and the
production guardrails that refuse an unsafe deployment at startup."""
from __future__ import annotations

import logging
from typing import Any

import pytest
from pydantic import ValidationError

from app.core.config import Settings


def _production(**overrides: Any) -> Settings:
    """A production configuration that passes every guardrail, plus overrides."""
    values: dict[str, Any] = {
        "app_env": "production",
        "secret_key": "s" * 48,
        "master_encryption_key": "bWFzdGVyLWtleS1mb3ItdGVzdHMtb25seS0zMmJ5dGU=",
        "blind_index_key": "YmxpbmQtaW5kZXgta2V5LWZvci10ZXN0cy1vbmx5LTM=",
        "postgres_sslmode": "require",
        "database_url": "",
        # Ignore a dual-role suite's MIGRATION_DATABASE_URL in the process env
        # unless the test opts in with its own override.
        "migration_database_url": "",
        "trusted_hosts": ["api.synaprank.ai"],
        "force_https": True,
        "rate_limit_enabled": True,
        "db_enforce_rls": True,
        "resend_api_key": "re_test",
        "public_base_url": "https://app.synaprank.ai",
        "cors_origins": ["https://app.synaprank.ai"],
        "demo_allowed_origins": ["https://www.synaprank.ai"],
        "portal_admin_emails": ["ops@synaprank.ai"],
        "trusted_proxies": ["10.0.0.0/24"],
        "creative_chromium_sandbox": True,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def _dev(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "app_env": "development",
        "migration_database_url": "",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


# ── Happy path ─────────────────────────────────────────────────────────────


def test_valid_production_config_starts() -> None:
    s = _production()
    assert s.is_production
    assert s.storage_backend == "postgres"


def test_creative_defaults() -> None:
    s = _dev()
    assert s.creative_renderer == "chromium"
    assert s.creative_chromium_sandbox is False
    assert s.creative_storage_max_bytes_per_tenant == 2 * 1024**3
    # Model ids never live in code: the allow lists are empty (deny all)
    # until configured; .env.example carries the recommended values.
    assert s.creative_allowed_text_models == []
    assert s.creative_denied_text_models == []
    assert s.creative_allowed_image_models == []
    assert s.creative_denied_image_models == []
    # URL fetching of generated images is off unless hosts are listed.
    assert s.image_fetch_allowed_hosts == []
    assert s.reddit_enabled is False
    assert s.linkedin_api_version == "202610"
    assert s.x_metrics_max_reads_per_run == 100
    assert (s.creative_render_soft_time_limit, s.creative_render_time_limit) == (600, 660)
    assert s.creative_render_isolated is False
    assert s.self_hosted_llm_allow_plain_http is False
    assert s.self_hosted_llm_allowed_ports == [443, 8000, 11434]
    assert s.edge_tls_terminated is False
    assert s.allow_privileged_db_role is False
    assert s.celery_max_tasks_per_child == 50
    assert s.celery_render_concurrency == 1
    assert s.celery_max_memory_per_child_kb == 1_500_000
    assert s.llm_price_table == {}


def test_csv_lists_are_split_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRUSTED_PROXIES", "127.0.0.1, ::1 ,10.1.2.3/16")
    monkeypatch.setenv("IMAGE_FETCH_ALLOWED_HOSTS", "Images.Example.NET")
    monkeypatch.setenv("SELF_HOSTED_LLM_ALLOWED_HOSTS", "ollama.internal,10.20.0.0/16")
    monkeypatch.setenv("CREATIVE_ALLOWED_IMAGE_MODELS", "openai:gpt-image-2*")
    s = Settings(_env_file=None)
    assert s.trusted_proxies == ["127.0.0.1/32", "::1/128", "10.1.0.0/16"]
    assert s.image_fetch_allowed_hosts == ["images.example.net"]
    assert s.self_hosted_llm_allowed_hosts == ["ollama.internal", "10.20.0.0/16"]
    assert s.creative_allowed_image_models == ["openai:gpt-image-2*"]


def test_price_table_parsed_from_json(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_PRICE_TABLE", '{"openai:gpt-5*": [1.25, 10], "anthropic:claude-*": [3, 15]}')
    s = Settings(_env_file=None)
    assert s.llm_price_table == {"openai:gpt-5*": [1.25, 10.0], "anthropic:claude-*": [3.0, 15.0]}


def test_empty_trusted_proxies_warns_in_production(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="app.core.config"):
        _production(trusted_proxies=[], edge_tls_terminated=True)
    assert any("TRUSTED_PROXIES is empty" in r.getMessage() for r in caplog.records)


# ── HTTPS behind a proxy ───────────────────────────────────────────────────


def test_force_https_without_proxies_is_refused_in_production() -> None:
    # Every request would look like plain http and be redirected in a loop.
    with pytest.raises(ValidationError, match="EDGE_TLS_TERMINATED"):
        _production(trusted_proxies=[])


def test_edge_tls_terminated_allows_empty_proxies() -> None:
    s = _production(trusted_proxies=[], edge_tls_terminated=True)
    assert s.force_https and s.edge_tls_terminated


# ── Database TLS ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("mode", ["require", "verify-ca", "verify-full"])
def test_strict_sslmodes_start_in_production(mode: str) -> None:
    s = _production(
        database_url=f"postgresql://app:pw@db.internal/app?sslmode={mode}",
        migration_database_url=f"postgresql://owner:pw@db.internal/app?sslmode={mode}",
    )
    assert s.effective_sslmode == mode


@pytest.mark.parametrize("mode", ["prefer", "allow", "disable"])
def test_weak_sslmode_is_refused_in_production(mode: str) -> None:
    with pytest.raises(ValidationError, match="POSTGRES_SSLMODE"):
        _production(postgres_sslmode=mode)
    with pytest.raises(ValidationError, match="POSTGRES_SSLMODE"):
        _production(database_url=f"postgresql://app:pw@db.internal/app?sslmode={mode}")


@pytest.mark.parametrize(
    "url",
    [
        "postgresql://owner:pw@db.internal/app",
        "postgresql://owner:pw@db.internal/app?sslmode=prefer",
        "postgresql://owner:pw@db.internal/app?sslmode=disable",
    ],
)
def test_migration_url_without_strict_sslmode_is_refused(url: str) -> None:
    with pytest.raises(ValidationError, match="MIGRATION_DATABASE_URL"):
        _production(migration_database_url=url)


def test_database_url_without_sslmode_uses_postgres_sslmode() -> None:
    s = _production(database_url="postgresql://app:pw@db.internal/app")
    assert s.effective_sslmode == "require"


# ── Chromium sandbox ───────────────────────────────────────────────────────


def test_unsandboxed_chromium_is_refused_in_production() -> None:
    with pytest.raises(ValidationError, match="CREATIVE_RENDER_ISOLATED"):
        _production(creative_chromium_sandbox=False)


def test_render_isolated_does_not_unblock_without_celery() -> None:
    # Isolated on a process that still runs Chromium locally is a false claim.
    with pytest.raises(ValidationError, match="CREATIVE_RENDER_ISOLATED"):
        _production(creative_chromium_sandbox=False, creative_render_isolated=True)


def test_unsandboxed_chromium_starts_when_isolated() -> None:
    s = _production(
        creative_chromium_sandbox=False,
        creative_render_isolated=True,
        celery_enabled=True,
    )
    assert s.creative_render_isolated
    assert s.celery_enabled


def test_celery_enabled_starts_unsandboxed_chromium_without_isolated() -> None:
    # The API has Celery on and does not set CREATIVE_RENDER_ISOLATED;
    # renders leave this process.
    s = _production(creative_chromium_sandbox=False, celery_enabled=True)
    assert s.celery_enabled
    assert s.creative_render_isolated is False


def test_pillow_renderer_skips_the_sandbox_check() -> None:
    s = _production(creative_chromium_sandbox=False, creative_renderer="pillow")
    assert s.creative_renderer == "pillow"


def test_unsandboxed_chromium_is_fine_in_development() -> None:
    assert _dev(creative_chromium_sandbox=False).creative_render_isolated is False


# ── Self-hosted model ports ────────────────────────────────────────────────


def test_llm_ports_parse_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SELF_HOSTED_LLM_ALLOWED_PORTS", "443, 8080,443")
    assert Settings(_env_file=None).self_hosted_llm_allowed_ports == [443, 8080]


@pytest.mark.parametrize("raw", ["0", "70000", "http", "", "-1"])
def test_bad_llm_ports_refused(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    monkeypatch.setenv("SELF_HOSTED_LLM_ALLOWED_PORTS", raw)
    with pytest.raises(ValidationError, match="(?i)SELF_HOSTED_LLM_ALLOWED_PORTS|int"):
        Settings(_env_file=None)


def test_too_many_llm_ports_refused() -> None:
    with pytest.raises(ValidationError, match="at most"):
        _dev(self_hosted_llm_allowed_ports=list(range(1000, 1030)))


def test_plain_http_switch_warns_in_production(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="app.core.config"):
        _production(self_hosted_llm_allow_plain_http=True)
    assert any("SELF_HOSTED_LLM_ALLOW_PLAIN_HTTP" in r.getMessage() for r in caplog.records)


def test_development_tolerates_localhost_cors() -> None:
    assert _dev().cors_origins[0].startswith("http://localhost")


# ── Production refusals ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "origins",
    [
        ["http://localhost:5173"],
        ["https://localhost"],
        ["https://127.0.0.1:8443"],
        ["*"],
        ["http://app.synaprank.ai"],
        ["https://app.synaprank.ai", "https://[::1]"],
    ],
)
def test_production_refuses_unsafe_cors(origins: list[str]) -> None:
    with pytest.raises(ValidationError, match="CORS_ORIGINS"):
        _production(cors_origins=origins)


def test_production_refuses_localhost_demo_origin() -> None:
    with pytest.raises(ValidationError, match="DEMO_ALLOWED_ORIGINS"):
        _production(demo_allowed_origins=["http://localhost:3000"])


def test_production_starts_with_a_concrete_trusted_host() -> None:
    s = _production(trusted_hosts=["api.synaprank.ai"])
    assert s.trusted_hosts == ["api.synaprank.ai"]


@pytest.mark.parametrize("hosts", [[], ["*"], ["", "  "], ["*", "api.synaprank.ai"]])
def test_production_refuses_empty_or_wildcard_trusted_hosts(hosts: list[str]) -> None:
    with pytest.raises(ValidationError, match="TRUSTED_HOSTS"):
        _production(trusted_hosts=hosts)


def test_blank_trusted_hosts_env_is_an_empty_list() -> None:
    s = _dev(trusted_hosts="")
    assert s.trusted_hosts == []


def test_production_refuses_rls_off() -> None:
    with pytest.raises(ValidationError, match="DB_ENFORCE_RLS"):
        _production(db_enforce_rls=False)


@pytest.mark.parametrize("email", ["admin@example.com", "ops@acme.test", "me@yourcompany.com"])
def test_production_refuses_placeholder_admin(email: str) -> None:
    with pytest.raises(ValidationError, match="PORTAL_ADMIN_EMAILS"):
        _production(portal_admin_emails=[email])


def test_production_refuses_s3_storage() -> None:
    with pytest.raises(ValidationError, match="STORAGE_BACKEND"):
        _production(storage_backend="s3")


@pytest.mark.parametrize("cidr", ["0.0.0.0/0", "::/0", "1.2.3.4/0"])
def test_production_refuses_catch_all_proxy(cidr: str) -> None:
    with pytest.raises(ValidationError, match="TRUSTED_PROXIES"):
        _production(trusted_proxies=["10.0.0.1", cidr])


# ── Malformed input (refused in every environment) ─────────────────────────


@pytest.mark.parametrize(
    "raw",
    [
        "{not json",
        "[1, 2]",
        '{"openai:gpt-5*": [1.0]}',
        '{"openai:gpt-5*": [-1, 2]}',
        '{"openai:gpt-5*": ["1", 2]}',
        '{"openai:gpt-5*": [true, 2]}',
        '{"bad key with spaces": [1, 2]}',
    ],
)
def test_invalid_price_table_refused(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    monkeypatch.setenv("LLM_PRICE_TABLE", raw)
    with pytest.raises(ValidationError, match="LLM_PRICE_TABLE"):
        Settings(_env_file=None)


def test_invalid_trusted_proxy_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRUSTED_PROXIES", "10.0.0.1,not-an-ip")
    with pytest.raises(ValidationError, match="TRUSTED_PROXIES"):
        Settings(_env_file=None)


@pytest.mark.parametrize("entry", ["0.0.0.0/0", "::/0"])
def test_self_hosted_catch_all_refused(entry: str) -> None:
    with pytest.raises(ValidationError, match="SELF_HOSTED_LLM_ALLOWED_HOSTS"):
        _dev(self_hosted_llm_allowed_hosts=[entry])


@pytest.mark.parametrize(
    "host", ["*", "*.example.com", "https://images.example.com", "host:8080", "a/b"]
)
def test_image_fetch_hosts_must_be_plain(host: str) -> None:
    with pytest.raises(ValidationError, match="IMAGE_FETCH_ALLOWED_HOSTS"):
        _dev(image_fetch_allowed_hosts=[host])


@pytest.mark.parametrize("pattern", ["gpt image", "a" * 129, "gpt-<script>"])
def test_model_patterns_validated(pattern: str) -> None:
    with pytest.raises(ValidationError):
        _dev(creative_allowed_text_models=[pattern])


def test_render_soft_limit_must_be_below_hard_limit() -> None:
    with pytest.raises(ValidationError, match="SOFT_TIME_LIMIT"):
        _dev(creative_render_soft_time_limit=150, creative_render_time_limit=150)


def test_bad_enum_and_version_refused() -> None:
    with pytest.raises(ValidationError):
        _dev(creative_renderer="wkhtmltopdf")
    with pytest.raises(ValidationError):
        _dev(linkedin_api_version="2026-10")


def test_migration_url_must_be_postgres() -> None:
    with pytest.raises(ValidationError):
        _dev(migration_database_url="mysql://u:p@h/db")
    s = _dev(migration_database_url="postgresql://owner:pw@db.internal/app")
    assert s.migration_database_url.startswith("postgresql+psycopg://")
    assert "pw@" not in repr(s)


def test_env_example_model_lists_parse(monkeypatch: pytest.MonkeyPatch) -> None:
    """The recommended allow/deny lists live in .env.example; they must load."""
    import pathlib

    example = pathlib.Path(__file__).resolve().parents[1] / ".env.example"
    wanted = {
        "CREATIVE_ALLOWED_TEXT_MODELS", "CREATIVE_DENIED_TEXT_MODELS",
        "CREATIVE_ALLOWED_IMAGE_MODELS", "CREATIVE_DENIED_IMAGE_MODELS",
    }
    found: dict[str, str] = {}
    for line in example.read_text(encoding="utf-8").splitlines():
        key, sep, value = line.partition("=")
        if sep and key in wanted:
            found[key] = value
            monkeypatch.setenv(key, value)
    assert set(found) == wanted
    s = Settings(_env_file=None)
    assert "ollama:*" in s.creative_allowed_text_models
    assert "*embedding*" in s.creative_denied_text_models
    assert len(s.creative_allowed_image_models) == 4
    assert len(s.creative_denied_image_models) == 2
