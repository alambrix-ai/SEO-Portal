"""Landing pages and A/B experiment metadata.

Revision ID: 0035_landing_page_ab
Revises: 0034_cross_channel_budget
Create Date: 2026-10-23
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0035_landing_page_ab"
down_revision: str | None = "0034_cross_channel_budget"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=False)
JSONB = postgresql.JSONB()
TABLES = ("landing_pages", "ab_experiments")


def upgrade() -> None:
    existing = set(sa.inspect(op.get_bind()).get_table_names())
    created: list[str] = []

    if "landing_pages" not in existing:
        op.create_table(
            "landing_pages",
            sa.Column("id", UUID, primary_key=True),
            sa.Column("tenant_id", UUID, nullable=False),
            sa.Column("title", sa.String(255), nullable=False),
            sa.Column("path", sa.String(512), nullable=False),
            sa.Column("body_encrypted", sa.Text(), nullable=False, server_default=""),
            sa.Column("status", sa.String(16), nullable=False, server_default="draft"),
            sa.Column("cms_id", sa.String(128), nullable=False, server_default=""),
            sa.Column("cms_slug", sa.String(64), nullable=False, server_default=""),
            sa.Column("cms_url", sa.String(1000), nullable=False, server_default=""),
            sa.Column("approval_item_id", UUID, nullable=True),
            sa.Column("proposed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column(
                "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
            ),
            sa.Column(
                "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
            ),
            sa.ForeignKeyConstraint(["tenant_id"], ["organizations.id"], ondelete="CASCADE"),
            sa.UniqueConstraint("tenant_id", "path", name="uq_landing_page_path"),
        )
        op.create_index("ix_landing_pages_tenant_id", "landing_pages", ["tenant_id"])
        op.create_index(
            "ix_landing_page_tenant_status", "landing_pages", ["tenant_id", "status"]
        )
        created.append("landing_pages")

    if "ab_experiments" not in existing:
        op.create_table(
            "ab_experiments",
            sa.Column("id", UUID, primary_key=True),
            sa.Column("tenant_id", UUID, nullable=False),
            sa.Column("slug", sa.String(80), nullable=False),
            sa.Column("name", sa.String(160), nullable=False),
            sa.Column("hypothesis", sa.Text(), nullable=False, server_default=""),
            sa.Column("control_page_id", UUID, nullable=False),
            sa.Column("variant_page_id", UUID, nullable=False),
            sa.Column("variant_weight", sa.Integer(), nullable=False, server_default="50"),
            sa.Column("metric_key", sa.String(40), nullable=False, server_default="sessions"),
            sa.Column("control_metric", sa.Float(), nullable=False, server_default="0"),
            sa.Column("variant_metric", sa.Float(), nullable=False, server_default="0"),
            sa.Column("status", sa.String(16), nullable=False, server_default="proposed"),
            sa.Column("approval_item_id", UUID, nullable=True),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("detail", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
            sa.Column(
                "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
            ),
            sa.Column(
                "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
            ),
            sa.ForeignKeyConstraint(["tenant_id"], ["organizations.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["control_page_id"], ["landing_pages.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["variant_page_id"], ["landing_pages.id"], ondelete="CASCADE"),
            sa.UniqueConstraint("tenant_id", "slug", name="uq_ab_experiment_slug"),
        )
        op.create_index("ix_ab_experiments_tenant_id", "ab_experiments", ["tenant_id"])
        op.create_index("ix_ab_experiments_control_page_id", "ab_experiments", ["control_page_id"])
        op.create_index("ix_ab_experiments_variant_page_id", "ab_experiments", ["variant_page_id"])
        op.create_index(
            "ix_ab_experiment_tenant_status", "ab_experiments", ["tenant_id", "status"]
        )
        created.append("ab_experiments")

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
