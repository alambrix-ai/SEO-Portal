"""Approval history and policy wiring (step 6.6).

* ``GET /approvals/history``: decided items, keyset-paginated, filtered by
  status and type, tenant-filtered, with the revision that replaced a
  superseded item.
* ``RevisionOut.job_id`` after "Ask for changes".
* "Publish automatically" needs ``confirm`` on the wizard's ``PUT /onboarding``
  exactly as on ``PUT /onboarding/policy``.

Most tests run against an in-memory session that evaluates the real query's
WHERE clause, ORDER BY and LIMIT, so the tenant filter and the keyset are
exercised without a database. The ``requires_db`` tests repeat the key
checks on PostgreSQL.
"""
from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.orm.evaluator import _EvaluatorCompiler
from sqlalchemy.sql import operators

from app.api.deps import CurrentUser
from app.api.v1 import approvals as approvals_api, onboarding as onboarding_api
from app.core.exceptions import ForbiddenError, InvalidInputError
from app.db.base import new_id, utcnow
from app.models.approval import ApprovalItem, ApprovalStatus
from app.schemas.workspace import (
    OnboardingUpdateRequest,
    RequestChangesRequest,
    WorkspacePolicyRequest,
)
from app.services import approvals
from app.services.creative.pipeline import REVISION_JOB_KEY
from app.services.encryption import Ctx, OrgCipher
from tests.conftest import requires_db

T0 = utcnow() - timedelta(days=2)


# ── An in-memory session that runs the statement's own filter ──────────────
class _Result:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def scalars(self) -> _Result:
        return self

    def first(self) -> Any:  # noqa: ANN401
        return self._rows[0] if self._rows else None

    def __iter__(self):  # noqa: ANN204
        return iter(self._rows)


class EvalDb:
    """Applies a ``select(ApprovalItem)``'s WHERE, ORDER BY and LIMIT in Python.

    Every statement the history issues is read for real: a row another
    tenant owns is returned only if the query itself fails to exclude it.
    """

    def __init__(self, *rows: Any) -> None:  # noqa: ANN401
        self.rows = list(rows)
        self.statements: list[Any] = []
        self.info: dict[str, Any] = {}
        self.commits = 0
        self.flushes = 0

    def execute(self, stmt: Any) -> _Result:  # noqa: ANN401
        self.statements.append(stmt)
        entity = stmt.column_descriptions[0]["entity"]
        rows = [r for r in self.rows if isinstance(r, entity)]
        if stmt.whereclause is not None:
            test = _EvaluatorCompiler(entity).process(stmt.whereclause)
            rows = [r for r in rows if test(r) is True]
        for clause in reversed(stmt._order_by_clauses):
            descending = getattr(clause, "modifier", None) is operators.desc_op
            column = getattr(clause, "element", clause)
            rows.sort(key=lambda r, key=column.key: getattr(r, key), reverse=descending)
        limit = stmt._limit
        return _Result(rows[:limit] if limit is not None else rows)

    def get(self, model: type, row_id: str) -> Any:  # noqa: ANN401
        return next((r for r in self.rows if isinstance(r, model) and r.id == row_id), None)

    def add(self, row: Any) -> None:  # noqa: ANN401
        if getattr(row, "id", None) is None:
            row.id = new_id()
        self.rows.append(row)

    def flush(self) -> None:
        self.flushes += 1

    def commit(self) -> None:
        self.commits += 1


def _org() -> SimpleNamespace:
    org_id = new_id()
    return SimpleNamespace(id=org_id, wrapped_dek=OrgCipher.provision(org_id), settings={})


def _actor(org: SimpleNamespace, role: str = "admin") -> SimpleNamespace:
    return SimpleNamespace(id=new_id(), tenant_id=org.id, name="Asha", role=role)


def _current(org: SimpleNamespace, role: str) -> CurrentUser:
    return CurrentUser(_actor(org, role), org, {})


