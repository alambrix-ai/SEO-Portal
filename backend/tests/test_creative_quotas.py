"""Creative Studio monthly quotas and the portal-admin override.

The first half is pure: a fake session stands in for Postgres, with a real
``threading.Lock`` playing the advisory lock, so the concurrency test proves
the code holds the lock from the count to the insert. The ``requires_db``
tests repeat the race against real Postgres advisory locks.

Reservations made while a job runs use a short transaction of their own
(``quotas._session_scope``); here that is another fake session on the same
store, which commits (and so releases the lock) when the block ends.
"""
from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError
from sqlalchemy.sql import Select

from app.core.config import settings
from app.db.base import new_id
from app.models.creative import CreativeJob
from app.services.creative import quotas
from app.services.creative.quotas import CreativeLimitsUpdate, QuotaExceeded
from tests.conftest import _noop_lifespan, requires_db

TENANT = "tenant-a"


# ── A fake database with a real lock ───────────────────────────────────────
class Store:
    """Shared between sessions: committed jobs, and the per-tenant locks."""

    def __init__(self, org_settings: dict[str, Any] | None = None) -> None:
        self.jobs: list[CreativeJob] = []
        self.org_settings = org_settings or {}
        self.locks: dict[str, threading.Lock] = {}
        self.guard = threading.Lock()

    def lock_for(self, key: str) -> threading.Lock:
        with self.guard:
            return self.locks.setdefault(key, threading.Lock())


class FakeSession:
    def __init__(self, store: Store) -> None:
        self.store = store
        self.held: list[threading.Lock] = []
        self.pending: list[CreativeJob] = []
        self.calls: list[str] = []

    def execute(self, stmt: Any, params: dict | None = None) -> Any:  # noqa: ANN401
        sql = str(stmt)
        if "pg_advisory_xact_lock" in sql:
            self.calls.append("lock")
            lock = self.store.lock_for(params["k"])
            lock.acquire()
            self.held.append(lock)
            return None
        if "lock_timeout" in sql:
            self.calls.append("timeout_reset" if "DEFAULT" in sql else f"timeout={params['v']}")
            return None
        if isinstance(stmt, Select) and stmt._for_update_arg is not None:
            self.calls.append("row")
            wanted = stmt.compile().params
            with self.store.guard:
                rows = [j for j in self.store.jobs
                        if j.id == wanted["id_1"] and j.tenant_id == wanted["tenant_id_1"]]
            return SimpleNamespace(scalars=lambda: SimpleNamespace(first=lambda: rows[0] if rows else None))
        raise AssertionError(f"unexpected statement {stmt}")

    def add(self, job: CreativeJob) -> None:
        self.calls.append("add")
        self.pending.append(job)

    def flush(self) -> None:
        self.calls.append("flush")

    def _release(self) -> None:
        for lock in self.held:
            lock.release()
        self.held.clear()

    def commit(self) -> None:
        with self.store.guard:
            self.store.jobs.extend(self.pending)
        self.pending.clear()
        self._release()

    def rollback(self) -> None:
        self.pending.clear()
        self._release()


def _install(monkeypatch: pytest.MonkeyPatch, store: Store, *, before_count: Callable[[], None] | None = None) -> None:
    def fake_usage(db: FakeSession, tenant_id: str, *, now: datetime | None = None) -> quotas.Usage:
        db.calls.append("count")
        with store.guard:
            jobs = [j for j in store.jobs if j.tenant_id == tenant_id]
        if before_count is not None:
            before_count()
        return quotas.Usage(
            jobs=len(jobs),
            images=sum(j.images_generated or 0 for j in jobs),
            tokens=sum((j.tokens_in or 0) + (j.tokens_out or 0) for j in jobs),
        )

    monkeypatch.setattr(quotas, "usage", fake_usage)
    monkeypatch.setattr(quotas, "_fresh_settings", lambda db, tenant_id: dict(store.org_settings))
    monkeypatch.setattr(quotas, "_session_scope", lambda tenant_id=None: short_scope(store))


@contextmanager
def short_scope(store: Store) -> Iterator[FakeSession]:
    """``session_scope`` on the fake: commit on success, roll back on error."""
    session = FakeSession(store)
    store.short_sessions = [*getattr(store, "short_sessions", []), session]
    try:
        yield session
    except Exception:
        session.rollback()
        raise
    session.commit()


