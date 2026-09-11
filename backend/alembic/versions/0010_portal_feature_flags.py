"""Platform feature flags for Portal Admin.

Revision ID: 0010_portal_feature_flags
Revises: 0009_agent_llm_connector
Create Date: 2026-09-11
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010_portal_feature_flags"
down_revision: str | None = "0009_agent_llm_connector"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "portal_feature_flags" in insp.get_table_names():
        return

    op.create_table(
        "portal_feature_flags",
        sa.Column("id", sa.UUID(as_uuid=False), primary_key=True),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("slug", sa.String(length=64), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("updated_by", sa.String(length=255), nullable=False, server_default=""),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.UniqueConstraint("kind", "slug", name="uq_portal_feature_kind_slug"),
    )
    op.create_index("ix_portal_feature_flags_kind", "portal_feature_flags", ["kind"])
    op.create_index("ix_portal_feature_flags_slug", "portal_feature_flags", ["slug"])


def downgrade() -> None:
    op.drop_table("portal_feature_flags")
