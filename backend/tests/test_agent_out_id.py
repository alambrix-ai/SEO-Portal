"""The agent card carries the agent's record id (``AgentOut.id``).

Agent settings versions are kept under the record id, not the slug
(``agents.record_config_version``), so the agent page needs the id to ask
``GET /history/agent_config?subject=<id>`` for that agent's versions. These
tests pin the id on the card to the subject a settings change is recorded
under, and check that the history list for it returns those versions and
nothing from another agent or workspace.

The database tests skip without a database (``requires_db``).
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from app.api.v1 import agents as agents_api, history as history_api
from app.models.agent import AgentRecord
from app.schemas.workspace import AutonomyRequest
from app.services import audit, history, views
from app.services.history import store
from tests.conftest import requires_db

AGENT_ID = "aaaaaaaa-0000-4000-8000-000000000001"
OTHER_ID = "bbbbbbbb-0000-4000-8000-000000000002"
SLUG = "seo_article_writer"


# ── Fakes ───────────────────────────────────────────────────────────────────
def _current(role: str = "admin") -> Any:  # noqa: ANN401
    from app.api.deps import CurrentUser

    user = SimpleNamespace(id="u1", name="Asha", email="asha@example.com", role=role, tenant_id="t1")
    org = SimpleNamespace(id="t1", name="Acme", settings={}, global_autonomy=False)
    return CurrentUser(user=user, organization=org, claims={})


def _agent(agent_id: str = AGENT_ID) -> SimpleNamespace:
    return SimpleNamespace(
        id=agent_id, tenant_id="t1", slug=SLUG, name="SEO Article Writer", settings={},
        schedule="Daily", scope="", notify_channel="None", llm_connector="", max_actions_per_day=5,
        autonomy=False, configured=True, status="paused", next_run_at=None,
        category="Content & Social", description="", metric_label="", last_error="", actions_today=0,
        last_run_at=None, consecutive_failures=0,
    )


class _Db:
    def __init__(self) -> None:
        self.commits = 0

    def flush(self) -> None:
        return None

    def commit(self) -> None:
        self.commits += 1

    def begin_nested(self) -> Any:  # noqa: ANN401
        import contextlib

        return contextlib.nullcontext()


@pytest.fixture
def versions(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """An in-memory history: record_version appends, history() filters by subject."""
    kept: list[dict[str, Any]] = []

    def record(db: Any, org: Any, kind: str, subject: str, payload: dict, **kw: Any) -> Any:  # noqa: ANN401
        kept.append({"id": f"v{len(kept) + 1}", "kind": str(kind), "subject_id": subject, "tenant": org.id})
        return SimpleNamespace(version=len(kept))

    def listed(db: Any, org: Any, kind: str, limit: int, *, subject: str | None = None, cursor: Any = None) -> dict:  # noqa: ANN401
        entries = [
            e for e in kept
            if e["kind"] == kind and e["tenant"] == org.id and (subject is None or e["subject_id"] == subject)
        ]
        return {"kind": kind, "subject_id": subject, "restorable": True, "entries": entries[:limit],
                "next_cursor": None, "started_at": None}

    monkeypatch.setattr(history, "record_version", record)
    monkeypatch.setattr(history, "history", listed)
    monkeypatch.setattr(history, "ensure_baseline", lambda db, org, kind: False)
    monkeypatch.setattr(store, "plan_cap", lambda db, org: None)
    monkeypatch.setattr(audit, "record_user_action", lambda db, **kw: None)
    monkeypatch.setattr(views, "guide_env", lambda db, tenant_id: None)
    return kept


def _list(subject: str | None) -> dict[str, Any]:
    return history_api.list_history("agent_config", _current(), _Db(), limit=30, subject=subject, cursor=None)


# ── The card ────────────────────────────────────────────────────────────────
def test_the_agent_card_carries_the_record_id() -> None:
    out = views.agent_out(_agent(), can_write=True)
    assert out.id == AGENT_ID
    assert out.model_dump()["id"] == AGENT_ID


def test_an_unsaved_record_has_an_empty_id_not_an_error() -> None:
    record = AgentRecord(
        slug=SLUG, name="SEO Article Writer", category="Content & Social", description="",
        status="paused", autonomy=False, metric_label="", schedule="Daily", scope="",
        notify_channel="None", llm_connector="", max_actions_per_day=5, configured=False,
        last_error="", actions_today=0, settings={},
    )
    assert views.agent_out(record, can_write=False).id == ""


# ── Card id → history subject ───────────────────────────────────────────────
def test_the_card_id_lists_the_versions_a_settings_change_recorded(
    monkeypatch: pytest.MonkeyPatch, versions: list[dict[str, Any]]
) -> None:
    record = _agent()
    monkeypatch.setattr(agents_api, "_record", lambda db, tenant_id, slug: record)
    agents_api.set_autonomy(SLUG, AutonomyRequest(autonomous=True), _current(), _Db(), "203.0.113.9")
    agents_api.set_autonomy(SLUG, AutonomyRequest(autonomous=False), _current(), _Db(), "203.0.113.9")

    card = agents_api.get_agent(SLUG, _current(), _Db())
    assert card.id == AGENT_ID
    listed = _list(card.id)
    assert [e["id"] for e in listed["entries"]] == ["v1", "v2"]
    assert all(e["subject_id"] == card.id for e in listed["entries"])


def test_the_slug_or_another_agents_id_lists_nothing(
    monkeypatch: pytest.MonkeyPatch, versions: list[dict[str, Any]]
) -> None:
    record = _agent()
    monkeypatch.setattr(agents_api, "_record", lambda db, tenant_id, slug: record)
    agents_api.set_autonomy(SLUG, AutonomyRequest(autonomous=True), _current(), _Db(), "203.0.113.9")
    # Another agent's version in the same workspace stays under its own id.
    versions.append({"id": "v9", "kind": "agent_config", "subject_id": OTHER_ID, "tenant": "t1"})

    assert _list(SLUG)["entries"] == []
    assert [e["id"] for e in _list(AGENT_ID)["entries"]] == ["v1"]
    assert [e["id"] for e in _list(OTHER_ID)["entries"]] == ["v9"]


def test_a_role_without_agents_view_cannot_list_agent_versions(versions: list[dict[str, Any]]) -> None:
    from app.core.exceptions import ForbiddenError

    with pytest.raises(ForbiddenError):
        history_api.list_history(
            "agent_config", _current("client"), _Db(), limit=30, subject=AGENT_ID, cursor=None
        )


# ── Database ────────────────────────────────────────────────────────────────
@requires_db
def test_db_card_id_lists_this_agents_versions_only(clean_db, org, second_org) -> None:  # noqa: ANN001
    from sqlalchemy import select

    from app.api.deps import CurrentUser
    from app.db.session import bind_tenant
    from app.models.workspace import User

    bind_tenant(clean_db, org.id)
    user = clean_db.query(User).filter(User.tenant_id == org.id).first()
    admin = CurrentUser(user=user, organization=org, claims={})
    slug = "technical_seo_auditor"
    agents_api.set_autonomy(slug, AutonomyRequest(autonomous=True), admin, clean_db, "ip")
    agents_api.set_autonomy(slug, AutonomyRequest(autonomous=False), admin, clean_db, "ip")

    card = agents_api.get_agent(slug, admin, clean_db)
    record = clean_db.execute(
        select(AgentRecord).where(AgentRecord.tenant_id == org.id, AgentRecord.slug == slug)
    ).scalar_one()
    assert card.id == record.id

    listed = history_api.list_history("agent_config", admin, clean_db, limit=30, subject=card.id, cursor=None)
    recorded = [e for e in listed["entries"] if e.get("source") != "baseline"]
    assert len(recorded) >= 2
    assert all(e.get("subject_id", card.id) == card.id for e in listed["entries"])
    # The slug is not a subject: nothing is listed under it.
    by_slug = history_api.list_history("agent_config", admin, clean_db, limit=30, subject=slug, cursor=None)
    assert by_slug["entries"] == []

    # Another workspace asking for this agent's id sees nothing.
    bind_tenant(clean_db, second_org.id)
    other_user = clean_db.query(User).filter(User.tenant_id == second_org.id).first()
    other = CurrentUser(user=other_user, organization=second_org, claims={})
    foreign = history_api.list_history("agent_config", other, clean_db, limit=30, subject=card.id, cursor=None)
    assert foreign["entries"] == []