def _job(tenant: str = TENANT, **kw: int) -> CreativeJob:
    counts = {"images_generated": 0, "tokens_in": 0, "tokens_out": 0, **kw}
    return CreativeJob(id=new_id(), tenant_id=tenant, created_by="u1", **counts)


def _fill(store: Store, n: int, tenant: str = TENANT, **kw: int) -> None:
    store.jobs.extend(_job(tenant, **kw) for _ in range(n))


@pytest.fixture
def default_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "creative_jobs_per_month", 100)
    monkeypatch.setattr(settings, "creative_images_per_month", 200)
    monkeypatch.setattr(settings, "creative_tokens_per_month", 0)


# ── Jobs ───────────────────────────────────────────────────────────────────
def test_the_100th_job_is_admitted_and_the_101st_refused(monkeypatch, default_limits) -> None:  # noqa: ANN001
    store = Store()
    _fill(store, 99)
    _install(monkeypatch, store)

    db = FakeSession(store)
    after = quotas.admit_job(db, _job())
    db.commit()
    assert after.jobs == 100 and len(store.jobs) == 100

    db = FakeSession(store)
    with pytest.raises(QuotaExceeded) as err:
        quotas.admit_job(db, _job())
    db.rollback()
    assert err.value.status_code == 429
    assert err.value.details == {"quota": "jobs_per_month"}
    assert "this month" in err.value.message
    assert len(store.jobs) == 100


def test_the_lock_is_taken_before_counting_and_held_through_the_insert(monkeypatch, default_limits) -> None:  # noqa: ANN001
    store = Store()
    _install(monkeypatch, store)
    db = FakeSession(store)
    quotas.admit_job(db, _job())
    assert db.calls[:6] == ["timeout=5s", "lock", "timeout_reset", "count", "add", "flush"]
    # Still held until the caller commits.
    assert db.held and db.held[0].locked()
    db.commit()
    assert not store.lock_for(f"creative_quota:{TENANT}").locked()


def test_one_workspace_full_does_not_block_another(monkeypatch, default_limits) -> None:  # noqa: ANN001
    store = Store()
    _fill(store, 100, tenant="tenant-b")
    _install(monkeypatch, store)
    db = FakeSession(store)
    quotas.admit_job(db, _job(TENANT))
    db.commit()


def test_a_job_without_a_tenant_is_rejected(monkeypatch, default_limits) -> None:  # noqa: ANN001
    _install(monkeypatch, Store())
    with pytest.raises(ValueError):
        quotas.admit_job(FakeSession(Store()), _job(tenant=""))


def test_zero_means_no_cap(monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setattr(settings, "creative_jobs_per_month", 0)
    store = Store()
    _fill(store, 5000)
    _install(monkeypatch, store)
    db = FakeSession(store)
    quotas.admit_job(db, _job())
    db.commit()


def test_a_portal_override_replaces_the_default(monkeypatch, default_limits) -> None:  # noqa: ANN001
    store = Store({quotas.SETTINGS_KEY: {"jobs_per_month": 2}})
    _fill(store, 2)
    _install(monkeypatch, store)
    with pytest.raises(QuotaExceeded):
        quotas.admit_job(FakeSession(store), _job())


# ── Concurrency ────────────────────────────────────────────────────────────
def _race(store: Store, workers: int) -> list[bool]:
    def attempt() -> bool:
        db = FakeSession(store)
        try:
            quotas.admit_job(db, _job())
        except QuotaExceeded:
            db.rollback()
            return False
        db.commit()
        return True

    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(lambda _: attempt(), range(workers)))


