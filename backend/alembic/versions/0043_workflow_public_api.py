"""Approval steps, API tokens, webhooks, ROI attribution snapshots.

Revision ID: 0043_workflow_public_api
Revises: 0042_agency_tier
Create Date: 2026-10-23
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0043_workflow_public_api"
down_revision: str | None = "0042_agency_tier"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=False)
JSONB = postgresql.JSONB()
TABLES = (
    "approval_steps",
    "api_tokens",
    "outbound_webhooks",
    "webhook_delivery_logs",
    "roi_attribution_snapshots",
)


def upgrade() -> None:
    existing = set(sa.inspect(op.get_bind()).get_table_names())
    created: list[str] = []

    if "approval_steps" not in existing:
        op.create_table(
            "approval_steps",
            sa.Column("id", UUID, primary_key=True),
            sa.Column("tenant_id", UUID, nullable=False),
            sa.Column("approval_item_id", UUID, nullable=False),
            sa.Column("step_order", sa.Integer(), nullable=False),
            sa.Column("required_role", sa.String(32), nullable=False, server_default=""),
            sa.Column("approver_email_index", sa.String(64), nullable=False, server_default=""),
            sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
            sa.Column("sla_hours", sa.Integer(), nullable=False, server_default="24"),
            sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("decided_by_id", UUID, nullable=True),
            sa.Column("notes", sa.String(500), nullable=False, server_default=""),
            sa.Column(
                "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
            ),
            sa.Column(
                "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
            ),
            sa.ForeignKeyConstraint(["tenant_id"], ["organizations.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["approval_item_id"], ["approval_items.id"], ondelete="CASCADE"),
            sa.UniqueConstraint("approval_item_id", "step_order", name="uq_approval_step_order"),
        )
        op.create_index("ix_approval_steps_tenant_id", "approval_steps", ["tenant_id"])
        op.create_index("ix_approval_steps_approval_item_id", "approval_steps", ["approval_item_id"])
        op.create_index(
            "ix_approval_steps_tenant_item", "approval_steps", ["tenant_id", "approval_item_id"]
        )
        created.append("approval_steps")

    if "api_tokens" not in existing:
        op.create_table(
            "api_tokens",
            sa.Column("id", UUID, primary_key=True),
            sa.Column("tenant_id", UUID, nullable=False),
            sa.Column("label", sa.String(120), nullable=False),
            sa.Column("token_hash", sa.String(64), nullable=False),
            sa.Column("token_prefix", sa.String(16), nullable=False),
            sa.Column("scopes", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
            sa.Column("meta_encrypted", sa.Text(), nullable=False, server_default=""),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_by_id", UUID, nullable=True),
            sa.Column(
                "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
            ),
            sa.Column(
                "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
            ),
            sa.ForeignKeyConstraint(["tenant_id"], ["organizations.id"], ondelete="CASCADE"),
            sa.UniqueConstraint("tenant_id", "token_hash", name="uq_api_token_hash"),
        )
        op.create_index("ix_api_tokens_tenant_id", "api_tokens", ["tenant_id"])
        op.create_index("ix_api_tokens_prefix", "api_tokens", ["token_prefix"])
        created.append("api_tokens")

    if "outbound_webhooks" not in existing:
        op.create_table(
            "outbound_webhooks",
            sa.Column("id", UUID, primary_key=True),
            sa.Column("tenant_id", UUID, nullable=False),
            sa.Column("name", sa.String(120), nullable=False),
            sa.Column("url", sa.String(1000), nullable=False),
            sa.Column("secret_encrypted", sa.Text(), nullable=False, server_default=""),
            sa.Column("events", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("created_by_id", UUID, nullable=True),
            sa.Column(
                "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
            ),
            sa.Column(
                "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
            ),
            sa.ForeignKeyConstraint(["tenant_id"], ["organizations.id"], ondelete="CASCADE"),
        )
        op.create_index("ix_outbound_webhooks_tenant_id", "outbound_webhooks", ["tenant_id"])
        op.create_index("ix_outbound_webhooks_tenant", "outbound_webhooks", ["tenant_id"])
        created.append("outbound_webhooks")

    if "webhook_delivery_logs" not in existing:
        op.create_table(
            "webhook_delivery_logs",
            sa.Column("id", UUID, primary_key=True),
            sa.Column("tenant_id", UUID, nullable=False),
            sa.Column("webhook_id", UUID, nullable=False),
            sa.Column("event", sa.String(64), nullable=False),
            sa.Column("nonce", sa.String(64), nullable=False),
            sa.Column("signed_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("status_code", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("success", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column(
                "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
            ),
            sa.Column(
                "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
            ),
            sa.ForeignKeyConstraint(["tenant_id"], ["organizations.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["webhook_id"], ["outbound_webhooks.id"], ondelete="CASCADE"),
            sa.UniqueConstraint("tenant_id", "nonce", name="uq_webhook_delivery_nonce"),
        )
        op.create_index(
            "ix_webhook_delivery_logs_tenant_id", "webhook_delivery_logs", ["tenant_id"]
        )
        op.create_index(
            "ix_webhook_delivery_hook", "webhook_delivery_logs", ["tenant_id", "webhook_id"]
        )
        created.append("webhook_delivery_logs")

    if "roi_attribution_snapshots" not in existing:
        op.create_table(
            "roi_attribution_snapshots",
            sa.Column("id", UUID, primary_key=True),
            sa.Column("tenant_id", UUID, nullable=False),
            sa.Column("period_start", sa.DateTime(timezone=True), nullable=False),
            sa.Column("period_end", sa.DateTime(timezone=True), nullable=False),
            sa.Column("channels", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
            sa.Column("blended_roas", sa.String(32), nullable=False, server_default="0"),
            sa.Column("notes", sa.String(500), nullable=False, server_default=""),
            sa.Column("created_by_id", UUID, nullable=True),
            sa.Column(
                "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
            ),
            sa.Column(
                "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
            ),
            sa.ForeignKeyConstraint(["tenant_id"], ["organizations.id"], ondelete="CASCADE"),
        )
        op.create_index(
            "ix_roi_attribution_snapshots_tenant_id", "roi_attribution_snapshots", ["tenant_id"]
        )
        op.create_index(
            "ix_roi_snapshots_tenant_period",
            "roi_attribution_snapshots",
            ["tenant_id", "period_start"],
        )
        created.append("roi_attribution_snapshots")

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
