"""Content agents: marketing assets and generated images.

Revision ID: 0013_content_assets
Revises: 0012_local_business
Create Date: 2026-10-05
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0013_content_assets"
down_revision: str | None = "0012_local_business"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=False)


def upgrade() -> None:
    existing = set(sa.inspect(op.get_bind()).get_table_names())
    created: list[str] = []

    if "marketing_assets" not in existing:
        op.create_table(
            "marketing_assets",
            sa.Column("id", UUID, primary_key=True),
            sa.Column("tenant_id", UUID, nullable=False),
            sa.Column("agent_slug", sa.String(64), nullable=False),
            sa.Column("run_id", sa.String(36), nullable=False, server_default=""),
            sa.Column("kind", sa.String(24), nullable=False),
            sa.Column("channel", sa.String(40), nullable=False, server_default=""),
            sa.Column("title", sa.String(300), nullable=False, server_default=""),
            sa.Column("content_encrypted", sa.Text(), nullable=False, server_default=""),
            sa.Column("status", sa.String(16), nullable=False, server_default="draft"),
            sa.Column("scheduled_for", sa.DateTime(timezone=True), nullable=True),
            sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("remote_id", sa.String(255), nullable=False, server_default=""),
            sa.Column("remote_url", sa.String(1000), nullable=False, server_default=""),
            sa.Column("error", sa.Text(), nullable=False, server_default=""),
            sa.Column("image_ids", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
            sa.Column("detail", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.ForeignKeyConstraint(["tenant_id"], ["organizations.id"], ondelete="CASCADE"),
        )
        op.create_index("ix_marketing_assets_tenant_id", "marketing_assets", ["tenant_id"])
        op.create_index("ix_marketing_assets_agent_slug", "marketing_assets", ["agent_slug"])
        op.create_index("ix_assets_tenant_kind_status", "marketing_assets", ["tenant_id", "kind", "status"])
        op.create_index("ix_assets_tenant_scheduled", "marketing_assets", ["tenant_id", "scheduled_for"])
        created.append("marketing_assets")

    if "generated_images" not in existing:
        op.create_table(
            "generated_images",
            sa.Column("id", UUID, primary_key=True),
            sa.Column("tenant_id", UUID, nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("prompt", sa.Text(), nullable=False, server_default=""),
            sa.Column("provider", sa.String(40), nullable=False, server_default=""),
            sa.Column("mime", sa.String(40), nullable=False, server_default="image/png"),
            sa.Column("width", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("height", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("data", sa.LargeBinary(), nullable=False),
            sa.ForeignKeyConstraint(["tenant_id"], ["organizations.id"], ondelete="CASCADE"),
        )
        op.create_index("ix_generated_images_tenant_id", "generated_images", ["tenant_id"])
        created.append("generated_images")

    if created:
        from app.db.rls import apply_policies

        apply_policies(op.get_bind(), tables=tuple(created))


def downgrade() -> None:
    for table in ("generated_images", "marketing_assets"):
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
        op.drop_table(table)