def _item(
    org: SimpleNamespace,
    *,
    status: str,
    minutes: int,
    type_: str = "Ad Creative",
    payload: dict | None = None,
    target_id: str | None = None,
    note: str = "",
) -> ApprovalItem:
    cipher = OrgCipher.for_org(org)
    decided = status not in (ApprovalStatus.PENDING.value, ApprovalStatus.PRACTISED.value)
    return ApprovalItem(
        id=new_id(),
        tenant_id=org.id,
        type=type_,
        title=f"{type_} {minutes}",
        agent_slug="creative_studio",
        agent_name="Creative Studio",
        target_kind="test_kind",
        target_id=target_id,
        payload_encrypted=cipher.encrypt_json(payload or {}, context=Ctx.APPROVAL_PAYLOAD),
        meta="",
        status=status,
        submitted_at=T0 + timedelta(minutes=minutes - 1),
        decided_at=T0 + timedelta(minutes=minutes) if decided else None,
        decided_by="Asha" if decided else "",
        decided_by_id=None,
        decision_note=note,
    )


@pytest.fixture
def two_tenants() -> SimpleNamespace:
    a, b = _org(), _org()
    mine = [
        _item(a, status="approved", minutes=1, note="Looks good"),
        _item(a, status="rejected", minutes=2, type_="Budget Shift", note="Too much"),
        _item(a, status="superseded", minutes=3, note="Changes requested"),
        _item(a, status="expired", minutes=4),
        _item(a, status="approved", minutes=5, type_="Budget Shift"),
    ]
    noise = [
        _item(a, status="pending", minutes=6),
        _item(a, status="practised", minutes=7),
        _item(b, status="approved", minutes=8),
        _item(b, status="rejected", minutes=9),
    ]
    return SimpleNamespace(a=a, b=b, mine=mine, db=EvalDb(*mine, *noise))


# ── History: paging, filters, isolation ────────────────────────────────────
def test_history_pages_newest_first_without_gaps_or_repeats(two_tenants):
    s = two_tenants
    seen: list[str] = []
    cursor = None
    pages = 0
    while True:
        page = approvals.list_history(s.db, org=s.a, cursor=cursor, limit=2)
        pages += 1
        assert len(page.entries) <= 2
        seen.extend(e.item.id for e in page.entries)
        cursor = page.next_cursor
        if cursor is None:
            break
    assert pages == 3
    # Newest decision first; only this tenant's decided items.
    assert seen == [i.id for i in reversed(s.mine)]


def test_history_never_lists_another_tenants_items(two_tenants):
    s = two_tenants
    page = approvals.list_history(s.db, org=s.b, limit=100)
    assert {e.item.tenant_id for e in page.entries} == {s.b.id}
    assert len(page.entries) == 2
    # The tenant filter is in the query itself, not only applied afterwards.
    compiled = s.db.statements[0].compile().params.values()
    assert s.b.id in compiled and s.a.id not in compiled


def test_history_filters_by_status_and_type(two_tenants):
    s = two_tenants
    rejected = approvals.list_history(s.db, org=s.a, status="rejected")
    assert [e.item.status for e in rejected.entries] == ["rejected"]
    budget = approvals.list_history(s.db, org=s.a, type_="Budget Shift")
    assert len(budget.entries) == 2 and {e.item.type for e in budget.entries} == {"Budget Shift"}
    both = approvals.list_history(s.db, org=s.a, status="approved", type_="Budget Shift")
    assert len(both.entries) == 1


def test_history_page_size_is_capped(two_tenants):
    s = two_tenants
    approvals.list_history(s.db, org=s.a, limit=10_000)
    assert s.db.statements[0]._limit == approvals.HISTORY_MAX_PAGE + 1


@pytest.mark.parametrize("status", ["pending", "practised", "anything"])
def test_history_refuses_undecided_or_unknown_statuses(two_tenants, status):
    with pytest.raises(InvalidInputError):
        approvals.list_history(two_tenants.db, org=two_tenants.a, status=status)


@pytest.mark.parametrize("cursor", ["not-a-cursor", "x" * 500, "MjAyNnxub3QtYS11dWlk"])
def test_a_forged_cursor_gets_a_friendly_422(two_tenants, cursor):
    with pytest.raises(InvalidInputError) as caught:
        approvals.list_history(two_tenants.db, org=two_tenants.a, cursor=cursor)
    assert "history" in str(caught.value.detail if hasattr(caught.value, "detail") else caught.value)


