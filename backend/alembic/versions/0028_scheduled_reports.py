"""Scheduled reports and encrypted report artifacts.

Revision ID: 0028_scheduled_reports
Revises: 0027_willy_grounding
Create Date: 2026-10-23
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0028_scheduled_reports"
down_revision: str | None = "0027_willy_grounding"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=False)
JSONB = postgresql.JSONB()
TABLES = ("scheduled_reports", "report_artifacts")


def upgrade() -> None:
    existing = set(sa.inspect(op.get_bind()).get_table_names())
    created: list[str] = []
    if "scheduled_reports" not in existing:
        op.create_table(
            "scheduled_reports",
            sa.Column("id", UUID, primary_key=True),
            sa.Column("tenant_id", UUID, nullable=False),
            sa.Column("kind", sa.String(40), nullable=False, server_default="weekly_autopilot"),
            sa.Column("status", sa.String(16), nullable=False, server_default="on"),
            sa.Column(
                "recipient_user_ids", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")
            ),
            sa.Column("last_sent_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column(
                "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
            ),
            sa.Column(
                "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
            ),
            sa.ForeignKeyConstraint(["tenant_id"], ["organizations.id"], ondelete="CASCADE"),
        )
        op.create_index("ix_scheduled_reports_tenant_id", "scheduled_reports", ["tenant_id"])
        op.create_index(
            "ix_scheduled_report_tenant_kind", "scheduled_reports", ["tenant_id", "kind"]
        )
        created.append("scheduled_reports")

    if "report_artifacts" not in existing:
        op.create_table(
            "report_artifacts",
            sa.Column("id", UUID, primary_key=True),
            sa.Column("tenant_id", UUID, nullable=False),
            sa.Column("schedule_id", UUID, nullable=True),
            sa.Column("kind", sa.String(40), nullable=False, server_default="weekly_autopilot"),
            sa.Column("subject", sa.String(200), nullable=False, server_default=""),
            sa.Column("body_encrypted", sa.Text(), nullable=False, server_default=""),
            sa.Column("storage_ref", sa.String(512), nullable=False, server_default=""),
            sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column(
                "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
            ),
            sa.Column(
                "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
            ),
            sa.ForeignKeyConstraint(["tenant_id"], ["organizations.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["schedule_id"], ["scheduled_reports.id"], ondelete="SET NULL"),
        )
        op.create_index("ix_report_artifacts_tenant_id", "report_artifacts", ["tenant_id"])
        op.create_index("ix_report_artifacts_schedule_id", "report_artifacts", ["schedule_id"])
        op.create_index(
            "ix_report_artifact_tenant_at", "report_artifacts", ["tenant_id", "generated_at"]
        )
        created.append("report_artifacts")

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
