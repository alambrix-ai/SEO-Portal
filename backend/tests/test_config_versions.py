"""Versions of agent settings and of the workspace policy, and restoring them.

* ``agent_config``: recorded on every settings save, autonomy change,
  practice switch and reset; restored through ``agents.restore_config``.
* ``workspace_policy``: "Review before publishing" plus the agents' switch,
  saved together by ``PUT /onboarding/policy`` (one transaction), recorded
  there, on onboarding guardrail saves and on the global switch; restored
  through ``onboarding.restore_policy``.

The pure parts run with fakes; tenant isolation and the real transaction are
``requires_db``.
"""
from __future__ import annotations

import contextlib
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from app.agents.base import safety
from app.agents.base.choice import CHOICES_KEY
from app.api.v1 import agents as agents_api, onboarding as onboarding_api
from app.core.exceptions import ForbiddenError, InvalidInputError, NotFoundError
from app.orchestration.runner import STRICT_MODEL_SETTING
from app.schemas.workspace import AutonomyRequest, LlmConnectorOut, NotifyChannelOut, WorkspacePolicyRequest
from app.services import audit, history, views
from app.services.history import config as history_config, store
from tests.conftest import requires_db

AGENT_ID = "aaaaaaaa-0000-4000-8000-000000000001"
SLUG = "seo_article_writer"
NOW = datetime(2026, 10, 21, tzinfo=UTC)


# ── Fakes ───────────────────────────────────────────────────────────────────
def _current(role: str = "admin", *, org: Any = None) -> Any:  # noqa: ANN401
    from app.api.deps import CurrentUser

    user = SimpleNamespace(id="u1", name="Asha", email="asha@example.com", role=role, tenant_id="t1")
    org = org or SimpleNamespace(id="t1", name="Acme", settings={}, global_autonomy=False)
    return CurrentUser(user=user, organization=org, claims={})


def _agent(**settings: Any) -> SimpleNamespace:  # noqa: ANN401
    return SimpleNamespace(
        id=AGENT_ID, tenant_id="t1", slug=SLUG, name="SEO Article Writer", settings=dict(settings),
        schedule="Daily", scope="", notify_channel="None", llm_connector="openai", max_actions_per_day=5,
        autonomy=False, configured=True, status="paused", next_run_at=None,
    )


class _Result:
    def __init__(self, rows: list[Any]) -> None:
        self.rows = rows

    def scalars(self) -> list[Any]:
        return list(self.rows)

    def scalar_one_or_none(self) -> Any:  # noqa: ANN401
        return self.rows[0] if self.rows else None


class _Db:
    """Enough of a session for these routes: one transaction, counted commits."""

    def __init__(self, *, records: list[Any] | None = None, state: Any = None) -> None:  # noqa: ANN401
        self.records = records or []
        self.state = state
        self.commits = 0
        self.flushes = 0

    def execute(self, statement: Any) -> _Result:  # noqa: ANN401
        return _Result(self.records)

    def scalar(self, statement: Any) -> Any:  # noqa: ANN401 - current_policy's guardrail read
        return getattr(self.state, "guardrail", None)

    def flush(self) -> None:
        self.flushes += 1

    def commit(self) -> None:
        self.commits += 1

    def begin_nested(self) -> contextlib.AbstractContextManager[None]:
        return contextlib.nullcontext()