def test_a_superseded_item_links_to_its_revision_and_never_to_another_tenants(two_tenants):
    s = two_tenants
    old = _item(s.a, status="superseded", minutes=20, target_id=new_id(), note="Changes requested")
    revised = _item(s.a, status="pending", minutes=21, target_id=old.target_id,
                    payload={approvals.REVISES_KEY: old.id, approvals.REVISION_KEY: 1})
    # Another workspace's item claiming to revise ours must not be linked.
    forged = _item(s.b, status="pending", minutes=20, target_id=old.target_id,
                   payload={approvals.REVISES_KEY: old.id, approvals.REVISION_KEY: 1})
    db = EvalDb(old, forged, revised)

    [entry] = approvals.list_history(db, org=s.a).entries
    assert entry.item.id == old.id
    assert (entry.replaced_by_id, entry.replaced_by_status) == (revised.id, "pending")

    revised.status, revised.decided_at = "approved", T0 + timedelta(minutes=22)
    page = approvals.list_history(db, org=s.a)
    by_id = {e.item.id: e for e in page.entries}
    assert by_id[revised.id].revises_id == old.id and by_id[revised.id].revision == 1
    assert by_id[old.id].replaced_by_status == "approved"


def test_a_superseded_item_without_its_revision_yet_has_no_link(two_tenants):
    s = two_tenants
    page = approvals.list_history(s.db, org=s.a, status="superseded")
    [entry] = page.entries
    assert entry.replaced_by_id is None and entry.replaced_by_status is None


# ── The route: roles and the response ──────────────────────────────────────
def test_a_role_without_approvals_view_gets_403(two_tenants):
    s = two_tenants
    with pytest.raises(ForbiddenError):
        approvals_api.list_history(_current(s.a, "client"), s.db, None, "", "", 50)
    assert s.db.statements == []


def test_the_route_shows_who_decided_when_and_why(two_tenants):
    s = two_tenants
    out = approvals_api.list_history(_current(s.a, "approver"), s.db, None, "", "", 2)
    assert [i.id for i in out.items] == [s.mine[4].id, s.mine[3].id]
    assert out.next_cursor
    nxt = approvals_api.list_history(_current(s.a, "approver"), s.db, None, "", out.next_cursor, 2)
    labels = {i.status: i.status_label for i in nxt.items}
    assert labels == {"superseded": "Replaced by a revised version", "rejected": "Rejected"}
    rejected = next(i for i in nxt.items if i.status == "rejected")
    assert rejected.decided_by == "Asha" and rejected.note == "Too much" and rejected.decided_at
    # The history card carries no payload.
    assert "payload" not in rejected.model_dump()


