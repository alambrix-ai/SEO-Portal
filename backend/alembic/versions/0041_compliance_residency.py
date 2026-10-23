"""Retention policies, subject requests, audit exports, AI governance.

Revision ID: 0041_compliance_residency
Revises: 0040_enterprise_identity
Create Date: 2026-10-23
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0041_compliance_residency"
down_revision: str | None = "0040_enterprise_identity"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=False)
JSONB = postgresql.JSONB()
TABLES = (
    "retention_policies",
    "subject_requests",
    "audit_export_jobs",
    "ai_governance_policies",
)


def upgrade() -> None:
    existing = set(sa.inspect(op.get_bind()).get_table_names())
    created: list[str] = []

    if "retention_policies" not in existing:
        op.create_table(
            "retention_policies",
            sa.Column("id", UUID, primary_key=True),
            sa.Column("tenant_id", UUID, nullable=False),
            sa.Column("data_class", sa.String(64), nullable=False),
            sa.Column("ttl_days", sa.Integer(), nullable=False),
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column(
                "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
            ),
            sa.Column(
                "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
            ),
            sa.ForeignKeyConstraint(["tenant_id"], ["organizations.id"], ondelete="CASCADE"),
            sa.UniqueConstraint("tenant_id", "data_class", name="uq_retention_data_class"),
        )
        op.create_index("ix_retention_policies_tenant_id", "retention_policies", ["tenant_id"])
        created.append("retention_policies")

    if "subject_requests" not in existing:
        op.create_table(
            "subject_requests",
            sa.Column("id", UUID, primary_key=True),
            sa.Column("tenant_id", UUID, nullable=False),
            sa.Column("kind", sa.String(16), nullable=False),
            sa.Column("subject_email_index", sa.String(64), nullable=False),
            sa.Column("status", sa.String(16), nullable=False, server_default="received"),
            sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("evidence", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
            sa.Column("result_encrypted", sa.Text(), nullable=False, server_default=""),
            sa.Column("requested_by_id", UUID, nullable=True),
            sa.Column(
                "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
            ),
            sa.Column(
                "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
            ),
            sa.ForeignKeyConstraint(["tenant_id"], ["organizations.id"], ondelete="CASCADE"),
        )
        op.create_index("ix_subject_requests_tenant_id", "subject_requests", ["tenant_id"])
        op.create_index(
            "ix_subject_requests_subject_email_index", "subject_requests", ["subject_email_index"]
        )
        created.append("subject_requests")

    if "audit_export_jobs" not in existing:
        op.create_table(
            "audit_export_jobs",
            sa.Column("id", UUID, primary_key=True),
            sa.Column("tenant_id", UUID, nullable=False),
            sa.Column("format", sa.String(16), nullable=False, server_default="json"),
            sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
            sa.Column("window_end", sa.DateTime(timezone=True), nullable=False),
            sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
            sa.Column("signature", sa.String(128), nullable=False, server_default=""),
            sa.Column("row_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("body_encrypted", sa.Text(), nullable=False, server_default=""),
            sa.Column("siem_url", sa.String(1000), nullable=False, server_default=""),
            sa.Column("created_by_id", UUID, nullable=True),
            sa.Column(
                "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
            ),
            sa.Column(
                "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
            ),
            sa.ForeignKeyConstraint(["tenant_id"], ["organizations.id"], ondelete="CASCADE"),
        )
        op.create_index("ix_audit_export_jobs_tenant_id", "audit_export_jobs", ["tenant_id"])
        created.append("audit_export_jobs")

    if "ai_governance_policies" not in existing:
        op.create_table(
            "ai_governance_policies",
            sa.Column("id", UUID, primary_key=True),
            sa.Column("tenant_id", UUID, nullable=False),
            sa.Column(
                "model_allowlist", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")
            ),
            sa.Column(
                "agent_cost_caps", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")
            ),
            sa.Column("log_prompts", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("log_actions", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column(
                "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
            ),
            sa.Column(
                "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
            ),
            sa.ForeignKeyConstraint(["tenant_id"], ["organizations.id"], ondelete="CASCADE"),
            sa.UniqueConstraint("tenant_id", name="uq_ai_governance_tenant"),
        )
        op.create_index(
            "ix_ai_governance_policies_tenant_id", "ai_governance_policies", ["tenant_id"]
        )
        created.append("ai_governance_policies")

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