def test_concurrent_requests_cannot_overshoot_the_quota(monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setattr(settings, "creative_jobs_per_month", 5)
    store = Store()
    _fill(store, 2)
    # Widen the window between counting and inserting.
    _install(monkeypatch, store, before_count=lambda: threading.Event().wait(0.002))
    results = _race(store, 24)
    assert sum(results) == 3
    assert len([j for j in store.jobs if j.tenant_id == TENANT]) == 5


def test_without_the_lock_the_same_race_would_overshoot(monkeypatch) -> None:  # noqa: ANN001
    """Control: proves the race test can see a race at all."""
    monkeypatch.setattr(settings, "creative_jobs_per_month", 5)
    workers = 8
    barrier = threading.Barrier(workers)
    store = Store()
    _install(monkeypatch, store, before_count=barrier.wait)
    monkeypatch.setattr(quotas, "_lock", lambda db, tenant_id: None)
    assert sum(_race(store, workers)) == workers  # every one admitted: 8 > 5


# ── Images and tokens ──────────────────────────────────────────────────────
def test_images_are_reserved_before_generation_and_capped(monkeypatch, default_limits) -> None:  # noqa: ANN001
    monkeypatch.setattr(settings, "creative_images_per_month", 10)
    store = Store()
    _fill(store, 1, images_generated=8)
    _install(monkeypatch, store)

    job = _job()
    store.jobs.append(job)
    db = FakeSession(store)
    quotas.reserve_images(db, job, 2)
    db.commit()
    assert job.images_generated == 2

    db = FakeSession(store)
    with pytest.raises(QuotaExceeded) as err:
        quotas.reserve_images(db, job, 1)
    db.rollback()
    assert err.value.details == {"quota": "images_per_month"}
    assert "No AI image" in err.value.message
    assert job.images_generated == 2

    quotas.release_images(FakeSession(store), job, 5)
    assert job.images_generated == 0  # never negative


def test_a_negative_image_count_is_rejected(monkeypatch, default_limits) -> None:  # noqa: ANN001
    _install(monkeypatch, Store())
    with pytest.raises(ValueError):
        quotas.reserve_images(FakeSession(Store()), _job(), -3)


def test_tokens_are_reserved_at_the_ceiling_then_settled(monkeypatch, default_limits) -> None:  # noqa: ANN001
    monkeypatch.setattr(settings, "creative_tokens_per_month", 5000)
    store = Store()
    _fill(store, 1, tokens_in=1000, tokens_out=1500)
    _install(monkeypatch, store)
    job = _job()
    store.jobs.append(job)

    db = FakeSession(store)
    reserved = quotas.reserve_tokens(db, job, 2000)
    db.commit()
    assert job.tokens_out == 2000
    quotas.settle_tokens(FakeSession(store), job, reserved, tokens_in=300, tokens_out=400)
    assert (job.tokens_in, job.tokens_out) == (300, 400)

    # 2500 + 700 used; another 2000-token call would pass 5000.
    with pytest.raises(QuotaExceeded) as err:
        quotas.reserve_tokens(FakeSession(store), job, 2000)
    assert err.value.details == {"quota": "tokens_per_month"}
    with pytest.raises(ValueError):
        quotas.reserve_tokens(FakeSession(store), job, 0)


def test_reported_then_unreported_never_drops_the_counted_total(monkeypatch, default_limits) -> None:  # noqa: ANN001
    monkeypatch.setattr(settings, "creative_tokens_per_month", 50_000)
    store = Store()
    job = _job()
    store.jobs.append(job)
    _install(monkeypatch, store)

    reserved = quotas.reserve_tokens(FakeSession(store), job, 2000)
    quotas.settle_tokens(FakeSession(store), job, reserved, tokens_in=300, tokens_out=400)
    assert (job.tokens_in, job.tokens_out) == (300, 400)
    counted = (job.tokens_in or 0) + (job.tokens_out or 0)
    assert counted == 700

    reserved = quotas.reserve_tokens(FakeSession(store), job, 2000)
    quotas.settle_tokens(FakeSession(store), job, reserved, tokens_in=None, tokens_out=None)
    # Previously counted input is folded into tokens_out, not wiped.
    assert job.tokens_in is None
    assert (job.tokens_in or 0) + (job.tokens_out or 0) == counted + 2000
    assert job.tokens_out == 2700


# ── Limits and overrides ───────────────────────────────────────────────────
def test_malformed_stored_overrides_are_ignored(default_limits) -> None:  # noqa: ANN001
    stored = {quotas.SETTINGS_KEY: {
        "jobs_per_month": True, "images_per_month": -1, "tokens_per_month": "9", "other": 1,
    }}
    assert quotas.limits_from(stored).as_dict() == {
        "jobs_per_month": 100, "images_per_month": 200, "tokens_per_month": 0,
    }
    assert quotas.limits_from({quotas.SETTINGS_KEY: "nope"}).jobs_per_month == 100


@pytest.mark.parametrize(
    "body",
    [
        {"jobs_per_month": -1},
        {"jobs_per_month": quotas.MAX_LIMIT + 1},
        {"jobs_per_month": True},
        {"jobs_per_month": "100"},
        {"jobs_per_month": 1.5},
        {"seats": 10},
    ],
)
def test_override_body_is_strictly_validated(body: dict) -> None:
    with pytest.raises(ValidationError):
        CreativeLimitsUpdate.model_validate(body)


def test_month_window_rolls_over_the_year() -> None:
    start, end = quotas.month_window(datetime(2026, 12, 31, 23, 59, tzinfo=UTC))
    assert (start, end) == (datetime(2026, 12, 1, tzinfo=UTC), datetime(2027, 1, 1, tzinfo=UTC))


class _OrgSession:
    def __init__(self, org: Any) -> None:  # noqa: ANN401
        self.org = org
        self.statements: list[Any] = []

    def execute(self, stmt: Any) -> Any:  # noqa: ANN401
        self.statements.append(stmt)
        return SimpleNamespace(scalar_one_or_none=lambda: self.org)

    def flush(self) -> None:
        pass


def test_set_overrides_locks_the_row_and_reports_changes(default_limits) -> None:  # noqa: ANN001
    org = SimpleNamespace(id="org-1", settings={"industry": "cars", quotas.SETTINGS_KEY: {"jobs_per_month": 5}})
    db = _OrgSession(org)
    update = CreativeLimitsUpdate.model_validate({"jobs_per_month": None, "images_per_month": 50})
    _, changed = quotas.set_overrides(db, "org-1", update)  # type: ignore[arg-type]
    assert db.statements[0]._for_update_arg is not None
    assert changed == {
        "jobs_per_month": {"from": 5, "to": None},
        "images_per_month": {"from": None, "to": 50},
    }
    assert org.settings == {"industry": "cars", quotas.SETTINGS_KEY: {"images_per_month": 50}}


def test_set_overrides_on_a_missing_workspace_is_not_found() -> None:
    from app.core.exceptions import NotFoundError

    with pytest.raises(NotFoundError):
        quotas.set_overrides(_OrgSession(None), "nope", CreativeLimitsUpdate())  # type: ignore[arg-type]


# ── Portal route: portal admins only, audited ──────────────────────────────
def _dependency_calls(dependant: Any) -> set[Any]:  # noqa: ANN401
    calls = set()
    for dep in dependant.dependencies:
        calls.add(dep.call)
        calls |= _dependency_calls(dep)
    return calls


def test_creative_limit_routes_require_a_portal_admin() -> None:
    from app.api.deps import require_portal_admin
    from app.api.v1 import portal

    routes = [r for r in portal.router.routes if r.path.endswith("/creative-limits")]
    assert {m for r in routes for m in r.methods} == {"GET", "PUT"}
    for route in routes:
        assert require_portal_admin in _dependency_calls(route.dependant), route.path


class _RouteDb:
    def __init__(self) -> None:
        self.commits = 0

    def commit(self) -> None:
        self.commits += 1


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch):  # noqa: ANN201
    """The app with auth and the database replaced; the caller's email is settable."""
    from fastapi.testclient import TestClient

    from app.api.deps import CurrentUser, get_current_user
    from app.db.session import get_db
    from app.main import create_app

    monkeypatch.setattr(settings, "portal_admin_emails", ["ops@platform.example"])
    state = SimpleNamespace(email="owner@customer.example", db=_RouteDb())
    app = create_app()
    # No scheduler or startup database probe: auth and the database are faked.
    app.router.lifespan_context = _noop_lifespan

    def current() -> CurrentUser:
        user = SimpleNamespace(id="u1", email=state.email, name="Caller", role="admin")
        return CurrentUser(user, SimpleNamespace(id="tenant-portal"), {})  # type: ignore[arg-type]

    app.dependency_overrides[get_current_user] = current
    app.dependency_overrides[get_db] = lambda: state.db
    with TestClient(app) as client:
        yield client, state


