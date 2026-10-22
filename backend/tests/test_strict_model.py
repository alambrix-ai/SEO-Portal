"""The "Always use the model I picked" agent setting (``runner.STRICT_MODEL_SETTING``).

Saved through ``PUT /agents/{slug}/config`` (``strict_model``), shown on the
agent card, and read by the runner. Only agents that write with a model can
have it switched on.
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from app.api.v1 import agents as agents_api
from app.core.exceptions import ForbiddenError, InvalidInputError
from app.orchestration.runner import STRICT_MODEL_SETTING
from app.schemas.workspace import AgentConfigRequest, LlmConnectorOut, NotifyChannelOut
from app.services import views

LLM_AGENT = "seo_article_writer"
NO_LLM_AGENT = "technical_seo_auditor"


def _current(role: str = "admin") -> Any:  # noqa: ANN401
    from app.api.deps import CurrentUser

    user = SimpleNamespace(id="u1", name="Person", email="person@example.com", role=role, tenant_id="t1")
    org = SimpleNamespace(id="t1", name="Acme", settings={}, global_autonomy=False)
    return CurrentUser(user=user, organization=org, claims={})


def _record(slug: str = LLM_AGENT, **settings: Any) -> SimpleNamespace:  # noqa: ANN401
    return SimpleNamespace(
        id="aaaaaaaa-0000-4000-8000-000000000001", slug=slug, name="Writer", settings=dict(settings),
        schedule="Daily", scope="", notify_channel="None", llm_connector="", max_actions_per_day=5,
        autonomy=False, configured=False, status="paused", next_run_at=None,
        category="Content & Social", description="", metric_label="", last_error="", actions_today=0,
        last_run_at=None, consecutive_failures=0,
    )


class _Db:
    def __init__(self) -> None:
        self.commits = 0

    def commit(self) -> None:
        self.commits += 1


@pytest.fixture
def offline(monkeypatch: pytest.MonkeyPatch) -> None:
    """One connected model and every notify channel, without a database."""
    monkeypatch.setattr(
        agents_api, "_llm_options",
        lambda db, org: [LlmConnectorOut(slug="openai", name="OpenAI", available=True)],
    )
    monkeypatch.setattr(agents_api, "_notify_options", lambda db, org: [NotifyChannelOut(value="None")])


# ── The request field ───────────────────────────────────────────────────────
def test_strict_model_is_optional_and_strictly_a_boolean() -> None:
    assert AgentConfigRequest().strict_model is None
    assert AgentConfigRequest(strict_model=True).strict_model is True
    assert AgentConfigRequest(strict_model=False).strict_model is False
    for junk in ("yes", "true", 1, "on"):
        with pytest.raises(ValidationError):
            AgentConfigRequest.model_validate({"strict_model": junk})


# ── Storing it ──────────────────────────────────────────────────────────────
def test_switching_strict_on_and_off_stores_a_true_flag_or_nothing() -> None:
    record = _record(other="kept")
    agents_api._apply_strict_choice(record, True)
    assert record.settings == {"other": "kept", STRICT_MODEL_SETTING: True}
    agents_api._apply_strict_choice(record, None)  # omitted: keep what is saved
    assert record.settings[STRICT_MODEL_SETTING] is True
    agents_api._apply_strict_choice(record, False)
    assert record.settings == {"other": "kept"}


def test_check_config_accepts_strict_for_an_agent_that_writes(offline: None) -> None:
    payload = AgentConfigRequest(llm_connector="openai", strict_model=True)
    warnings, choices = agents_api._check_config(None, SimpleNamespace(id="t1"), LLM_AGENT, payload, None)
    assert warnings == [] and choices is None


def test_check_config_refuses_strict_for_an_agent_without_a_model(offline: None) -> None:
    payload = AgentConfigRequest(strict_model=True)
    with pytest.raises(InvalidInputError) as caught:
        agents_api._check_config(None, SimpleNamespace(id="t1"), NO_LLM_AGENT, payload, None)
    assert caught.value.status_code == 422
    assert "Always use the model I picked" in caught.value.message
    # Off is always fine.
    agents_api._check_config(None, SimpleNamespace(id="t1"), NO_LLM_AGENT, AgentConfigRequest(strict_model=False), None)


def _configure(monkeypatch: pytest.MonkeyPatch, record: SimpleNamespace, payload: AgentConfigRequest,
               current: Any = None) -> tuple[_Db, list[Any]]:  # noqa: ANN401
    """Run the configure route with everything outside this file faked."""
    from app.orchestration import runner
    from app.services import audit

    versions: list[Any] = []
    monkeypatch.setattr(agents_api, "_record", lambda db, tenant_id, slug: record)
    monkeypatch.setattr(runner, "build_context", lambda *a, **k: None)
    monkeypatch.setattr(agents_api, "lookup_agent", lambda slug: SimpleNamespace(
        spec=SimpleNamespace(requires_llm=slug == LLM_AGENT, connector_use=()),
        validate_scope=lambda scope: "",
        on_configure=lambda ctx, config: None,
    ))
    monkeypatch.setattr(audit, "record_user_action", lambda db, **kw: None)
    monkeypatch.setattr(agents_api, "record_config_version",
                        lambda db, org, rec, *, actor, summary=None: versions.append(dict(rec.settings or {})))
    monkeypatch.setattr(views, "guide_env", lambda db, tenant_id: None)
    monkeypatch.setattr(views, "agent_out", lambda rec, **kw: SimpleNamespace(warnings=[], settings=rec.settings))
    db = _Db()
    agents_api.configure(record.slug, payload, current or _current(), db, "203.0.113.9")
    return db, versions


def test_configure_saves_strict_on_then_off_and_records_each_version(
    monkeypatch: pytest.MonkeyPatch, offline: None
) -> None:
    record = _record()
    db, versions = _configure(monkeypatch, record, AgentConfigRequest(llm_connector="openai", strict_model=True))
    assert record.settings.get(STRICT_MODEL_SETTING) is True and record.llm_connector == "openai"
    assert db.commits == 1 and versions[-1].get(STRICT_MODEL_SETTING) is True

    # Saving without the field keeps it.
    _configure(monkeypatch, record, AgentConfigRequest(llm_connector="openai"))
    assert record.settings.get(STRICT_MODEL_SETTING) is True

    _, versions = _configure(monkeypatch, record, AgentConfigRequest(llm_connector="openai", strict_model=False))
    assert STRICT_MODEL_SETTING not in record.settings and STRICT_MODEL_SETTING not in versions[-1]


def test_configure_refuses_strict_for_a_reporting_agent_and_saves_nothing(
    monkeypatch: pytest.MonkeyPatch, offline: None
) -> None:
    record = _record(NO_LLM_AGENT)
    with pytest.raises(InvalidInputError):
        _configure(monkeypatch, record, AgentConfigRequest(strict_model=True))
    assert record.settings == {} and record.configured is False


def test_a_view_only_role_cannot_change_it(monkeypatch: pytest.MonkeyPatch, offline: None) -> None:
    record = _record()
    with pytest.raises(ForbiddenError):
        _configure(monkeypatch, record, AgentConfigRequest(llm_connector="openai", strict_model=True),
                   current=_current("approver"))
    assert record.settings == {}


# ── Reading it back ─────────────────────────────────────────────────────────
def test_the_agent_card_shows_the_switch_only_where_it_applies() -> None:
    assert views.strict_model_on(_record(**{STRICT_MODEL_SETTING: True})) is True
    # Anything but a real True reads as off, as the runner reads it.
    assert views.strict_model_on(_record(**{STRICT_MODEL_SETTING: "true"})) is False
    assert views.strict_model_on(_record()) is False

    on = views.agent_out(_record(**{STRICT_MODEL_SETTING: True}), can_write=True)
    assert on.strict_model is True
    stale = views.agent_out(_record(NO_LLM_AGENT, **{STRICT_MODEL_SETTING: True}), can_write=True)
    assert stale.strict_model is False


def test_reset_clears_the_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services import audit

    record = _record(**{STRICT_MODEL_SETTING: True})
    monkeypatch.setattr(agents_api, "_record", lambda db, tenant_id, slug: record)
    monkeypatch.setattr(audit, "record_user_action", lambda db, **kw: None)
    monkeypatch.setattr(agents_api, "record_config_version", lambda *a, **k: None)
    monkeypatch.setattr(views, "guide_env", lambda db, tenant_id: None)
    monkeypatch.setattr(views, "agent_out", lambda rec, **kw: rec)
    agents_api.reset_config(LLM_AGENT, _current(), _Db(), "203.0.113.9")
    assert STRICT_MODEL_SETTING not in record.settings
