"""Metrics, LLM usage and versions: models, migration 0021 and database rules.

The migration cannot be run here (there is no disposable database in this
environment), so it is executed against a recording stand-in for
``alembic.op`` and its schema compared with the models, as
tests/test_creative_models.py does for 0019. The database-backed tests run
when TEST_DATABASE_URL points at a throwaway database.
"""
from __future__ import annotations

import importlib.util
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from sqlalchemy import BigInteger, CheckConstraint, Column, ForeignKeyConstraint, UniqueConstraint

from app.db.rls import RLS_EXEMPT_TABLES, TENANT_SCOPED_TABLES
from app.models.agent import AgentRun
from app.models.history import (
    SNAPSHOT_TRIGGER_DDL,
    AuditSnapshot,
    SnapshotActor,
    SnapshotKind,
)
from app.models.social import POST_METRIC_FIELDS, MetricPoint, SocialPostMetric
from tests.conftest import requires_db

VERSIONS = Path(__file__).resolve().parents[1] / "alembic" / "versions"
MIGRATION = "0021_metrics_usage_versions"
NEW_TABLES = ("metric_points", "social_post_metrics")
#: audit_snapshots columns as 0015 created them.
SNAPSHOT_COLUMNS_0015 = {
    "id", "tenant_id", "kind", "run_id", "source", "label", "created_by",
    "created_by_name", "created_at", "metrics", "items", "item_count",
}
AGENT_RUN_COLUMNS_BEFORE = set(AgentRun.__table__.c.keys()) - {"llm_connector", "llm_model"}


def _load_migration(name: str = MIGRATION) -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"m_{name}", VERSIONS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ── Social post metrics ────────────────────────────────────────────────────
def test_post_metric_counts_are_nullable_big_integers() -> None:
    table = SocialPostMetric.__table__
    assert POST_METRIC_FIELDS == (
        "impressions", "reach", "engagements", "clicks", "likes", "comments", "shares", "saves",
    )
    for field in POST_METRIC_FIELDS:
        column = table.c[field]
        assert column.nullable is True, field
        assert isinstance(column.type, BigInteger), field
        assert column.default is None and column.server_default is None, field


def test_a_metric_the_platform_did_not_report_stays_unknown_not_zero() -> None:
    row = SocialPostMetric(
        tenant_id="t", asset_id="a", network="reddit", fetched_at=datetime.now(UTC), likes=4
    )
    assert row.likes == 4
    assert row.impressions is None
    assert row.reach is None


def test_one_metrics_row_per_post_removed_with_the_post() -> None:
    table = SocialPostMetric.__table__
    unique = {
        tuple(c.name for c in uc.columns)
        for uc in table.constraints
        if isinstance(uc, UniqueConstraint)
    }
    assert ("asset_id",) in unique
    fk = next(iter(table.c.asset_id.foreign_keys))
    assert fk.column.table.name == "marketing_assets"
    assert fk.ondelete == "CASCADE"


def test_negative_counts_are_refused_by_the_database() -> None:
    checks = {c.name: str(c.sqltext) for c in SocialPostMetric.__table__.constraints
              if isinstance(c, CheckConstraint)}
    for field in POST_METRIC_FIELDS:
        assert checks[f"ck_social_post_metrics_{field}"] == f"{field} IS NULL OR {field} >= 0"


# ── Metric points ──────────────────────────────────────────────────────────
def test_metric_points_have_one_value_per_day_source_metric_and_dimension() -> None:
    table = MetricPoint.__table__
    unique = next(
        uc for uc in table.constraints
        if isinstance(uc, UniqueConstraint) and uc.name == "uq_metric_points_key"
    )
    assert [c.name for c in unique.columns] == ["tenant_id", "day", "source", "metric", "dimension"]
    assert table.c.metric.type.length == 32
    assert table.c.dimension.type.length == 128
    assert table.c.source.type.length == 64
    assert table.c.value.nullable is False


