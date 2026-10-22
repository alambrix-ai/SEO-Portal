"""Retention pruning (step 4.1): ``app.services.retention``.

The statement-shape tests run without a database. The behaviour tests at the
bottom need ``TEST_DATABASE_URL`` (a throwaway database).
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.dialects import postgresql

from app.services import retention
from tests.conftest import requires_db

TENANT_ID = "00000000-0000-0000-0000-00000000aa01"
OTHER_TENANT = "00000000-0000-0000-0000-00000000bb02"
NOW = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)
GOOD_SHA = "a" * 64


def _sql(statement: Any) -> tuple[str, dict[str, Any]]:  # noqa: ANN401
    compiled = statement.compile(dialect=postgresql.dialect())
    return str(compiled), dict(compiled.params)


# ── Expired rows ───────────────────────────────────────────────────────────
def test_expired_rows_are_judged_by_expiry_or_age_and_the_configured_days(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(retention.settings, "retention_login_codes_days", 7)
    monkeypatch.setattr(retention.settings, "retention_refresh_tokens_days", 30)
    monkeypatch.setattr(retention.settings, "retention_demo_analyses_days", 90)

    conditions = retention.expired_conditions(NOW)

    assert set(conditions) == {"login_codes", "refresh_tokens", "demo_analyses"}
    expected = {
        # A code or token is pruned only after it has been EXPIRED this long,
        # so anything still redeemable is never touched.
        "login_codes": ("login_codes.expires_at <", NOW - timedelta(days=7)),
        "refresh_tokens": ("refresh_tokens.expires_at <", NOW - timedelta(days=30)),
        "demo_analyses": ("demo_analyses.created_at <", NOW - timedelta(days=90)),
    }
    for table, (_model, condition) in conditions.items():
        sql, params = _sql(condition)
        fragment, cutoff = expected[table]
        assert fragment in sql, (table, sql)
        assert list(params.values()) == [cutoff], table


class _DeleteDb:
    """Reports ``counts`` as the rowcount of successive DELETE statements."""

    def __init__(self, counts: list[int]) -> None:
        self.counts = counts
        self.statements: list[Any] = []

    def execute(self, statement: Any) -> SimpleNamespace:  # noqa: ANN401
        self.statements.append(statement)
        return SimpleNamespace(rowcount=self.counts.pop(0) if self.counts else 0)


def test_deletes_run_in_bounded_batches(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(retention, "DELETE_BATCH", 10)
    monkeypatch.setattr(retention, "MAX_BATCHES", 3)
    # login_codes: full, full, full -> stops at the cap; refresh: one short
    # batch; demo: nothing.
    db = _DeleteDb([10, 10, 10, 4, 0])

    counts = retention.prune_expired(db, now=NOW)  # type: ignore[arg-type]

    assert counts == {"login_codes": 30, "refresh_tokens": 4, "demo_analyses": 0}
    sql, params = _sql(db.statements[0])
    assert sql.startswith("DELETE FROM login_codes WHERE login_codes.id IN (SELECT")
    assert 10 in params.values()  # the LIMIT


# ── Orphaned uploads: the query ────────────────────────────────────────────
def test_the_orphan_query_only_considers_old_uploads_of_three_kinds() -> None:
    sql, params = _sql(retention.orphan_uploads_query(TENANT_ID, now=NOW))

    kinds = [v for k, v in params.items() if k.startswith("kind_")]
    assert kinds == [["logo", "product_photo", "background"]]
    assert "render" not in str(params.values()) and "preview" not in str(params.values())
    assert params["source_1"] == "upload"
    assert params["job_id_1"] == ""
    assert params["created_at_1"] == NOW - timedelta(days=retention.ORPHAN_UPLOAD_DAYS)
    assert "FOR UPDATE OF stored_files SKIP LOCKED" in sql
    assert "LIMIT" in sql


def test_every_reference_check_is_scoped_to_the_same_workspace() -> None:
    sql, params = _sql(retention.orphan_uploads_query(TENANT_ID, now=NOW))

    for table in ("brand_kits", "products", "creative_jobs", "marketing_assets"):
        assert f"NOT (EXISTS (SELECT * \nFROM {table}" in sql, table
        assert f"{table}.tenant_id = %(" in sql, table
    assert "stored_files.tenant_id = %(" in sql
    tenant_params = [v for k, v in params.items() if k.startswith("tenant_id_")]
    assert tenant_params and set(tenant_params) == {TENANT_ID}
    # Logos by foreign key, product photos by JSON containment, job outputs
    # and asset links by the id's text.
    assert "brand_kits.logo_file_id = stored_files.id" in sql
    assert "brand_kits.logo_dark_file_id = stored_files.id" in sql
    assert "products.image_file_ids @> jsonb_build_array(CAST(stored_files.id AS VARCHAR))" in sql
    assert "CAST(creative_jobs.outputs AS TEXT) LIKE" in sql
    assert "CAST(marketing_assets.detail AS TEXT) LIKE" in sql
    assert "CAST(marketing_assets.image_ids AS TEXT) LIKE" in sql


# ── Orphaned uploads: the deletion ─────────────────────────────────────────
class _Scalars:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def scalars(self) -> list[Any]:
        return list(self._rows)


class _OrphanDb:
    def __init__(self, rows: list[Any]) -> None:
        self.rows = rows

    def execute(self, statement: Any) -> _Scalars:  # noqa: ANN401
        return _Scalars(self.rows)


class _Storage:
    def __init__(self, *, fail_on: str = "") -> None:
        self.deleted: list[tuple[str, str]] = []
        self.fail_on = fail_on

    def delete(self, db: object, *, tenant_id: str, file_id: str) -> None:
        if file_id == self.fail_on:
            raise RuntimeError("bucket said no: token=sk-live-secret-bucket-key")
        self.deleted.append((tenant_id, file_id))


def _file(file_id: str, *, kind: str = "logo", source: str = "upload", tenant: str = TENANT_ID) -> SimpleNamespace:
    return SimpleNamespace(
        id=file_id, tenant_id=tenant, kind=kind, source=source, size_bytes=100,
        display_name="my-secret-logo.png", storage_backend="postgres",
    )


@pytest.fixture
def storage(monkeypatch: pytest.MonkeyPatch) -> _Storage:
    from app.services import storage as storage_module

    fake = _Storage()
    monkeypatch.setattr(storage_module, "storage_for", lambda stored: fake)
    return fake


@pytest.fixture
def audited(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    from app.services import audit

    entries: list[dict[str, Any]] = []
    monkeypatch.setattr(audit, "record_system_action", lambda db, **kw: entries.append(kw))
    return entries


def test_orphans_are_deleted_through_their_storage_and_audited_as_counts(
    storage: _Storage, audited: list[dict[str, Any]]
) -> None:
    db = _OrphanDb([_file("f1"), _file("f2", kind="product_photo")])

    result = retention.prune_orphan_uploads(db, tenant_id=TENANT_ID, now=NOW)  # type: ignore[arg-type]

    assert result == retention.OrphanPrune(files=2, bytes=200, failed=0)
    assert storage.deleted == [(TENANT_ID, "f1"), (TENANT_ID, "f2")]
    [entry] = audited
    assert entry["tenant_id"] == TENANT_ID
    assert entry["context"] == {"files": 2, "bytes": 200}
    # Counts only: no ids or names anywhere in the audit entry.
    assert "f1" not in str(entry) and "my-secret-logo" not in str(entry)


@pytest.mark.parametrize(
    "row",
    [
        _file("render", kind="render", source="rendered"),
        _file("preview", kind="preview", source="rendered"),
        _file("generated-bg", kind="background", source="generated"),
        _file("theirs", tenant=OTHER_TENANT),
    ],
    ids=["render", "preview", "generated", "other-workspace"],
)
def test_rows_this_module_must_never_delete_are_refused_even_if_returned(
    storage: _Storage, audited: list[dict[str, Any]], row: SimpleNamespace
) -> None:
    result = retention.prune_orphan_uploads(_OrphanDb([row]), tenant_id=TENANT_ID, now=NOW)  # type: ignore[arg-type]

    assert storage.deleted == []
    assert result.files == 0 and result.failed == 1
    assert audited == []


def test_a_storage_failure_skips_that_file_only_and_is_logged_without_its_text(
    storage: _Storage, audited: list[dict[str, Any]], caplog: pytest.LogCaptureFixture
) -> None:
    storage.fail_on = "f1"
    with caplog.at_level(logging.WARNING):
        result = retention.prune_orphan_uploads(
            _OrphanDb([_file("f1"), _file("f2")]), tenant_id=TENANT_ID, now=NOW  # type: ignore[arg-type]
        )

    assert result == retention.OrphanPrune(files=1, bytes=100, failed=1)
    assert storage.deleted == [(TENANT_ID, "f2")]
    assert "sk-live-secret-bucket-key" not in caplog.text
    assert audited[0]["context"] == {"files": 1, "bytes": 100}


def test_nothing_to_prune_writes_no_audit_entry(storage: _Storage, audited: list[dict[str, Any]]) -> None:
    assert retention.prune_orphan_uploads(_OrphanDb([]), tenant_id=TENANT_ID, now=NOW) == retention.OrphanPrune()  # type: ignore[arg-type]
    assert audited == []


# ── Wiring ─────────────────────────────────────────────────────────────────
def test_prune_retention_prunes_globally_once_then_per_workspace(monkeypatch: pytest.MonkeyPatch) -> None:
    from contextlib import contextmanager

    from app.orchestration import scheduler as scheduler_module

    calls: list[tuple[str, Any]] = []

    @contextmanager
    def fake_scope(tenant_id: str | None = None):  # noqa: ANN202
        calls.append(("scope", tenant_id))
        yield "global-db"

    class _Session:
        def close(self) -> None:
            calls.append(("closed", None))

    monkeypatch.setattr(scheduler_module, "session_scope", fake_scope)
    monkeypatch.setattr(scheduler_module, "SessionLocal", _Session)
    monkeypatch.setattr(
        scheduler_module,
        "run_per_organization",
        lambda db, handler, **_: [handler(db, TENANT_ID), handler(db, OTHER_TENANT)],
    )
    monkeypatch.setattr(
        retention, "prune_expired", lambda db, **_: calls.append(("expired", db)) or {"login_codes": 3}
    )
    monkeypatch.setattr(
        retention,
        "prune_orphan_uploads",
        lambda db, *, tenant_id, **_: calls.append(("orphans", tenant_id))
        or retention.OrphanPrune(files=2, bytes=10, failed=1),
    )

    counts = scheduler_module.prune_retention()

    assert counts == {"login_codes": 3, "unused_uploads": 4}
    # Unpinned for the global tables; each workspace's uploads under its own run.
    assert calls[:2] == [("scope", None), ("expired", "global-db")]
    assert ("orphans", TENANT_ID) in calls and ("orphans", OTHER_TENANT) in calls


def test_in_process_pruning_waits_after_start_then_runs_daily(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.db.base import utcnow
    from app.orchestration import scheduler as scheduler_module

    runs: list[int] = []
    monkeypatch.setattr(scheduler_module, "prune_retention", lambda: runs.append(1) or {})
    monkeypatch.setattr(scheduler_module.settings, "celery_enabled", False)
    sched = scheduler_module.AgentScheduler(tick_seconds=30)

    sched._maybe_prune_retention()
    assert runs == []  # never the first thing a process does
    sched._next_retention = utcnow() - timedelta(seconds=1)
    sched._maybe_prune_retention()
    sched._maybe_prune_retention()
    assert runs == [1]

    monkeypatch.setattr(scheduler_module.settings, "celery_enabled", True)
    sched._next_retention = utcnow() - timedelta(seconds=1)
    sched._maybe_prune_retention()
    assert runs == [1]  # beat owns it under Celery


# ── Behaviour (needs a database) ───────────────────────────────────────────
def _stored(db: Any, tenant_id: str, *, kind: str = "logo", source: str = "upload", age_days: int = 30, job_id: str = "") -> str:  # noqa: ANN401
    from app.models.creative import StoredFile

    row = StoredFile(
        tenant_id=tenant_id, kind=kind, source=source, mime="image/png", size_bytes=3,
        sha256=GOOD_SHA, data=b"png", job_id=job_id,
    )
    db.add(row)
    db.flush()
    row.created_at = datetime.now(UTC) - timedelta(days=age_days)
    db.flush()
    return row.id


@requires_db
def test_only_unreferenced_old_uploads_are_pruned(clean_db, org, second_org) -> None:  # noqa: ANN001
    from sqlalchemy import select

    from app.db.session import bind_tenant
    from app.models.content import MarketingAsset
    from app.models.creative import BrandKit, CreativeJob, Product, StoredFile

    bind_tenant(clean_db, org.id)
    orphan = _stored(clean_db, org.id)
    recent = _stored(clean_db, org.id, age_days=2)
    kit_logo = _stored(clean_db, org.id)
    photo = _stored(clean_db, org.id, kind="product_photo")
    in_job = _stored(clean_db, org.id, kind="background")
    in_asset = _stored(clean_db, org.id)
    render = _stored(clean_db, org.id, kind="render", source="rendered")
    clean_db.add_all(
        [
            BrandKit(tenant_id=org.id, name="Main", logo_file_id=kit_logo),
            Product(tenant_id=org.id, name="Widget", image_file_ids=[photo]),
            CreativeJob(tenant_id=org.id, created_by="u", outputs=[{"file_ids": [in_job]}]),
            MarketingAsset(
                tenant_id=org.id, agent_slug="creative", kind="creative",
                detail={"preview_file_id": in_asset},
            ),
        ]
    )
    clean_db.commit()

    bind_tenant(clean_db, second_org.id)
    # The other workspace's product lists our orphan's id: that must not
    # protect it (references are per workspace), and theirs must survive.
    theirs = _stored(clean_db, second_org.id, age_days=2)
    clean_db.add(Product(tenant_id=second_org.id, name="Copycat", image_file_ids=[orphan]))
    clean_db.commit()

    bind_tenant(clean_db, org.id)
    result = retention.prune_orphan_uploads(clean_db, tenant_id=org.id)
    clean_db.commit()

    assert result.files == 1
    left = set(clean_db.execute(select(StoredFile.id)).scalars())
    assert left == {recent, kit_logo, photo, in_job, in_asset, render}

    bind_tenant(clean_db, second_org.id)
    assert clean_db.execute(select(StoredFile.id)).scalars().all() == [theirs]


@requires_db
def test_expired_codes_tokens_and_demo_requests_are_pruned_and_live_ones_kept(clean_db, org) -> None:  # noqa: ANN001
    from sqlalchemy import func, select

    from app.db.session import bind_tenant
    from app.models.demo import DemoAnalysis
    from app.models.identity import LoginCode
    from app.models.workspace import RefreshToken, User

    now = datetime.now(UTC)
    bind_tenant(clean_db, org.id)
    user = clean_db.execute(select(User).where(User.tenant_id == org.id)).scalars().first()
    clean_db.add_all(
        [
            LoginCode(email_index="e" * 64, purpose="sign_in", code_hash="1" * 64, expires_at=now - timedelta(days=30)),
            LoginCode(email_index="e" * 64, purpose="sign_in", code_hash="2" * 64, expires_at=now + timedelta(minutes=5)),
            RefreshToken(tenant_id=org.id, user_id=user.id, token_hash="3" * 64, family_id=org.id, expires_at=now - timedelta(days=60)),
            RefreshToken(tenant_id=org.id, user_id=user.id, token_hash="4" * 64, family_id=org.id, expires_at=now + timedelta(days=5)),
        ]
    )
    old_demo = DemoAnalysis(device_hash="d", fingerprint_hash="f", ip_hash="i", token_hash="t", website_url="https://a.example")
    new_demo = DemoAnalysis(device_hash="d2", fingerprint_hash="f", ip_hash="i", token_hash="t2", website_url="https://b.example")
    clean_db.add_all([old_demo, new_demo])
    clean_db.flush()
    old_demo.created_at = now - timedelta(days=200)
    clean_db.commit()

    counts = retention.prune_expired(clean_db)
    clean_db.commit()

    assert counts == {"login_codes": 1, "refresh_tokens": 1, "demo_analyses": 1}
    assert clean_db.execute(select(func.count()).select_from(LoginCode)).scalar_one() == 1
    assert clean_db.execute(select(func.count()).select_from(RefreshToken)).scalar_one() == 1
    assert clean_db.execute(select(DemoAnalysis.id)).scalars().all() == [new_demo.id]