@pytest.fixture
def recorded(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Every history.record_version call, instead of writing it."""
    calls: list[dict[str, Any]] = []

    def record(db: Any, org: Any, kind: str, subject: str, payload: dict, *, actor: Any, summary: Any = None,  # noqa: ANN401
               **kw: Any) -> SimpleNamespace:  # noqa: ANN401
        calls.append({"kind": str(kind), "subject": subject, "payload": json.loads(json.dumps(payload)),
                      "actor": getattr(actor, "id", None), "summary": summary})
        return SimpleNamespace(version=len(calls))

    monkeypatch.setattr(history, "record_version", record)
    return calls


@pytest.fixture
def audited(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    monkeypatch.setattr(audit, "record_user_action", lambda db, **kw: entries.append(kw))
    return entries


@pytest.fixture
def offline(monkeypatch: pytest.MonkeyPatch) -> None:
    """The agent and its options without a database or connectors."""
    from app.orchestration import runner

    monkeypatch.setattr(agents_api, "_llm_options",
                        lambda db, org: [LlmConnectorOut(slug="openai", name="OpenAI", available=True)])
    monkeypatch.setattr(agents_api, "_notify_options", lambda db, org: [NotifyChannelOut(value="None")])
    monkeypatch.setattr(agents_api, "lookup_agent", lambda slug: SimpleNamespace(
        spec=SimpleNamespace(requires_llm=True, connector_use=()),
        validate_scope=lambda scope: "" if len(scope) < 100 else "Too long",
        on_configure=lambda ctx, config: None,
    ))
    monkeypatch.setattr(runner, "build_context", lambda *a, **k: None)


# ── The agent_config document ───────────────────────────────────────────────
def test_config_document_holds_configuration_but_never_agent_state() -> None:
    record = _agent(**{
        STRICT_MODEL_SETTING: True,
        safety.PRACTICE_SETTING: False,
        CHOICES_KEY: {"publish_content": ["wordpress"]},
        "_cursor": "page-40",  # working state an agent keeps for itself
    })
    document = agents_api.config_document(record)
    assert document["schedule"] == "Daily" and document["llm_connector"] == "openai"
    assert document["autonomy"] is False and document["max_actions_per_day"] == 5
    assert document["settings"] == {
        STRICT_MODEL_SETTING: True,
        safety.PRACTICE_SETTING: False,
        CHOICES_KEY: {"publish_content": ["wordpress"]},
    }
    # The shape history compares and shows field by field.
    assert {spec.key for spec in history_config.FIELDS["agent_config"]} <= set(document)


def test_record_config_version_uses_the_agent_id_and_never_fails_the_save(
    monkeypatch: pytest.MonkeyPatch, recorded: list[dict[str, Any]]
) -> None:
    record = _agent()
    agents_api.record_config_version(_Db(), SimpleNamespace(id="t1"), record, actor=SimpleNamespace(id="u1"))
    assert recorded[0]["kind"] == "agent_config" and recorded[0]["subject"] == AGENT_ID

    def broken(*a: Any, **k: Any) -> None:  # noqa: ANN401
        raise RuntimeError("history table missing")

    monkeypatch.setattr(history, "record_version", broken)
    assert agents_api.record_config_version(_Db(), SimpleNamespace(id="t1"), record, actor=None) is None


def test_each_autonomy_change_records_a_version(
    monkeypatch: pytest.MonkeyPatch, recorded: list[dict[str, Any]], audited: list[dict[str, Any]]
) -> None:
    record = _agent()
    monkeypatch.setattr(agents_api, "_record", lambda db, tenant_id, slug: record)
    db = _Db()
    agents_api.set_autonomy(SLUG, AutonomyRequest(autonomous=True), _current(), db, "203.0.113.9")
    assert db.commits == 1
    assert recorded[-1]["kind"] == "agent_config" and recorded[-1]["payload"]["autonomy"] is True


def test_the_global_switch_records_the_policy_and_every_agent(
    recorded: list[dict[str, Any]], audited: list[dict[str, Any]]
) -> None:
    first, second = _agent(), _agent()
    second.id = "aaaaaaaa-0000-4000-8000-000000000002"
    db = _Db(records=[first, second], state=SimpleNamespace(guardrail="hybrid"))
    current = _current()
    agents_api.set_global_autonomy(AutonomyRequest(autonomous=True), current, db, "203.0.113.9")
    assert db.commits == 1 and current.organization.global_autonomy is True
    kinds = [c["kind"] for c in recorded]
    assert kinds == ["workspace_policy", "agent_config", "agent_config"]
    assert recorded[0]["payload"] == {"guardrail": "hybrid", "autonomous": True} and recorded[0]["subject"] == ""


# ── PUT /onboarding/policy ──────────────────────────────────────────────────
def test_policy_request_is_strict() -> None:
    assert WorkspacePolicyRequest(guardrail="hybrid", autonomous=True).confirm is False
    for bad in (
        {"guardrail": "yolo", "autonomous": True},
        {"guardrail": "full", "autonomous": "yes"},
        {"guardrail": "full", "autonomous": True, "confirm": "true"},
        {"guardrail": "human", "autonomous": False, "is_admin": True},
        {"autonomous": True},
    ):
        with pytest.raises(ValidationError):
            WorkspacePolicyRequest.model_validate(bad)


def _policy_db(monkeypatch: pytest.MonkeyPatch, guardrail: str = "hybrid") -> tuple[_Db, SimpleNamespace]:
    state = SimpleNamespace(guardrail=guardrail, tenant_id="t1")
    monkeypatch.setattr(onboarding_api, "_state", lambda db, tenant_id: state)
    return _Db(records=[_agent()], state=state), state


@pytest.mark.parametrize("role", ["manager", "seo", "approver", "client"])
def test_only_an_admin_can_set_the_policy(monkeypatch: pytest.MonkeyPatch, role: str) -> None:
    db, state = _policy_db(monkeypatch)
    current = _current(role)
    with pytest.raises(ForbiddenError):
        onboarding_api.set_policy(WorkspacePolicyRequest(guardrail="human", autonomous=False), current, db, "ip")
    assert state.guardrail == "hybrid" and db.commits == 0


def test_publish_automatically_without_confirm_is_refused_and_changes_nothing(
    monkeypatch: pytest.MonkeyPatch, recorded: list[dict[str, Any]], audited: list[dict[str, Any]]
) -> None:
    db, state = _policy_db(monkeypatch)
    current = _current()
    with pytest.raises(InvalidInputError) as caught:
        onboarding_api.set_policy(WorkspacePolicyRequest(guardrail="full", autonomous=True), current, db, "ip")
    assert caught.value.status_code == 422 and caught.value.details["needs_confirmation"] is True
    assert state.guardrail == "hybrid" and current.organization.global_autonomy is False
    assert db.commits == 0 and recorded == [] and audited == []


def test_policy_save_sets_both_records_a_version_and_audits_in_one_commit(
    monkeypatch: pytest.MonkeyPatch, recorded: list[dict[str, Any]], audited: list[dict[str, Any]]
) -> None:
    db, state = _policy_db(monkeypatch)
    current = _current()
    out = onboarding_api.set_policy(
        WorkspacePolicyRequest(guardrail="full", autonomous=True, confirm=True), current, db, "203.0.113.9"
    )
    assert state.guardrail == "full" and current.organization.global_autonomy is True
    assert db.records[0].autonomy is True and out.agents_updated == 1
    assert db.commits == 1 and out.version == 1 and out.guardrail == "full" and out.autonomous is True
    assert recorded[0] == {"kind": "workspace_policy", "subject": "", "actor": "u1", "summary": None,
                           "payload": {"guardrail": "full", "autonomous": True}}
    assert recorded[1]["kind"] == "agent_config"
    entry = audited[0]
    assert entry["module"] == "admin" and entry["context"]["guardrail"] == "full"
    assert entry["context"]["autonomous"] is True and entry["ip_address"] == "203.0.113.9"


def test_resaving_the_same_switch_leaves_each_agents_own_choice(
    monkeypatch: pytest.MonkeyPatch, recorded: list[dict[str, Any]], audited: list[dict[str, Any]]
) -> None:
    db, state = _policy_db(monkeypatch)
    current = _current()
    current.organization.global_autonomy = True
    db.records[0].autonomy = False  # this agent was set to always ask
    out = onboarding_api.set_policy(WorkspacePolicyRequest(guardrail="human", autonomous=True), current, db, "ip")
    assert out.agents_updated == 0 and db.records[0].autonomy is False and state.guardrail == "human"


def test_policy_save_is_atomic_when_a_later_step_fails(
    monkeypatch: pytest.MonkeyPatch, recorded: list[dict[str, Any]]
) -> None:
    db, _ = _policy_db(monkeypatch)

    def audit_down(db: Any, **kw: Any) -> None:  # noqa: ANN401
        raise RuntimeError("audit log unavailable")

    monkeypatch.setattr(audit, "record_user_action", audit_down)
    with pytest.raises(RuntimeError):
        onboarding_api.set_policy(
            WorkspacePolicyRequest(guardrail="human", autonomous=False), _current(), db, "ip"
        )
    # Nothing was committed, so the request's session rolls both halves back.
    assert db.commits == 0


def test_an_onboarding_guardrail_change_records_a_policy_version(
    monkeypatch: pytest.MonkeyPatch, recorded: list[dict[str, Any]]
) -> None:
    from app.schemas.workspace import OnboardingUpdateRequest

    db, state = _policy_db(monkeypatch)
    monkeypatch.setattr(onboarding_api, "_out", lambda state, db=None: state)
    onboarding_api.update_state(OnboardingUpdateRequest(guardrail="human"), _current(), db)
    assert recorded[0]["payload"] == {"guardrail": "human", "autonomous": False} and db.commits == 1
    # The same value again records nothing new.
    onboarding_api.update_state(OnboardingUpdateRequest(guardrail="human"), _current(), db)
    assert len(recorded) == 1


# ── Restoring ───────────────────────────────────────────────────────────────
class _FakeCipher:
    def encrypt_json(self, document: Any, *, context: str) -> str:  # noqa: ANN401
        return "enc:" + json.dumps(document)

    def decrypt_json(self, token: str, *, context: str, default: Any = None) -> Any:  # noqa: ANN401
        return json.loads(token[4:]) if token.startswith("enc:") else default


def _row(kind: str, document: dict[str, Any], *, version: int, subject: str = "", tenant: str = "t1") -> Any:  # noqa: ANN401
    return SimpleNamespace(id=f"r{version}", tenant_id=tenant, kind=kind, subject_id=subject, version=version,
                           payload_encrypted="enc:" + json.dumps(document), items={}, item_count=0,
                           created_at=NOW, source="saved", label="", created_by_name="", run_id="")


@pytest.fixture
def snapshots(monkeypatch: pytest.MonkeyPatch, audited: list[dict[str, Any]]) -> dict[str, Any]:
    """history.restore over in-memory versions."""
    state: dict[str, Any] = {"rows": {}, "latest": None, "written": []}
    monkeypatch.setattr(history, "_cipher", lambda org: _FakeCipher())
    monkeypatch.setattr(store, "latest_of", lambda db, tenant, kind, subject: state["latest"])

    def get(db: Any, tenant: str, kind: str, entry_id: str) -> Any:  # noqa: ANN401
        row = state["rows"].get(entry_id)
        if row is None or row.tenant_id != tenant or row.kind != kind:
            raise NotFoundError("That history entry was not found")
        return row

    def record(db: Any, **kw: Any) -> SimpleNamespace:  # noqa: ANN401
        state["written"].append(kw)
        return SimpleNamespace(id="new", version=99, kind=kw["kind"], subject_id=kw.get("subject_id", ""),
                               source=kw["source"], label=kw["label"], run_id="", created_by_name="",
                               actor_type=kw.get("actor_type", ""), agent_slug="", model="",
                               created_at=NOW, items={}, item_count=0)

    monkeypatch.setattr(store, "get", get)
    monkeypatch.setattr(store, "record", record)
    return state


def test_policy_restore_to_publish_automatically_needs_confirm_then_sets_both(
    monkeypatch: pytest.MonkeyPatch, snapshots: dict[str, Any], recorded: list[dict[str, Any]]
) -> None:
    assert history.RESTORERS["workspace_policy"] is onboarding_api.restore_policy
    org = SimpleNamespace(id="t1", settings={}, global_autonomy=False)
    db, state = _policy_db(monkeypatch)
    snapshots["rows"]["r1"] = _row("workspace_policy", {"guardrail": "full", "autonomous": True}, version=1)
    snapshots["latest"] = _row("workspace_policy", {"guardrail": "hybrid", "autonomous": False}, version=2)
    user = SimpleNamespace(id="u1", name="Asha")

    with pytest.raises(InvalidInputError) as caught:
        history.restore(db, org, "workspace_policy", "r1", user=user)
    assert caught.value.status_code == 422 and caught.value.details["needs_confirmation"] is True
    assert state.guardrail == "hybrid" and org.global_autonomy is False and snapshots["written"] == []

    out = history.restore(db, org, "workspace_policy", "r1", user=user, confirm=True)
    assert state.guardrail == "full" and org.global_autonomy is True and db.records[0].autonomy is True
    assert out["restored_version"] == 1 and snapshots["written"][0]["source"] == "restored"
    assert recorded and recorded[0]["kind"] == "agent_config"  # the agent's own switch moved with it


@pytest.mark.parametrize(
    "document",
    [{"guardrail": "everything", "autonomous": True}, {"guardrail": "human", "autonomous": "yes"}, {}],
)
def test_a_policy_version_that_fails_todays_rules_is_refused(
    monkeypatch: pytest.MonkeyPatch, document: dict[str, Any]
) -> None:
    db, state = _policy_db(monkeypatch)
    org = SimpleNamespace(id="t1", global_autonomy=False)
    with pytest.raises(InvalidInputError):
        onboarding_api.restore_policy(db, org, "", document, user=None)
    assert state.guardrail == "hybrid" and org.global_autonomy is False


def test_a_policy_version_with_a_subject_is_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    db, _ = _policy_db(monkeypatch)
    with pytest.raises(NotFoundError):
        onboarding_api.restore_policy(db, SimpleNamespace(id="t1"), "x", {"guardrail": "human", "autonomous": False},
                                      user=None)


def test_agent_config_restore_sets_the_fields_back_and_keeps_agent_state(
    monkeypatch: pytest.MonkeyPatch, offline: None
) -> None:
    from app.agents.base.plain import guide_for

    assert history.RESTORERS["agent_config"] is agents_api.restore_config
    declared = [spec.key for spec in (guide_for(SLUG).settings if guide_for(SLUG) else ())]
    record = _agent(**{STRICT_MODEL_SETTING: True, "_cursor": "page-40"},
                    **({declared[0]: "kept from today"} if declared else {}))
    record.schedule, record.autonomy = "Hourly", True
    monkeypatch.setattr(agents_api, "_record", lambda db, tenant_id, slug: record)
    old = {"schedule": "Weekly", "scope": "", "notify_channel": "None", "llm_connector": "openai",
           "max_actions_per_day": 3, "autonomy": False, "settings": {}}

    out = agents_api.restore_config(_Db(records=[record]), SimpleNamespace(id="t1"), AGENT_ID, old, user=None)
    assert record.schedule == "Weekly" and record.max_actions_per_day == 3 and record.autonomy is False
    assert STRICT_MODEL_SETTING not in record.settings  # the version had it off
    assert record.settings.get("_cursor") == "page-40"  # agent state is not configuration
    if declared:
        assert declared[0] not in record.settings  # set since that version, so removed
    assert out == agents_api.config_document(record)

    agents_api.restore_config(
        _Db(records=[record]), SimpleNamespace(id="t1"), AGENT_ID,
        {**old, "settings": {STRICT_MODEL_SETTING: True}}, user=None,
    )
    assert record.settings.get(STRICT_MODEL_SETTING) is True


@pytest.mark.parametrize(
    "change",
    [{"schedule": "Every minute"}, {"max_actions_per_day": 0}, {"max_actions_per_day": "lots"},
     {"notify_channel": "Carrier pigeon"}, {"scope": "x" * 300}, {"settings": {"no_such_setting": "1"}}],
)
def test_an_agent_version_that_fails_todays_rules_is_refused(
    monkeypatch: pytest.MonkeyPatch, offline: None, change: dict[str, Any]
) -> None:
    record = _agent()
    monkeypatch.setattr(agents_api, "_record", lambda db, tenant_id, slug: record)
    old = {"schedule": "Weekly", "scope": "", "notify_channel": "None", "llm_connector": "openai",
           "max_actions_per_day": 3, "autonomy": True, "settings": {}}
    document = {**old, **change}
    if "settings" in change:
        # An undeclared key in a stored version is ignored, never written.
        agents_api.restore_config(_Db(records=[record]), SimpleNamespace(id="t1"), AGENT_ID, document, user=None)
        assert "no_such_setting" not in record.settings
        return
    with pytest.raises(InvalidInputError):
        agents_api.restore_config(_Db(records=[record]), SimpleNamespace(id="t1"), AGENT_ID, document, user=None)
    assert record.schedule == "Daily" and record.autonomy is False


def test_another_workspaces_agent_is_not_found(offline: None) -> None:
    # The tenant-filtered lookup finds nothing for an agent of another workspace.
    with pytest.raises(NotFoundError):
        agents_api.restore_config(_Db(records=[]), SimpleNamespace(id="t2"), AGENT_ID,
                                  {"schedule": "Daily"}, user=None)


def test_another_workspaces_version_is_not_found(snapshots: dict[str, Any]) -> None:
    snapshots["rows"]["r1"] = _row("agent_config", {"schedule": "Daily"}, version=1, subject=AGENT_ID,
                                   tenant="someone-else")
    with pytest.raises(NotFoundError):
        history.restore(None, SimpleNamespace(id="t1"), "agent_config", "r1", user=None)
    snapshots["rows"]["r2"] = _row("workspace_policy", {"guardrail": "human", "autonomous": False}, version=2,
                                   tenant="someone-else")
    with pytest.raises(NotFoundError):
        history.restore(None, SimpleNamespace(id="t1"), "workspace_policy", "r2", user=None, confirm=True)


# ── With a database ─────────────────────────────────────────────────────────
def _admin(db: Any, org: Any) -> Any:  # noqa: ANN401
    from app.api.deps import CurrentUser
    from app.models.workspace import User

    user = db.query(User).filter(User.tenant_id == org.id).first()
    return CurrentUser(user=user, organization=org, claims={})


def _policy_now(db: Any, org: Any) -> dict[str, Any]:  # noqa: ANN401
    db.expire_all()
    return history_config.current_policy(db, org)


@requires_db
def test_db_policy_save_is_one_transaction(clean_db, org, monkeypatch) -> None:  # noqa: ANN001
    from sqlalchemy import func, select

    from app.db.session import bind_tenant
    from app.models.history import AuditSnapshot

    bind_tenant(clean_db, org.id)
    onboarding_api.set_policy(WorkspacePolicyRequest(guardrail="human", autonomous=False), _admin(clean_db, org),
                              clean_db, "203.0.113.9")
    assert _policy_now(clean_db, org) == {"guardrail": "human", "autonomous": False}

    def audit_down(db: Any, **kw: Any) -> None:  # noqa: ANN401
        raise RuntimeError("audit log unavailable")

    monkeypatch.setattr(audit, "record_user_action", audit_down)
    with pytest.raises(RuntimeError):
        onboarding_api.set_policy(WorkspacePolicyRequest(guardrail="hybrid", autonomous=True),
                                  _admin(clean_db, org), clean_db, "203.0.113.9")
    clean_db.rollback()
    bind_tenant(clean_db, org.id)
    # Neither half was saved, and no version of the failed save exists.
    assert _policy_now(clean_db, org) == {"guardrail": "human", "autonomous": False}
    versions = clean_db.scalar(select(func.count()).select_from(AuditSnapshot).where(
        AuditSnapshot.tenant_id == org.id, AuditSnapshot.kind == "workspace_policy"))
    assert versions == 1


@requires_db
def test_db_policy_versions_restore_and_stay_in_their_workspace(clean_db, org, second_org) -> None:  # noqa: ANN001
    from app.db.session import bind_tenant

    bind_tenant(clean_db, org.id)
    admin = _admin(clean_db, org)
    onboarding_api.set_policy(WorkspacePolicyRequest(guardrail="hybrid", autonomous=True), admin, clean_db, "ip")
    onboarding_api.set_policy(WorkspacePolicyRequest(guardrail="human", autonomous=False), admin, clean_db, "ip")
    listed = history.history(clean_db, org, "workspace_policy", 10)
    first = listed["entries"][-1]
    clean_db.commit()

    history.restore(clean_db, org, "workspace_policy", first["id"], user=admin.user)
    clean_db.commit()
    assert _policy_now(clean_db, org) == {"guardrail": "hybrid", "autonomous": True}

    bind_tenant(clean_db, second_org.id)
    with pytest.raises(NotFoundError):
        history.restore(clean_db, second_org, "workspace_policy", first["id"], user=None, confirm=True)


@requires_db
def test_db_agent_config_restore_and_tenant_isolation(clean_db, org, second_org, monkeypatch) -> None:  # noqa: ANN001
    from sqlalchemy import select

    from app.db.session import bind_tenant
    from app.models.agent import AgentRecord

    monkeypatch.setattr(agents_api, "_llm_options",
                        lambda db, o: [LlmConnectorOut(slug="openai", name="OpenAI", available=True)])
    monkeypatch.setattr(agents_api, "_notify_options", lambda db, o: [NotifyChannelOut(value="None")])
    bind_tenant(clean_db, org.id)
    admin = _admin(clean_db, org)
    record = clean_db.execute(select(AgentRecord).where(
        AgentRecord.tenant_id == org.id, AgentRecord.slug == "technical_seo_auditor")).scalar_one()
    agents_api.set_autonomy(record.slug, AutonomyRequest(autonomous=True), admin, clean_db, "ip")
    agents_api.set_autonomy(record.slug, AutonomyRequest(autonomous=False), admin, clean_db, "ip")
    listed = history.history(clean_db, org, "agent_config", 10, subject=record.id)
    assert len(listed["entries"]) == 2
    on = listed["entries"][-1]
    clean_db.commit()

    history.restore(clean_db, org, "agent_config", on["id"], user=admin.user)
    clean_db.commit()
    clean_db.expire_all()
    assert clean_db.get(AgentRecord, record.id).autonomy is True

    bind_tenant(clean_db, second_org.id)
    with pytest.raises(NotFoundError):
        history.restore(clean_db, second_org, "agent_config", on["id"], user=None)
    with pytest.raises(NotFoundError):
        agents_api.restore_config(clean_db, second_org, record.id, {"schedule": "Daily"}, user=None)


def test_view_helpers_are_unchanged_by_versioning() -> None:
    # The agent card still builds from a record with no versions at all.
    assert views.strict_model_on(_agent()) is False


# ── ModelListRequest bounds (lead request; each address may mean an outbound call) ──
def test_model_list_request_accepts_a_normal_dialog() -> None:
    from app.schemas.workspace import ModelListRequest

    ok = ModelListRequest.model_validate({"values": {"apiKey": "", "baseUrl": "https://llm.example.com/v1"}})
    assert ok.values["baseUrl"].startswith("https://")
    assert ModelListRequest.model_validate({}).values == {}


@pytest.mark.parametrize(
    "body",
    [
        {"values": {f"field{i}": "x" for i in range(21)}},  # too many keys
        {"values": {"baseUrl": "x" * 5000}},  # 5 KB value
        {"values": {"base-url": "x"}},  # key shape
        {"values": {"1key": "x"}},
        {"values": {"k" * 41: "x"}},
        {"values": {"apiKey": 123}},  # strings only
        {"values": {"apiKey": {"nested": "x"}}},
        {"values": {"apiKey": ["x"]}},
        {"values": {}, "extra": True},  # no extra structure
        {"values": "apiKey=x"},
    ],
)
def test_model_list_request_refuses_oversized_or_odd_input(body: dict[str, Any]) -> None:
    from app.schemas.workspace import ModelListRequest

    with pytest.raises(ValidationError):
        ModelListRequest.model_validate(body)
