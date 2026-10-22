"""Metrics, LLM usage and versions.

* ``metric_points``: one daily figure per organisation, source, metric and
  dimension (Search Console, ad channels, social networks).
* ``social_post_metrics``: the latest totals per published post. Every count
  is nullable: a figure the platform does not report is unknown, not 0.
* ``agent_runs`` records which connector and model a run used.
* ``audit_snapshots`` becomes a numbered version history: a subject, a
  version per (tenant, kind, subject), who or what made it, the model, the
  inputs, and an encrypted payload for customer content. Existing rows are
  numbered by ``created_at``. Triggers number rows inserted without a version
  and refuse UPDATE; DELETE stays allowed for retention.

The two new tables go under row-level security as they are created.

The trigger statements below are frozen copies, not imports: a migration must
keep producing the same schema however the application code changes later.

Revision ID: 0021_metrics_usage_versions
Revises: 0020_audit_append_only
Create Date: 2026-10-21
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# Kept under 32 characters, the width of alembic_version.version_num.
revision: str = "0021_metrics_usage_versions"
down_revision: str | None = "0020_audit_append_only"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=False)
JSONB = postgresql.JSONB()

POST_METRIC_FIELDS: tuple[str, ...] = (
    "impressions",
    "reach",
    "engagements",
    "clicks",
    "likes",
    "comments",
    "shares",
    "saves",
)

#: Creation order; dropped in reverse.
TABLES: tuple[str, ...] = ("metric_points", "social_post_metrics")

#: (name, width) of the string columns added to agent_runs.
AGENT_RUN_COLUMNS: tuple[tuple[str, int], ...] = (("llm_connector", 64), ("llm_model", 128))

#: Columns added to audit_snapshots, other than ``version``.
SNAPSHOT_COLUMNS: tuple[str, ...] = (
    "subject_id",
    "actor_type",
    "agent_slug",
    "model",
    "inputs",
    "payload_encrypted",
)

# Number existing snapshots 1, 2, 3 … per (tenant, kind, subject), oldest
# first; id breaks a tie between rows written in the same instant. Also says
# who made each one, from what the row already records.
BACKFILL_SQL = """
UPDATE audit_snapshots AS s
   SET version = numbered.rn,
       actor_type = CASE
           WHEN s.source = 'agent_run' THEN 'agent'
           WHEN s.created_by <> '' THEN 'user'
           ELSE 'system'
       END
  FROM (
        SELECT id,
               row_number() OVER (
                   PARTITION BY tenant_id, kind, subject_id
                   ORDER BY created_at, id
               ) AS rn
          FROM audit_snapshots
       ) AS numbered
 WHERE s.id = numbered.id
""".strip()

SNAPSHOT_TRIGGER_DDL: tuple[str, ...] = (
    """
CREATE OR REPLACE FUNCTION audit_snapshots_number_version() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, public AS $$
BEGIN
    IF NEW.version IS NULL THEN
        SELECT COALESCE(MAX(s.version), 0) + 1 INTO NEW.version
          FROM audit_snapshots AS s
         WHERE s.tenant_id = NEW.tenant_id
           AND s.kind = NEW.kind
           AND s.subject_id = NEW.subject_id;
    END IF;
    RETURN NEW;
END
$$
""".strip(),
    """
CREATE OR REPLACE FUNCTION audit_snapshots_block_update() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, public AS $$
BEGIN
    RAISE EXCEPTION 'audit_snapshots rows cannot be changed; record a new version instead'
        USING ERRCODE = 'restrict_violation';
