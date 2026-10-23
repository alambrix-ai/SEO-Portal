"""Anomaly alert rules and deduped events.

Revision ID: 0029_alert_rules
Revises: 0028_scheduled_reports
Create Date: 2026-10-23
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0029_alert_rules"
down_revision: str | None = "0028_scheduled_reports"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=False)
JSONB = postgresql.JSONB()
TABLES = ("alert_rules", "alert_events")


def upgrade() -> None:
    existing = set(sa.inspect(op.get_bind()).get_table_names())
    created: list[str] = []
    if "alert_rules" not in existing:
        op.create_table(
            "alert_rules",
            sa.Column("id", UUID, primary_key=True),
            sa.Column("tenant_id", UUID, nullable=False),
            sa.Column("name", sa.String(120), nullable=False),
            sa.Column("metric", sa.String(64), nullable=False),
            sa.Column("threshold", sa.Float(), nullable=False, server_default="0.3"),
            sa.Column("channel", sa.String(32), nullable=False, server_default="email"),
            sa.Column("webhook_url", sa.String(512), nullable=False, server_default=""),
            sa.Column("enabled", sa.String(8), nullable=False, server_default="on"),
            sa.Column(
                "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
            ),
            sa.Column(
                "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
            ),
            sa.ForeignKeyConstraint(["tenant_id"], ["organizations.id"], ondelete="CASCADE"),
            sa.UniqueConstraint("tenant_id", "name", name="uq_alert_rule_name"),
        )
        op.create_index("ix_alert_rules_tenant_id", "alert_rules", ["tenant_id"])
        op.create_index("ix_alert_rule_tenant_enabled", "alert_rules", ["tenant_id", "enabled"])
        created.append("alert_rules")

    if "alert_events" not in existing:
        op.create_table(
            "alert_events",
            sa.Column("id", UUID, primary_key=True),
            sa.Column("tenant_id", UUID, nullable=False),
            sa.Column("rule_id", sa.String(36), nullable=False),
            sa.Column("metric", sa.String(64), nullable=False),
            sa.Column("summary", sa.Text(), nullable=False),
            sa.Column("evidence", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
            sa.Column("dedupe_key", sa.String(120), nullable=False),
            sa.Column("fired_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("status", sa.String(16), nullable=False, server_default="open"),
            sa.Column(
                "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
            ),
            sa.Column(
                "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
            ),
            sa.ForeignKeyConstraint(["tenant_id"], ["organizations.id"], ondelete="CASCADE"),
            sa.UniqueConstraint("tenant_id", "dedupe_key", name="uq_alert_event_dedupe"),
        )
        op.create_index("ix_alert_events_tenant_id", "alert_events", ["tenant_id"])
        op.create_index("ix_alert_events_rule_id", "alert_events", ["rule_id"])
        op.create_index("ix_alert_event_tenant_at", "alert_events", ["tenant_id", "fired_at"])
        created.append("alert_events")

    if created:
        from app.db.rls import apply_policies

        apply_policies(op.get_bind(), tables=tuple(created))


def downgrade() -> None:
    existing = set(sa.inspect(op.get_bind()).get_table_names())
    for table in reversed(TABLES):
        if table not in existing:
            continue
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
        op.drop_table(table)