def test_history_query_parameters_are_bounded():
    """The route's own limits, as FastAPI enforces them."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api.deps import get_current_user
    from app.db.session import get_db

    org = _org()
    app = FastAPI()
    app.include_router(approvals_api.router)
    app.dependency_overrides[get_current_user] = lambda: _current(org, "admin")
    app.dependency_overrides[get_db] = lambda: EvalDb()
    client = TestClient(app, raise_server_exceptions=False)

    assert client.get("/approvals/history").status_code == 200
    assert client.get("/approvals/history?limit=101").status_code == 422
    assert client.get("/approvals/history?status=pending").status_code == 422
    assert client.get("/approvals/history?type=" + "a" * 41).status_code == 422
    assert client.get("/approvals/history?type=%3Cscript%3E").status_code == 422
    assert client.get("/approvals/history?cursor=" + "a" * 201).status_code == 422


# ── RevisionOut.job_id ─────────────────────────────────────────────────────
def test_request_changes_returns_the_revision_job_id(monkeypatch):
    org = _org()
    item = _item(org, status="pending", minutes=1)
    db = EvalDb(item)
    monkeypatch.setattr(approvals.audit, "record_user_action", lambda db, **kw: None)

    def reviser(db, org, item, payload, request):  # noqa: ANN001, ANN202
        db.info[REVISION_JOB_KEY] = "job-123"
        return approvals.RevisionOutcome()

    monkeypatch.setitem(approvals.REVISERS, "test_kind", reviser)
    out = approvals_api.request_changes(
        item.id, RequestChangesRequest(feedback="Shorter, please"), _current(org, "admin"), db, "127.0.0.1"
    )
    assert out.job_id == "job-123" and db.commits == 1
    # Taken off the session, so it cannot leak into a later response.
    assert REVISION_JOB_KEY not in db.info


def test_request_changes_without_a_job_returns_none(monkeypatch):
    org = _org()
    item = _item(org, status="pending", minutes=1)
    monkeypatch.setattr(approvals.audit, "record_user_action", lambda db, **kw: None)
    monkeypatch.setitem(approvals.REVISERS, "test_kind", lambda *a: approvals.RevisionOutcome())
    out = approvals_api.request_changes(
        item.id, RequestChangesRequest(feedback="Shorter"), _current(org, "admin"), EvalDb(item), "127.0.0.1"
    )
    assert out.job_id is None


# ── "Publish automatically" needs confirm on both endpoints ────────────────
class _PolicyDb:
    def __init__(self) -> None:
        self.commits = 0

    def execute(self, statement: Any) -> Any:  # noqa: ANN401
        return SimpleNamespace(scalars=lambda: [])

    def scalar(self, statement: Any) -> Any:  # noqa: ANN401
        return None

    def flush(self) -> None:
        pass

    def commit(self) -> None:
        self.commits += 1


def _wizard(monkeypatch: pytest.MonkeyPatch) -> tuple[CurrentUser, SimpleNamespace, _PolicyDb]:
    org = SimpleNamespace(id="t1", name="Acme", settings={}, global_autonomy=False, primary_domain="")
    user = SimpleNamespace(id="u1", name="Asha", role="admin", tenant_id="t1")
    state = SimpleNamespace(guardrail="hybrid", tenant_id="t1", step=2, domain="", cms="", completed=False)
    monkeypatch.setattr(onboarding_api, "_state", lambda db, tenant_id: state)
    monkeypatch.setattr(onboarding_api, "_out", lambda state, db=None: state)
    monkeypatch.setattr(onboarding_api, "record_policy_version", lambda *a, **kw: None)
    return CurrentUser(user=user, organization=org, claims={}), state, _PolicyDb()


def test_the_wizard_refuses_publish_automatically_without_confirm(monkeypatch):
    current, state, db = _wizard(monkeypatch)
    with pytest.raises(InvalidInputError) as caught:
        onboarding_api.update_state(
            OnboardingUpdateRequest(guardrail="full", step=3, domain="evil.example"), current, db
        )
    assert caught.value.status_code == 422
    assert caught.value.details == {"needs_confirmation": True, "setting": "full"}
    # Refused before anything changed, the other fields included.
    assert state.guardrail == "hybrid" and state.step == 2 and state.domain == "" and db.commits == 0


def test_the_wizard_saves_publish_automatically_with_confirm(monkeypatch):
    current, state, db = _wizard(monkeypatch)
    onboarding_api.update_state(OnboardingUpdateRequest(guardrail="full", confirm=True), current, db)
    assert state.guardrail == "full" and db.commits == 1


def test_the_wizard_needs_no_confirm_for_the_other_settings(monkeypatch):
    current, state, db = _wizard(monkeypatch)
    onboarding_api.update_state(OnboardingUpdateRequest(guardrail="human"), current, db)
    assert state.guardrail == "human" and db.commits == 1


def test_the_wizard_confirm_must_be_a_real_boolean():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        OnboardingUpdateRequest.model_validate({"guardrail": "full", "confirm": "yes"})


def test_the_policy_endpoint_refuses_publish_automatically_without_confirm(monkeypatch):
    current, state, db = _wizard(monkeypatch)
    with pytest.raises(InvalidInputError) as caught:
        onboarding_api.set_policy(
            WorkspacePolicyRequest(guardrail="full", autonomous=True), current, db, "127.0.0.1"
        )
    assert caught.value.details["needs_confirmation"] is True
    assert state.guardrail == "hybrid" and db.commits == 0


def test_both_endpoints_answer_422_with_needs_confirmation_over_http(monkeypatch):
    """The error shape the console reads, through the real exception handlers."""
    from fastapi.testclient import TestClient

    from app.api.deps import get_current_user
    from app.db.session import get_db
    from app.main import app

    current, state, db = _wizard(monkeypatch)
    app.dependency_overrides[get_current_user] = lambda: current
    app.dependency_overrides[get_db] = lambda: db
    try:
        client = TestClient(app, raise_server_exceptions=False)
        for path, body in (
            ("/api/v1/onboarding", {"guardrail": "full"}),
            ("/api/v1/onboarding/policy", {"guardrail": "full", "autonomous": True}),
        ):
            response = client.put(path, json=body)
            assert response.status_code == 422, path
            # The envelope carries an error's details as "fields".
            assert response.json()["fields"]["needs_confirmation"] is True, path
    finally:
        app.dependency_overrides.pop(get_current_user, None)
        app.dependency_overrides.pop(get_db, None)
    assert state.guardrail == "hybrid" and db.commits == 0


# ── Database ───────────────────────────────────────────────────────────────
@requires_db
def test_history_against_the_database(clean_db, org, second_org):
    from sqlalchemy import select

    from app.db.session import bind_tenant
    from app.models.workspace import User

    bind_tenant(clean_db, org.id)
    actor = clean_db.execute(select(User).where(User.tenant_id == org.id)).scalars().first()
    made = []
    for n in range(3):
        item = approvals.queue_manual(
            clean_db, org=org, type_="Ad Creative" if n else "Budget Shift", title=f"Banner {n}",
            agent_slug="creative_studio", agent_name="Creative Studio", target_kind="test_kind",
            target_id=None, payload={},
        )
        clean_db.flush()
        approvals.reject(clean_db, org=org, actor=actor, item_id=item.id, note=f"No {n}")
        item.decided_at = T0 + timedelta(minutes=n)
        made.append(item)
    clean_db.commit()

    first = approvals.list_history(clean_db, org=org, limit=2)
    assert [e.item.id for e in first.entries] == [made[2].id, made[1].id] and first.next_cursor
    second = approvals.list_history(clean_db, org=org, cursor=first.next_cursor, limit=2)
    assert [e.item.id for e in second.entries] == [made[0].id] and second.next_cursor is None
    assert len(approvals.list_history(clean_db, org=org, type_="Budget Shift").entries) == 1
    assert approvals.list_history(clean_db, org=org, status="approved").entries == []
    clean_db.rollback()

    bind_tenant(clean_db, second_org.id)
    assert approvals.list_history(clean_db, org=second_org).entries == []
    clean_db.rollback()


@requires_db
def test_history_links_a_revision_against_the_database(clean_db, org):
    from sqlalchemy import select

    from app.db.session import bind_tenant
    from app.models.workspace import User

    bind_tenant(clean_db, org.id)
    actor = clean_db.execute(select(User).where(User.tenant_id == org.id)).scalars().first()
    old = approvals.queue_manual(
        clean_db, org=org, type_="Ad Creative", title="Banner", agent_slug="creative_studio",
        agent_name="Creative Studio", target_kind="test_kind", target_id=None, payload={},
    )
    clean_db.commit()

    def reviser(db, org_, item, payload, request):  # noqa: ANN001, ANN202
        new = approvals.queue_revision(db, org=org_, previous=item, title="Banner v2", payload={})
        return approvals.RevisionOutcome(approval_id=new.id)

    approvals.REVISERS["test_kind"] = reviser
    try:
        result = approvals.request_changes(clean_db, org=org, actor=actor, item_id=old.id, feedback="Shorter")
        clean_db.commit()
    finally:
        approvals.REVISERS.pop("test_kind", None)

    [entry] = approvals.list_history(clean_db, org=org).entries
    assert entry.item.id == old.id and entry.item.status == ApprovalStatus.SUPERSEDED.value
    assert entry.replaced_by_id == result.approval_id and entry.replaced_by_status == "pending"
    clean_db.rollback()