END
$$
""".strip(),
    "DROP TRIGGER IF EXISTS trg_audit_snapshots_number_version ON audit_snapshots",
    (
        "CREATE TRIGGER trg_audit_snapshots_number_version BEFORE INSERT ON audit_snapshots "
        "FOR EACH ROW EXECUTE FUNCTION audit_snapshots_number_version()"
    ),
    "DROP TRIGGER IF EXISTS trg_audit_snapshots_immutable ON audit_snapshots",
    (
        "CREATE TRIGGER trg_audit_snapshots_immutable BEFORE UPDATE ON audit_snapshots "
        "FOR EACH ROW EXECUTE FUNCTION audit_snapshots_block_update()"
    ),
)

SNAPSHOT_TRIGGER_DROP: tuple[str, ...] = (
    "DROP TRIGGER IF EXISTS trg_audit_snapshots_immutable ON audit_snapshots",
    "DROP TRIGGER IF EXISTS trg_audit_snapshots_number_version ON audit_snapshots",
    "DROP FUNCTION IF EXISTS audit_snapshots_block_update()",
    "DROP FUNCTION IF EXISTS audit_snapshots_number_version()",
)


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    ]


def _create_tables(existing: set[str]) -> list[str]:
    created: list[str] = []

    if "metric_points" not in existing:
        op.create_table(
            "metric_points",
            sa.Column("id", UUID, primary_key=True),
            sa.Column("tenant_id", UUID, nullable=False),
            sa.Column("day", sa.Date(), nullable=False),
            sa.Column("source", sa.String(64), nullable=False),
            sa.Column("metric", sa.String(32), nullable=False),
            sa.Column("dimension", sa.String(128), nullable=False, server_default=""),
            sa.Column("value", sa.Float(), nullable=False),
            *_timestamps(),
            sa.ForeignKeyConstraint(["tenant_id"], ["organizations.id"], ondelete="CASCADE"),
            sa.UniqueConstraint(
                "tenant_id", "day", "source", "metric", "dimension", name="uq_metric_points_key"
            ),
            sa.CheckConstraint("source <> ''", name="ck_metric_points_source"),
            sa.CheckConstraint("metric <> ''", name="ck_metric_points_metric"),
        )
        op.create_index("ix_metric_points_tenant_id", "metric_points", ["tenant_id"])
        op.create_index(
            "ix_metric_points_tenant_metric_day", "metric_points", ["tenant_id", "metric", "day"]
        )
        created.append("metric_points")

    if "social_post_metrics" not in existing:
        op.create_table(
            "social_post_metrics",
            sa.Column("id", UUID, primary_key=True),
            sa.Column("tenant_id", UUID, nullable=False),
            sa.Column("asset_id", UUID, nullable=False),
            sa.Column("network", sa.String(32), nullable=False),
            sa.Column("remote_id", sa.String(255), nullable=False, server_default=""),
            sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
            *(sa.Column(field, sa.BigInteger(), nullable=True) for field in POST_METRIC_FIELDS),
            *_timestamps(),
            sa.ForeignKeyConstraint(["tenant_id"], ["organizations.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["asset_id"], ["marketing_assets.id"], ondelete="CASCADE"),
            sa.UniqueConstraint("asset_id", name="uq_social_post_metrics_asset"),
            *(
                sa.CheckConstraint(
                    f"{field} IS NULL OR {field} >= 0", name=f"ck_social_post_metrics_{field}"
                )
                for field in POST_METRIC_FIELDS
            ),
        )
        op.create_index("ix_social_post_metrics_tenant_id", "social_post_metrics", ["tenant_id"])
        op.create_index(
            "ix_social_post_metrics_tenant_network_fetched",
            "social_post_metrics",
            ["tenant_id", "network", "fetched_at"],
        )
        created.append("social_post_metrics")

    return created


def _upgrade_agent_runs(columns: set[str]) -> None:
    for name, width in AGENT_RUN_COLUMNS:
        if name not in columns:
            op.add_column(
                "agent_runs",
                sa.Column(name, sa.String(width), nullable=False, server_default=""),
            )


def _upgrade_snapshots(columns: set[str]) -> None:
    if "version" in columns:
        # Already upgraded; only make sure the triggers are in place.
        for statement in SNAPSHOT_TRIGGER_DDL:
            op.execute(statement)
        return

    additions = {
        "subject_id": sa.Column("subject_id", sa.String(64), nullable=False, server_default=""),
        "actor_type": sa.Column("actor_type", sa.String(16), nullable=False, server_default=""),
        "agent_slug": sa.Column("agent_slug", sa.String(64), nullable=False, server_default=""),
        "model": sa.Column("model", sa.String(128), nullable=False, server_default=""),
        "inputs": sa.Column("inputs", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        "payload_encrypted": sa.Column("payload_encrypted", sa.Text(), nullable=False, server_default=""),
    }
    for name in SNAPSHOT_COLUMNS:
        if name not in columns:
            op.add_column("audit_snapshots", additions[name])
    # Nullable until the backfill has numbered every existing row.
    op.add_column("audit_snapshots", sa.Column("version", sa.Integer(), nullable=True))

    # The table is under FORCE row-level security, which would hide every
    # row from the backfill (no tenant is pinned during a migration). Lift
    # FORCE for this one statement, inside the migration's transaction, and
    # put it straight back.
    op.execute("ALTER TABLE audit_snapshots NO FORCE ROW LEVEL SECURITY")
    op.execute(BACKFILL_SQL)
    op.execute("ALTER TABLE audit_snapshots FORCE ROW LEVEL SECURITY")

    op.alter_column("audit_snapshots", "version", existing_type=sa.Integer(), nullable=False)
    op.create_check_constraint("ck_audit_snapshots_version", "audit_snapshots", "version >= 1")
    op.create_unique_constraint(
        "uq_audit_snapshots_subject_version",
        "audit_snapshots",
        ["tenant_id", "kind", "subject_id", "version"],
    )
    op.create_index(
        "ix_audit_snapshots_tenant_kind_subject_created",
        "audit_snapshots",
        ["tenant_id", "kind", "subject_id", "created_at"],
    )
    for statement in SNAPSHOT_TRIGGER_DDL:
        op.execute(statement)


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    existing = set(inspector.get_table_names())

    created = _create_tables(existing)
    if created:
        from app.db.rls import apply_policies

        apply_policies(op.get_bind(), tables=tuple(created))

    _upgrade_agent_runs({c["name"] for c in inspector.get_columns("agent_runs")})
    _upgrade_snapshots({c["name"] for c in inspector.get_columns("audit_snapshots")})


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    existing = set(inspector.get_table_names())

    # Triggers first, so nothing below is refused by them.
    for statement in SNAPSHOT_TRIGGER_DROP:
        op.execute(statement)

    snapshot_columns = {c["name"] for c in inspector.get_columns("audit_snapshots")}
    if "version" in snapshot_columns:
        op.drop_index("ix_audit_snapshots_tenant_kind_subject_created", table_name="audit_snapshots")
        op.drop_constraint("uq_audit_snapshots_subject_version", "audit_snapshots", type_="unique")
        op.drop_constraint("ck_audit_snapshots_version", "audit_snapshots", type_="check")
        op.drop_column("audit_snapshots", "version")
    for name in reversed(SNAPSHOT_COLUMNS):
        if name in snapshot_columns:
            op.drop_column("audit_snapshots", name)

    run_columns = {c["name"] for c in inspector.get_columns("agent_runs")}
    for name, _width in reversed(AGENT_RUN_COLUMNS):
        if name in run_columns:
            op.drop_column("agent_runs", name)

    # Reverse creation order. Indexes and constraints go with their table.
    for table in reversed(TABLES):
        if table not in existing:
            continue
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
        op.drop_table(table)