def test_a_workspace_admin_gets_403_on_the_override(api, monkeypatch) -> None:  # noqa: ANN001
    client, state = api
    touched: list[str] = []
    monkeypatch.setattr(quotas, "set_overrides", lambda *a, **k: touched.append("set"))
    response = client.put(
        "/api/v1/portal/organizations/org-1/creative-limits", json={"jobs_per_month": 100000}
    )
    assert response.status_code == 403
    assert touched == [] and state.db.commits == 0
    assert client.get("/api/v1/portal/organizations/org-1/creative-limits").status_code == 403


def test_a_portal_admin_override_is_validated_and_audited(api, monkeypatch) -> None:  # noqa: ANN001
    from app.services import audit

    client, state = api
    state.email = "ops@platform.example"
    org = SimpleNamespace(id="org-1", settings={quotas.SETTINGS_KEY: {"jobs_per_month": 250}})
    monkeypatch.setattr(
        quotas, "set_overrides",
        lambda db, org_id, update: (org, {"jobs_per_month": {"from": None, "to": 250}}),
    )
    monkeypatch.setattr(quotas, "usage", lambda db, tenant_id, now=None: quotas.Usage(jobs=3))
    monkeypatch.setattr("app.db.session.bind_tenant", lambda db, tenant_id: None)
    recorded: list[dict] = []
    monkeypatch.setattr(audit, "record", lambda db, **kw: recorded.append(kw))

    bad = client.put("/api/v1/portal/organizations/org-1/creative-limits", json={"jobs_per_month": -5})
    assert bad.status_code == 422 and recorded == []

    response = client.put(
        "/api/v1/portal/organizations/org-1/creative-limits", json={"jobs_per_month": 250}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["effective"]["jobs_per_month"] == 250
    assert body["overrides"] == {"jobs_per_month": 250}
    assert body["usage"]["jobs"] == 3
    # Audited in the portal admin's workspace and in the changed workspace.
    assert [r["tenant_id"] for r in recorded] == ["tenant-portal", "org-1"]
    assert recorded[0]["context"] == {
        "organization_id": "org-1", "changed": {"jobs_per_month": {"from": None, "to": 250}},
    }
    assert state.db.commits == 1


def test_an_oversized_org_id_is_rejected(api) -> None:  # noqa: ANN001
    client, state = api
    state.email = "ops@platform.example"
    response = client.put(
        "/api/v1/portal/organizations/" + "x" * 65 + "/creative-limits", json={}
    )
    assert response.status_code == 422


# ── Real Postgres ──────────────────────────────────────────────────────────
@requires_db
def test_db_concurrent_admission_never_overshoots(org) -> None:  # noqa: ANN001
    from sqlalchemy import func, select

    from app.db.session import SessionLocal, bind_tenant

    setup = SessionLocal()
    try:
        quotas.set_overrides(setup, org.id, CreativeLimitsUpdate(jobs_per_month=3))
        setup.commit()
    finally:
        setup.close()

    def attempt(_: int) -> bool:
        session = SessionLocal()
        try:
            bind_tenant(session, org.id)
            quotas.admit_job(session, CreativeJob(tenant_id=org.id, created_by="u1"))
            # Hold the lock a moment so the others queue behind it.
            threading.Event().wait(0.02)
            session.commit()
            return True
        except QuotaExceeded:
            session.rollback()
            return False
        finally:
            session.close()

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(attempt, range(8)))
    assert sum(results) == 3

    check = SessionLocal()
    try:
        bind_tenant(check, org.id)
        count = check.execute(
            select(func.count(CreativeJob.id)).where(CreativeJob.tenant_id == org.id)
        ).scalar_one()
        assert count == 3
    finally:
        check.close()


@requires_db
def test_db_usage_is_per_tenant(org, second_org, clean_db) -> None:  # noqa: ANN001
    from app.db.session import bind_tenant

    bind_tenant(clean_db, org.id)
    quotas.admit_job(clean_db, CreativeJob(tenant_id=org.id, created_by="u1", images_generated=4))
    clean_db.commit()
    assert quotas.usage(clean_db, org.id).as_dict() == {"jobs": 1, "images": 4, "tokens": 0}
    bind_tenant(clean_db, second_org.id)
    assert quotas.usage(clean_db, second_org.id).jobs == 0
