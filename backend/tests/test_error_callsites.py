"""Raw exception text never reaches a user, or a stored ``last_error``.

Each place that used to echo ``str(exc)`` is driven with an exception whose
text carries a fake secret and a stack-trace-like tail. The response (or the
stored field the console reads) must hold authored copy and neither of those.
The logs must not carry them either.
"""
from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any

import pytest

from app.core.exceptions import AppError, ConnectorError, InvalidInputError
from app.llm.base import LLMError

FAKE_SECRET = "sk-live-FAKE0123456789abcdefSECRET"
TRACE = 'Traceback (most recent call last):\n  File "/srv/app/internal.py", line 42, in call'
POISON = f"vendor said key={FAKE_SECRET} {TRACE}"


def _assert_clean(text: str) -> None:
    assert FAKE_SECRET not in text
    assert "Traceback" not in text
    assert "internal.py" not in text
    assert "key=" not in text


def _assert_logs_clean(caplog: pytest.LogCaptureFixture) -> None:
    for record in caplog.records:
        _assert_clean(record.getMessage())
        assert record.exc_info is None, "a traceback would print the secret"


# ── SEO assistant ───────────────────────────────────────────────────────────
@pytest.fixture
def assistant(monkeypatch: pytest.MonkeyPatch) -> Any:  # noqa: ANN401
    from app.services import seo_assistant

    monkeypatch.setattr(seo_assistant, "_catalogue", lambda db, tenant_id: {"agents": [], "connectors": []})
    monkeypatch.setattr(seo_assistant, "_system_prompt", lambda *a, **k: "system")
    monkeypatch.setattr(seo_assistant, "_looks_off_topic", lambda text: False)
    return seo_assistant


@pytest.mark.parametrize("error", [LLMError(POISON), RuntimeError(POISON)])
def test_assistant_chat_never_echoes_the_exception(
    assistant: Any, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, error: Exception  # noqa: ANN401
) -> None:
    from app.services.assistant_errors import AssistantUpstreamError

    def boom(**kwargs: Any) -> None:  # noqa: ANN401
        raise error

    monkeypatch.setattr("app.llm.willy_chat_engine.run_chat_engine", boom)
    caplog.set_level(logging.DEBUG)
    with pytest.raises(AssistantUpstreamError) as raised:
        assistant.chat(None, tenant_id="t1", mode="ask", message="How do I rank?")
    _assert_clean(raised.value.message)
    assert "ANTHROPIC_API_KEY" not in raised.value.message
    assert ".env" not in raised.value.message
    assert raised.value.message.startswith("The assistant")
    _assert_logs_clean(caplog)


@pytest.mark.parametrize("error", [LLMError(POISON), RuntimeError(POISON)])
def test_assistant_stream_never_echoes_the_exception(
    assistant: Any, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, error: Exception  # noqa: ANN401
) -> None:
    def boom(**kwargs: Any) -> Iterator[tuple[str, ...]]:  # noqa: ANN401
        raise error
        yield ("delta", "")  # pragma: no cover - makes this a generator

    monkeypatch.setattr("app.llm.willy_chat_engine.run_chat_engine_stream", boom)
    caplog.set_level(logging.DEBUG)
    events = list(assistant.chat_stream(None, tenant_id="t1", mode="ask", message="How do I rank?"))
    errors = [e for e in events if e["event"] == "error"]
    assert len(errors) == 1
    _assert_clean(errors[0]["detail"])
    assert ".env" not in errors[0]["detail"]
    _assert_logs_clean(caplog)


def test_assistant_setup_gap_does_not_name_environment_variables(
    assistant: Any, monkeypatch: pytest.MonkeyPatch  # noqa: ANN401
) -> None:
    from app.services.assistant_errors import AssistantConfigError

    monkeypatch.setattr(assistant.settings, "anthropic_api_key", "")
    with pytest.raises(AssistantConfigError) as raised:
        assistant._provider()
    assert "ANTHROPIC_API_KEY" not in raised.value.message
    assert ".env" not in raised.value.message

    events = list(assistant.chat_stream(None, tenant_id="t1", mode="ask", message="How do I rank?"))
    assert events == [{"event": "error", "detail": raised.value.message}]

    monkeypatch.setattr(assistant.settings, "anthropic_api_key", "configured")
    monkeypatch.setattr(assistant.settings, "assistant_model", "")
    monkeypatch.setattr(assistant.settings, "llm_model", "")
    with pytest.raises(AssistantConfigError) as raised:
        assistant._provider()
    assert "MODEL" not in raised.value.message


