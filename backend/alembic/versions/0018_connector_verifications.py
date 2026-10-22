"""Live connector verification results, and platform settings.

``connector_verifications``: one row per live verification run of a connector
type (``manage.py verify`` or Portal Admin -> Verification). The latest ok run
within 30 days makes a connector "verified"; anything else shows "Beta".

``platform_settings``: small platform-wide settings set in Portal Admin, as a
key and a JSON value. The first is ``hide_unverified_connectors``.

Both are platform-level, like portal_feature_flags: no tenant_id and outside
row-level security.

Revision ID: 0018_connector_verifications
Revises: 0017_platform_oauth_apps
Create Date: 2026-10-13
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0018_connector_verifications"
down_revision: str | None = "0017_platform_oauth_apps"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=False)
JSONB = postgresql.JSONB()


def upgrade() -> None:
    existing = set(sa.inspect(op.get_bind()).get_table_names())

    if "connector_verifications" not in existing:
        op.create_table(
            "connector_verifications",
            sa.Column("id", UUID, primary_key=True),
            sa.Column("slug", sa.String(64), nullable=False),
            sa.Column("environment", sa.String(32), nullable=False, server_default="live"),
            sa.Column("ok", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("steps", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
            sa.Column("note", sa.Text(), nullable=False, server_default=""),
            sa.Column("run_by", sa.String(255), nullable=False, server_default=""),
            sa.Column("workspace_id", UUID, nullable=True),
            sa.Column(
                "started_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.Column(
                "finished_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
        )
        op.create_index(
            "ix_connector_verifications_slug_finished",
            "connector_verifications",
            ["slug", "finished_at"],
        )

    if "platform_settings" not in existing:
        op.create_table(
            "platform_settings",
            sa.Column("id", UUID, primary_key=True),
            sa.Column("key", sa.String(64), nullable=False),
            sa.Column("value", JSONB, nullable=True),
            sa.Column("updated_by", sa.String(255), nullable=False, server_default=""),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.UniqueConstraint("key", name="uq_platform_settings_key"),
        )


def downgrade() -> None:
    op.drop_table("platform_settings")
    op.drop_index(
        "ix_connector_verifications_slug_finished", table_name="connector_verifications"
    )
    op.drop_table("connector_verifications")