def test_new_tables_are_tenant_scoped_and_under_rls() -> None:
    for model in (MetricPoint, SocialPostMetric):
        table = model.__table__
        assert table.name in TENANT_SCOPED_TABLES
        assert table.name not in RLS_EXEMPT_TABLES
        fk = next(iter(table.c.tenant_id.foreign_keys))
        assert fk.column.table.name == "organizations"
        assert fk.ondelete == "CASCADE"


# ── LLM usage on runs ──────────────────────────────────────────────────────
def test_runs_record_the_connector_and_model_they_used() -> None:
    table = AgentRun.__table__
    assert table.c.llm_connector.type.length == 64
    assert table.c.llm_model.type.length == 128
    for name in ("llm_connector", "llm_model", "tokens_in", "tokens_out"):
        assert table.c[name].nullable is False


# ── Versions ───────────────────────────────────────────────────────────────
def test_snapshot_versions_are_unique_per_subject() -> None:
    table = AuditSnapshot.__table__
    unique = next(
        uc for uc in table.constraints
        if isinstance(uc, UniqueConstraint) and uc.name == "uq_audit_snapshots_subject_version"
    )
    assert [c.name for c in unique.columns] == ["tenant_id", "kind", "subject_id", "version"]
    index = next(i for i in table.indexes if i.name == "ix_audit_snapshots_tenant_kind_subject_created")
    assert [c.name for c in index.columns] == ["tenant_id", "kind", "subject_id", "created_at"]
    # Numbered by the database (or the history store), never defaulted to 1.
    assert table.c.version.nullable is False
    assert table.c.version.default is None
    assert table.c.version.server_default is not None


def test_snapshot_columns_and_widths() -> None:
    table = AuditSnapshot.__table__
    assert table.c.subject_id.type.length == 64
    assert table.c.model.type.length == 128
    assert table.c.agent_slug.type.length == 64
    assert table.c.actor_type.type.length == 16
    for value in (SnapshotActor.USER, SnapshotActor.AGENT, SnapshotActor.SYSTEM):
        assert len(value) <= 16


def test_customer_content_has_an_encrypted_home_not_a_plain_column() -> None:
    columns = set(AuditSnapshot.__table__.c.keys())
    assert "payload_encrypted" in columns
    assert not {"payload", "content", "copy", "caption", "brief", "prompt"} & columns


def test_every_snapshot_kind_fits_its_column() -> None:
    kinds = {v for k, v in vars(SnapshotKind).items() if k.isupper()}
    assert {
        "site_analysis", "backlink_scan", "competitor_scan", "brand_kit", "product",
        "agent_config", "workspace_policy", "creative_text",
    } <= kinds
    width = AuditSnapshot.__table__.c.kind.type.length
    assert all(len(kind) <= width for kind in kinds)


def test_snapshots_refuse_update_but_allow_delete() -> None:
    creates = [s for s in SNAPSHOT_TRIGGER_DDL if s.startswith("CREATE TRIGGER")]
    assert any("BEFORE UPDATE ON audit_snapshots" in s for s in creates)
    assert any("BEFORE INSERT ON audit_snapshots" in s for s in creates)
    # Retention prunes delete rows; nothing may block that.
    assert not any("DELETE" in s for s in creates)
    # DDL() reads '%' as a format marker.
    assert not any("%" in s for s in SNAPSHOT_TRIGGER_DDL)


def test_the_models_install_the_triggers_on_create() -> None:
    """A schema built from the models (the test database) gets the same rules."""
    from sqlalchemy.schema import DDL

    hooks = [
        getattr(listener, "__self__", listener)
        for listener in AuditSnapshot.__table__.dispatch.after_create
    ]
    installed = [str(h.statement) for h in hooks if isinstance(h, DDL)]
    assert tuple(installed) == SNAPSHOT_TRIGGER_DDL


# ── Migration ──────────────────────────────────────────────────────────────
def test_migration_chain_and_ids() -> None:
    module = _load_migration()
    assert module.revision == MIGRATION
    assert module.down_revision == "0020_audit_append_only"
    assert len(module.revision) <= 32  # alembic_version.version_num
    assert callable(module.upgrade) and callable(module.downgrade)
    assert not [p.name for p in VERSIONS.glob("0021_*.py") if p.name != f"{MIGRATION}.py"]


