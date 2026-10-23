"""Billing accounts, usage meters, and plan entitlements.

Revision ID: 0044_commercial_billing
Revises: 0043_workflow_public_api
Create Date: 2026-10-23
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0044_commercial_billing"
down_revision: str | None = "0043_workflow_public_api"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=False)
JSONB = postgresql.JSONB()
TABLES = (
    "billing_accounts",
    "usage_meters",
    "plan_entitlements",
)


def upgrade() -> None:
    existing = set(sa.inspect(op.get_bind()).get_table_names())
    created: list[str] = []

    if "billing_accounts" not in existing:
        op.create_table(
            "billing_accounts",
            sa.Column("id", UUID, primary_key=True),
            sa.Column("tenant_id", UUID, nullable=False),
            sa.Column("provider", sa.String(32), nullable=False, server_default="fake"),
            sa.Column("external_customer_id", sa.String(120), nullable=False, server_default=""),
            sa.Column("status", sa.String(24), nullable=False, server_default="trial"),
            sa.Column("plan_slug", sa.String(64), nullable=False, server_default="trial"),
            sa.Column("seats_purchased", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("secret_encrypted", sa.Text(), nullable=False, server_default=""),
            sa.Column("current_period_end", sa.DateTime(timezone=True), nullable=True),
            sa.Column(
                "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
            ),
            sa.Column(
                "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
            ),
            sa.ForeignKeyConstraint(["tenant_id"], ["organizations.id"], ondelete="CASCADE"),
            sa.UniqueConstraint("tenant_id", name="uq_billing_account_tenant"),
        )
        op.create_index("ix_billing_accounts_tenant_id", "billing_accounts", ["tenant_id"])
        created.append("billing_accounts")

    if "usage_meters" not in existing:
        op.create_table(
            "usage_meters",
            sa.Column("id", UUID, primary_key=True),
            sa.Column("tenant_id", UUID, nullable=False),
            sa.Column("metric", sa.String(64), nullable=False),
            sa.Column("period_start", sa.DateTime(timezone=True), nullable=False),
            sa.Column("period_end", sa.DateTime(timezone=True), nullable=False),
            sa.Column("quantity", sa.Integer(), nullable=False, server_default="0"),
            sa.Column(
                "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
            ),
            sa.Column(
                "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
            ),
            sa.ForeignKeyConstraint(["tenant_id"], ["organizations.id"], ondelete="CASCADE"),
            sa.UniqueConstraint(
                "tenant_id", "metric", "period_start", name="uq_usage_meter_period"
            ),
        )
        op.create_index("ix_usage_meters_tenant_id", "usage_meters", ["tenant_id"])
        op.create_index(
            "ix_usage_meters_tenant_metric", "usage_meters", ["tenant_id", "metric"]
        )
        created.append("usage_meters")

    if "plan_entitlements" not in existing:
        op.create_table(
            "plan_entitlements",
            sa.Column("id", UUID, primary_key=True),
            sa.Column("tenant_id", UUID, nullable=False),
            sa.Column("plan_slug", sa.String(64), nullable=False),
            sa.Column(
                "entitlements", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")
            ),
            sa.Column("synced_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_by_id", UUID, nullable=True),
            sa.Column(
                "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
            ),
            sa.Column(
                "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
            ),
            sa.ForeignKeyConstraint(["tenant_id"], ["organizations.id"], ondelete="CASCADE"),
            sa.UniqueConstraint("tenant_id", name="uq_plan_entitlement_tenant"),
        )
        op.create_index("ix_plan_entitlements_tenant_id", "plan_entitlements", ["tenant_id"])
        created.append("plan_entitlements")

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