# ── Public demo analysis and the in-portal site analysis ───────────────────
class _FakeSession:
    def __init__(self, objects: dict[type, Any]) -> None:
        self.objects = objects

    def get(self, model: type, key: str) -> Any:  # noqa: ANN401
        return self.objects.get(model)

    def commit(self) -> None:
        pass

    def close(self) -> None:
        pass


def _analysis_row() -> SimpleNamespace:
    return SimpleNamespace(
        status="queued", started_at=None, finished_at=None, error="", report=None,
        website_url="https://example.com", business_url="", model="", context={},
        progress=[{"key": "fetch", "state": "running", "detail": ""}],
    )


def test_public_demo_analysis_stores_no_exception_text(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from app.models.demo import DemoAnalysis, DemoStatus
    from app.services.demo_analysis import service

    row = _analysis_row()
    monkeypatch.setattr("app.db.session.SessionLocal", lambda: _FakeSession({DemoAnalysis: row}))

    def boom(*args: Any, **kwargs: Any) -> None:  # noqa: ANN401
        raise RuntimeError(POISON)

    monkeypatch.setattr(service, "build_report", boom)
    caplog.set_level(logging.DEBUG)
    service.run("a1")
    assert row.status == DemoStatus.FAILED
    assert row.error == service.ANALYSIS_FAILED_MESSAGE
    _assert_clean(row.error)
    assert row.progress[0]["state"] == "failed"
    _assert_logs_clean(caplog)


def test_public_demo_analysis_keeps_authored_domain_messages(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.models.demo import DemoAnalysis
    from app.services.demo_analysis import service

    row = _analysis_row()
    monkeypatch.setattr("app.db.session.SessionLocal", lambda: _FakeSession({DemoAnalysis: row}))

    def refused(*args: Any, **kwargs: Any) -> None:  # noqa: ANN401
        raise AppError("We could not reach that website.")

    monkeypatch.setattr(service, "build_report", refused)
    service.run("a1")
    assert row.error == "We could not reach that website."


def test_workspace_site_analysis_stores_no_exception_text(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from app.models.demo import DemoStatus, SiteAnalysis
    from app.models.workspace import Organization
    from app.services.demo_analysis import service, workspace

    row = _analysis_row()
    session = _FakeSession({SiteAnalysis: row, Organization: SimpleNamespace(id="t1")})

    @contextmanager
    def scope(tenant_id: str) -> Iterator[_FakeSession]:
        yield session

    def boom(*args: Any, **kwargs: Any) -> None:  # noqa: ANN401
        raise RuntimeError(POISON)

    monkeypatch.setattr("app.db.session.session_scope", scope)
    monkeypatch.setattr(
        "app.services.connectors.build_connector_bundle",
        lambda db, org: SimpleNamespace(instances={}, names={}, close=lambda: None),
    )
    monkeypatch.setattr("app.llm.from_connector.provider_from_connector", boom)
    caplog.set_level(logging.DEBUG)
    workspace.run("a1", "t1")
    assert row.status == DemoStatus.FAILED
    assert row.error == service.ANALYSIS_FAILED_MESSAGE
    _assert_logs_clean(caplog)


# ── Connector last_error ───────────────────────────────────────────────────
@pytest.mark.parametrize(
    "error",
    [
        RuntimeError(POISON),
        ConnectorError(POISON),
        ConnectorError(f"Rejected the key {FAKE_SECRET}"),
        ConnectorError("Bearer abc.def.ghi was refused"),
        ConnectorError("AKIAABCDEFGHIJKLMNOP is not allowed"),
        ConnectorError("x" * 40),
    ],
)
def test_public_connector_error_withholds_secrets_and_traces(error: Exception) -> None:
    from app.services.connectors import CONNECTION_PROBLEM, public_connector_error

    message = public_connector_error(error)
    assert message == CONNECTION_PROBLEM
    _assert_clean(message)


def test_public_connector_error_passes_authored_connector_copy() -> None:
    from app.services.connectors import public_connector_error

    authored = "WordPress could not be reached. Check your connection and try again."
    assert public_connector_error(ConnectorError(authored)) == authored
    assert public_connector_error(ValueError("plain"), fallback="Fallback.") == "Fallback."


class _FlushOnly:
    def flush(self) -> None:
        pass


class _BrokenConnector:
    def __enter__(self) -> _BrokenConnector:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def check_health(self) -> None:
        raise ConnectorError(POISON)

    def on_connect(self) -> None:
        raise ConnectorError(POISON)


def test_health_check_last_error_has_no_exception_text(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from app.models.connector import ConnectorHealth
    from app.services import connectors

    record = SimpleNamespace(slug="fake_cms", connected=True, health="ok", last_error="", last_health_check_at=None)
    monkeypatch.setattr(connectors, "get_record", lambda db, tenant_id, slug: record)
    monkeypatch.setattr(connectors.OrgCipher, "for_org", classmethod(lambda cls, org: object()))
    monkeypatch.setattr(connectors, "instantiate", lambda record, cipher: _BrokenConnector())
    caplog.set_level(logging.DEBUG)
    connectors.check_health(_FlushOnly(), org=SimpleNamespace(id="t1"), slug="fake_cms")
    assert record.health == ConnectorHealth.FAILING.value
    assert record.last_error == connectors.CONNECTION_PROBLEM
    _assert_logs_clean(caplog)


def test_failed_probe_detail_is_screened(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services import connectors

    class _Refusing(_BrokenConnector):
        def check_health(self) -> SimpleNamespace:
            return SimpleNamespace(ok=False, detail=f"token={FAKE_SECRET}")

    record = SimpleNamespace(slug="fake_cms", connected=True, health="ok", last_error="", last_health_check_at=None)
    monkeypatch.setattr(connectors, "get_record", lambda db, tenant_id, slug: record)
    monkeypatch.setattr(connectors.OrgCipher, "for_org", classmethod(lambda cls, org: object()))
    monkeypatch.setattr(connectors, "instantiate", lambda record, cipher: _Refusing())
    connectors.check_health(_FlushOnly(), org=SimpleNamespace(id="t1"), slug="fake_cms")
    _assert_clean(record.last_error)


def test_on_connect_failure_records_no_exception_text(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from app.connectors.base.connector import HealthReport
    from app.models.connector import ConnectorHealth, ConnectorRecord
    from app.services import connectors

    class _Cipher:
        def encrypt_json(self, value: Any, *, context: str) -> str:  # noqa: ANN401
            return "ciphertext"

    record = ConnectorRecord(slug="fake_cms", name="Fake CMS", credentials_version=0)
    monkeypatch.setattr(connectors.OrgCipher, "for_org", classmethod(lambda cls, org: _Cipher()))
    monkeypatch.setattr(connectors, "instantiate", lambda record, cipher: _BrokenConnector())
    caplog.set_level(logging.DEBUG)
    connectors.store_verified(
        _FlushOnly(),
        org=SimpleNamespace(id="t1"),
        actor=SimpleNamespace(name="Ada"),
        record=record,
        values={"siteUrl": "https://example.com"},
        report=HealthReport(ok=True, probed=True),
    )
    assert record.health == ConnectorHealth.DEGRADED.value
    assert record.last_error == connectors.CONNECTION_PROBLEM
    _assert_logs_clean(caplog)


# ── Scheduler rollup ───────────────────────────────────────────────────────
class _OneRecord:
    def __init__(self, record: Any) -> None:  # noqa: ANN401
        self.record = record

    def execute(self, statement: Any) -> SimpleNamespace:  # noqa: ANN401
        return SimpleNamespace(scalar_one_or_none=lambda: self.record)


@pytest.mark.parametrize("error", [RuntimeError(POISON), ConnectorError(POISON)])
def test_rollup_failure_records_no_exception_text(
    caplog: pytest.LogCaptureFixture, error: Exception
) -> None:
    from app.models.connector import ConnectorHealth
    from app.orchestration import scheduler

    record = SimpleNamespace(health="ok", last_error="")
    caplog.set_level(logging.DEBUG)
    scheduler._mark_failing(_OneRecord(record), "t1", "fake_analytics", error)
    assert record.health == ConnectorHealth.FAILING.value
    _assert_clean(record.last_error)
    assert record.last_error
    _assert_logs_clean(caplog)


def test_rollup_expired_sign_in_still_says_reconnect() -> None:
    from app.orchestration import scheduler

    record = SimpleNamespace(health="ok", last_error="")
    scheduler._mark_failing(
        _OneRecord(record), "t1", "fake_analytics", ConnectorError(f"invalid_grant {FAKE_SECRET}")
    )
    assert record.last_error.startswith("Sign-in expired")
    _assert_clean(record.last_error)


# ── Runner: a change that fails to apply ───────────────────────────────────
def test_failed_apply_records_no_exception_text(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from app.agents.base.policy import Decision
    from app.orchestration import runner

    def boom(action: Any, ctx: Any) -> None:  # noqa: ANN401
        raise RuntimeError(POISON)

    route = SimpleNamespace(notes=[], measure=None)
    monkeypatch.setattr(runner.safety, "assess", lambda action, gate: route)
    monkeypatch.setattr(runner, "decide", lambda action, **kwargs: (Decision.APPLY, "ok"))
    monkeypatch.setattr(runner, "_apply", boom)
    record = SimpleNamespace(slug="on_page_seo_sync", category="SEO & AEO", autonomy=True, last_error="", actions_today=0)
    ctx = SimpleNamespace(progress=lambda *a, **k: None, remaining_actions=5)
    gate = SimpleNamespace(practice=None, halted=lambda: None, spending=False)
    action = SimpleNamespace(kind="update_meta", title="Update the title")
    caplog.set_level(logging.DEBUG)
    tally = runner._process_actions(
        None, ctx=ctx, org=SimpleNamespace(id="t1"), record=record, run_id="r1",
        actions=[action], guardrail=None, gate=gate,
    )
    assert tally.applied == 0
    assert record.last_error
    _assert_clean(record.last_error)
    _assert_logs_clean(caplog)


# ── API validation errors ──────────────────────────────────────────────────
def test_agent_setting_error_is_written_from_the_field(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from app.agents.base import plain
    from app.api.v1 import agents

    def poisoned_clean(self: Any, raw: str) -> str:  # noqa: ANN401
        raise ValueError(POISON)

    record = SimpleNamespace(name="Technical SEO auditor", settings={}, schedule="", scope="")
    monkeypatch.setattr(agents, "_record", lambda db, tenant_id, slug: record)
    monkeypatch.setattr(agents, "_validate_config", lambda *a: ([], None))
    monkeypatch.setattr(plain.SettingField, "clean", poisoned_clean)
    current = SimpleNamespace(tenant_id="t1", require_write=lambda module: None)
    payload = SimpleNamespace(
        schedule="daily", scope="", notify_channel="", llm_connector="",
        max_actions_per_day=5, settings={"thin_words": "abc"},
    )
    caplog.set_level(logging.DEBUG)
    with pytest.raises(InvalidInputError) as raised:
        agents.configure("technical_seo_auditor", payload, current, None, None)
    _assert_clean(raised.value.message)
    assert raised.value.message == "Thin content below (words) must be a whole number from 50 to 5000."
    _assert_logs_clean(caplog)


def test_portal_feature_error_is_authored(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from app.api.v1 import portal

    def boom(*args: Any, **kwargs: Any) -> None:  # noqa: ANN401
        raise ValueError(POISON)

    monkeypatch.setattr(portal.portal_features, "set_flag", boom)
    current = SimpleNamespace(user=SimpleNamespace(email="a@example.com", name="A"), tenant_id="t1")
    caplog.set_level(logging.DEBUG)
    with pytest.raises(InvalidInputError) as raised:
        portal.update_feature("agent", "nope", portal.PortalFeatureUpdate(enabled=True), current, None, None)
    assert raised.value.message == "That feature is not recognised."
    _assert_logs_clean(caplog)


def test_verification_option_error_is_authored(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from app.api.v1 import verification as route

    def boom(options: Any) -> None:  # noqa: ANN401
        raise ValueError(POISON)

    monkeypatch.setattr(route.verification, "has_steps", lambda slug: True)
    monkeypatch.setattr(route.verification, "clean_options", boom)
    payload = route.VerificationRunRequest(workspace_id="w1", options={"verifyX": "1"})
    caplog.set_level(logging.DEBUG)
    with pytest.raises(InvalidInputError) as raised:
        route.start_run("fake_cms", payload, None, None, None)
    _assert_clean(raised.value.message)
    assert raised.value.message.startswith("Run options must have names starting with")
    _assert_logs_clean(caplog)


def test_verification_rejects_a_real_bad_option() -> None:
    """Negative case through the real validator: an option that could
    overwrite a credential is refused with the authored message."""
    from app.api.v1 import verification as route

    payload = route.VerificationRunRequest(workspace_id="w1", options={"apiKey": FAKE_SECRET})
    with pytest.raises(InvalidInputError) as raised:
        route.start_run("anthropic_claude", payload, None, None, None)
    _assert_clean(raised.value.message)