def test_migration_imports_nothing_from_the_previous_revision() -> None:
    source = (VERSIONS / f"{MIGRATION}.py").read_text(encoding="utf-8")
    imports = [line for line in source.splitlines() if line.lstrip().startswith(("import ", "from "))]
    assert not [line for line in imports if "0020" in line or "versions" in line]


def test_frozen_copies_match_the_application() -> None:
    module = _load_migration()
    assert module.SNAPSHOT_TRIGGER_DDL == SNAPSHOT_TRIGGER_DDL
    assert module.POST_METRIC_FIELDS == POST_METRIC_FIELDS
    assert module.TABLES == NEW_TABLES


def test_backfill_numbers_per_subject_oldest_first() -> None:
    sql = " ".join(_load_migration().BACKFILL_SQL.split())
    assert "PARTITION BY tenant_id, kind, subject_id ORDER BY created_at, id" in sql
    assert sql.startswith("UPDATE audit_snapshots")


class _RecordingOp:
    """Stands in for ``alembic.op``: records calls, touches no database."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.tables: dict[str, list[Any]] = {}
        self.added: dict[str, list[Any]] = {}
        self.dropped_columns: list[tuple[str, str]] = []
        self.dropped_tables: list[str] = []

    def get_bind(self) -> object:
        return object()

    def create_table(self, name: str, *elements: Any) -> None:  # noqa: ANN401
        self.tables[name] = list(elements)
        self.calls.append(("create_table", name))

    def create_index(self, name: str, table: str, columns: list[str], **kw: Any) -> None:  # noqa: ANN401
        self.calls.append(("create_index", (name, table, tuple(columns))))

    def add_column(self, table: str, column: Any) -> None:  # noqa: ANN401
        self.added.setdefault(table, []).append(column)
        self.calls.append(("add_column", (table, column.name, column.nullable)))

    def alter_column(self, table: str, column: str, **kw: Any) -> None:  # noqa: ANN401
        self.calls.append(("alter_column", (table, column, kw.get("nullable"))))

    def create_check_constraint(self, name: str, table: str, condition: str) -> None:
        self.calls.append(("create_check_constraint", (name, table, condition)))

    def create_unique_constraint(self, name: str, table: str, columns: list[str]) -> None:
        self.calls.append(("create_unique_constraint", (name, table, tuple(columns))))

    def drop_index(self, name: str, table_name: str) -> None:
        self.calls.append(("drop_index", (name, table_name)))

    def drop_constraint(self, name: str, table: str, type_: str) -> None:
        self.calls.append(("drop_constraint", (name, table, type_)))

    def drop_column(self, table: str, column: str) -> None:
        self.dropped_columns.append((table, column))
        self.calls.append(("drop_column", (table, column)))

    def drop_table(self, name: str) -> None:
        self.dropped_tables.append(name)
        self.calls.append(("drop_table", name))

    def execute(self, statement: str) -> None:
        self.calls.append(("execute", statement))

    def executed(self) -> list[str]:
        return [arg for kind, arg in self.calls if kind == "execute"]


def _run(
    direction: str,
    tables: set[str],
    columns: dict[str, set[str]],
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[_RecordingOp, list]:
    import sqlalchemy as sa

    import app.db.rls as rls

    module = _load_migration()
    recorder = _RecordingOp()
    policies: list[tuple[str, ...]] = []

    class _Inspector:
        def get_table_names(self) -> list[str]:
            return sorted(tables)

        def get_columns(self, table: str) -> list[dict[str, str]]:
            return [{"name": n} for n in sorted(columns.get(table, set()))]

    monkeypatch.setattr(module, "op", recorder)
    monkeypatch.setattr(sa, "inspect", lambda _bind: _Inspector())
    monkeypatch.setattr(rls, "apply_policies", lambda _conn, tables: policies.append(tables))
    getattr(module, direction)()
    return recorder, policies


_BEFORE = {"agent_runs": AGENT_RUN_COLUMNS_BEFORE, "audit_snapshots": SNAPSHOT_COLUMNS_0015}
_AFTER = {
    "agent_runs": set(AgentRun.__table__.c.keys()),
    "audit_snapshots": set(AuditSnapshot.__table__.c.keys()),
}
_TABLES_BEFORE = {"organizations", "marketing_assets", "agent_runs", "audit_snapshots"}


def test_upgrade_creates_what_the_models_declare(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.db.base import Base

    recorder, policies = _run("upgrade", _TABLES_BEFORE, _BEFORE, monkeypatch)
    assert list(recorder.tables) == list(NEW_TABLES)
    assert policies == [NEW_TABLES]
    for name, elements in recorder.tables.items():
        model = Base.metadata.tables[name]
        columns = {e.name: e for e in elements if isinstance(e, Column)}
        assert set(columns) == set(model.c.keys()), name
        for col_name, col in columns.items():
            assert col.nullable == model.c[col_name].nullable, (name, col_name)
        checks = {e.name for e in elements if isinstance(e, CheckConstraint)}
        model_checks = {c.name for c in model.constraints if isinstance(c, CheckConstraint)}
        assert checks == model_checks, name
        fks = {
            (tuple(e.column_keys), e.ondelete) for e in elements if isinstance(e, ForeignKeyConstraint)
        }
        model_fks = {
            (tuple(c.name for c in fk.columns), fk.ondelete)
            for fk in model.constraints if isinstance(fk, ForeignKeyConstraint)
        }
        assert fks == model_fks, name


def test_upgrade_adds_exactly_the_model_columns(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder, _ = _run("upgrade", _TABLES_BEFORE, _BEFORE, monkeypatch)
    for table in ("agent_runs", "audit_snapshots"):
        added = {c.name for c in recorder.added[table]}
        assert _BEFORE[table] | added == _AFTER[table], table
        assert not _BEFORE[table] & added, table
        for column in recorder.added[table]:
            if column.name != "version":
                assert column.nullable is False and column.server_default is not None, column.name


def test_upgrade_numbers_existing_rows_before_requiring_a_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_migration()
    recorder, _ = _run("upgrade", _TABLES_BEFORE, _BEFORE, monkeypatch)
    calls = recorder.calls
    pos = {}
    for i, call in enumerate(calls):
        pos.setdefault(call, i)
    add_version = pos[("add_column", ("audit_snapshots", "version", True))]
    no_force = pos[("execute", "ALTER TABLE audit_snapshots NO FORCE ROW LEVEL SECURITY")]
    backfill = pos[("execute", module.BACKFILL_SQL)]
    force = pos[("execute", "ALTER TABLE audit_snapshots FORCE ROW LEVEL SECURITY")]
    not_null = pos[("alter_column", ("audit_snapshots", "version", False))]
    unique = pos[(
        "create_unique_constraint",
        ("uq_audit_snapshots_subject_version", "audit_snapshots",
         ("tenant_id", "kind", "subject_id", "version")),
    )]
    first_trigger = pos[("execute", SNAPSHOT_TRIGGER_DDL[0])]
    assert add_version < no_force < backfill < force < not_null < unique < first_trigger
    # RLS is put back: FORCE is the last word on the table.
    assert recorder.executed().count("ALTER TABLE audit_snapshots FORCE ROW LEVEL SECURITY") == 1
    # Every trigger statement runs, in order, after the backfill.
    executed = recorder.executed()
    start = executed.index(SNAPSHOT_TRIGGER_DDL[0])
    assert tuple(executed[start:start + len(SNAPSHOT_TRIGGER_DDL)]) == SNAPSHOT_TRIGGER_DDL


def test_upgrade_is_idempotent_over_an_upgraded_schema(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder, policies = _run("upgrade", _TABLES_BEFORE | set(NEW_TABLES), _AFTER, monkeypatch)
    assert recorder.tables == {}
    assert recorder.added == {}
    assert policies == []
    # No second backfill (it would renumber, and the triggers would refuse it).
    assert _load_migration().BACKFILL_SQL not in recorder.executed()
    assert tuple(recorder.executed()) == SNAPSHOT_TRIGGER_DDL


def test_downgrade_drops_triggers_first_then_everything_added(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_migration()
    recorder, _ = _run("downgrade", _TABLES_BEFORE | set(NEW_TABLES), _AFTER, monkeypatch)
    executed = recorder.executed()
    assert tuple(executed[: len(module.SNAPSHOT_TRIGGER_DROP)]) == module.SNAPSHOT_TRIGGER_DROP
    dropped = set(recorder.dropped_columns)
    assert {("audit_snapshots", c) for c in _AFTER["audit_snapshots"] - SNAPSHOT_COLUMNS_0015} == {
        c for c in dropped if c[0] == "audit_snapshots"
    }
    assert {("agent_runs", "llm_connector"), ("agent_runs", "llm_model")} <= dropped
    assert recorder.dropped_tables == list(reversed(NEW_TABLES))
    assert "DROP POLICY IF EXISTS tenant_isolation ON social_post_metrics" in executed


def test_downgrade_skips_what_is_already_gone(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder, _ = _run("downgrade", _TABLES_BEFORE, _BEFORE, monkeypatch)
    assert recorder.dropped_tables == []
    assert recorder.dropped_columns == []


# ── Database behaviour ─────────────────────────────────────────────────────
def _skip_if_rls_is_bypassed(db: Any) -> None:  # noqa: ANN401
    from sqlalchemy import text

    bypass = db.execute(
        text("SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname = current_user")
    ).scalar_one()
    if bypass:
        pytest.skip("The test database role bypasses row-level security")


def _snapshot(tenant_id: str, *, subject: str = "", minutes: int = 0, **kw: Any) -> AuditSnapshot:  # noqa: ANN401
    return AuditSnapshot(
        tenant_id=tenant_id,
        kind=SnapshotKind.BRAND_KIT,
        subject_id=subject,
        source="edited",
        created_at=datetime(2026, 10, 1, tzinfo=UTC) + timedelta(minutes=minutes),
        **kw,
    )


def _asset(tenant_id: str) -> Any:  # noqa: ANN401
    from app.models.content import AssetKind, AssetStatus, MarketingAsset

    return MarketingAsset(
        tenant_id=tenant_id, agent_slug="social_media_manager", kind=AssetKind.SOCIAL_POST,
        channel="instagram", status=AssetStatus.PUBLISHED, remote_id="17890",
    )


@requires_db
def test_the_database_numbers_versions_per_subject(clean_db, org) -> None:  # noqa: ANN001
    from app.db.session import bind_tenant

    bind_tenant(clean_db, org.id)
    rows = [_snapshot(org.id, subject="kit-a", minutes=i) for i in range(3)]
    other = _snapshot(org.id, subject="kit-b", minutes=9)
    for row in [*rows, other]:
        clean_db.add(row)
        clean_db.flush()
    assert [r.version for r in rows] == [1, 2, 3]
    assert other.version == 1


@requires_db
def test_a_snapshot_cannot_be_updated(clean_db, org) -> None:  # noqa: ANN001
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError

    from app.db.session import bind_tenant

    bind_tenant(clean_db, org.id)
    row = _snapshot(org.id, subject="kit-a")
    clean_db.add(row)
    clean_db.flush()
    nested = clean_db.begin_nested()
    with pytest.raises(DBAPIError):
        clean_db.execute(
            text("UPDATE audit_snapshots SET label = 'tampered' WHERE id = :id"), {"id": row.id}
        )
    nested.rollback()
    nested = clean_db.begin_nested()
    with pytest.raises(DBAPIError):
        clean_db.execute(
            text("UPDATE audit_snapshots SET version = 99 WHERE id = :id"), {"id": row.id}
        )
    nested.rollback()


@requires_db
def test_a_snapshot_can_still_be_deleted_for_retention(clean_db, org) -> None:  # noqa: ANN001
    from sqlalchemy import select

    from app.db.session import bind_tenant

    bind_tenant(clean_db, org.id)
    row = _snapshot(org.id)
    clean_db.add(row)
    clean_db.flush()
    clean_db.delete(row)
    clean_db.flush()
    assert clean_db.execute(select(AuditSnapshot)).scalars().all() == []


@requires_db
def test_a_duplicate_version_is_refused(clean_db, org) -> None:  # noqa: ANN001
    from sqlalchemy.exc import IntegrityError

    from app.db.session import bind_tenant

    bind_tenant(clean_db, org.id)
    clean_db.add(_snapshot(org.id, subject="p1", version=1))
    clean_db.flush()
    nested = clean_db.begin_nested()
    clean_db.add(_snapshot(org.id, subject="p1", minutes=1, version=1))
    with pytest.raises(IntegrityError):
        clean_db.flush()
    nested.rollback()


@requires_db
def test_the_backfill_numbers_oldest_first_without_gaps(clean_db, org) -> None:  # noqa: ANN001
    from sqlalchemy import text

    from app.db.session import bind_tenant

    bind_tenant(clean_db, org.id)
    # Inserted newest first, with explicit, scrambled numbers.
    for minutes, version in ((30, 7), (10, 3), (20, 12), (0, 40)):
        clean_db.add(_snapshot(org.id, subject="s", minutes=minutes, version=version))
    clean_db.add(_snapshot(org.id, subject="t", minutes=5, version=5))
    clean_db.flush()

    nested = clean_db.begin_nested()
    # Simulate the migration's moment: no unique key or update guard yet.
    clean_db.execute(text("ALTER TABLE audit_snapshots DISABLE TRIGGER trg_audit_snapshots_immutable"))
    clean_db.execute(text(
        "ALTER TABLE audit_snapshots DROP CONSTRAINT uq_audit_snapshots_subject_version"
    ))
    clean_db.execute(text(_load_migration().BACKFILL_SQL))
    numbered = clean_db.execute(text(
        "SELECT subject_id, version FROM audit_snapshots ORDER BY subject_id, created_at"
    )).all()
    nested.rollback()

    assert [tuple(r) for r in numbered] == [("s", 1), ("s", 2), ("s", 3), ("s", 4), ("t", 1)]


@requires_db
def test_another_workspaces_metrics_are_invisible(clean_db, org, second_org) -> None:  # noqa: ANN001
    from sqlalchemy import select

    from app.db.session import bind_tenant

    _skip_if_rls_is_bypassed(clean_db)
    bind_tenant(clean_db, org.id)
    asset = _asset(org.id)
    clean_db.add(asset)
    clean_db.flush()
    clean_db.add_all([
        SocialPostMetric(
            tenant_id=org.id, asset_id=asset.id, network="instagram", remote_id="17890",
            fetched_at=datetime.now(UTC), reach=120, likes=8,
        ),
        MetricPoint(
            tenant_id=org.id, day=date(2026, 10, 1), source="google_search_console",
            metric="clicks", value=42.0,
        ),
        _snapshot(org.id, subject="kit-a"),
    ])
    clean_db.commit()

    bind_tenant(clean_db, second_org.id)
    for model in (SocialPostMetric, MetricPoint, AuditSnapshot):
        # Deliberately no tenant filter: RLS alone must hide the rows.
        assert clean_db.execute(select(model)).scalars().all() == [], model.__tablename__

    bind_tenant(clean_db, org.id)
    stored = clean_db.execute(select(SocialPostMetric)).scalar_one()
    assert stored.reach == 120
    assert stored.impressions is None  # unknown stays unknown


@requires_db
def test_a_second_metrics_row_for_one_post_is_refused(clean_db, org) -> None:  # noqa: ANN001
    from sqlalchemy.exc import IntegrityError

    from app.db.session import bind_tenant

    bind_tenant(clean_db, org.id)
    asset = _asset(org.id)
    clean_db.add(asset)
    clean_db.flush()
    now = datetime.now(UTC)
    clean_db.add(SocialPostMetric(tenant_id=org.id, asset_id=asset.id, network="x", fetched_at=now))
    clean_db.flush()
    nested = clean_db.begin_nested()
    clean_db.add(SocialPostMetric(tenant_id=org.id, asset_id=asset.id, network="x", fetched_at=now))
    with pytest.raises(IntegrityError):
        clean_db.flush()
    nested.rollback()
