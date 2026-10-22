"""Sign-in provider apps set up in Portal Admin.

One row per provider (Google, Meta, ...): the platform's own app with that
vendor, so "Sign in with <Vendor>" can be switched on from Portal Admin
instead of the server environment. Platform-level, like
portal_feature_flags: no tenant_id and outside row-level security. Secrets
are stored encrypted under a key derived from the platform master key.

Revision ID: 0017_platform_oauth_apps
Revises: 0016_oauth_states
Create Date: 2026-10-13
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0017_platform_oauth_apps"
down_revision: str | None = "0016_oauth_states"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=False)


def upgrade() -> None:
    existing = set(sa.inspect(op.get_bind()).get_table_names())
    if "platform_oauth_apps" in existing:
        return

    op.create_table(
        "platform_oauth_apps",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("client_id", sa.Text(), nullable=False, server_default=""),
        sa.Column("client_secret_encrypted", sa.Text(), nullable=False, server_default=""),
        sa.Column("extras_encrypted", sa.Text(), nullable=False, server_default=""),
        sa.Column("updated_by", sa.String(255), nullable=False, server_default=""),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("provider", name="uq_platform_oauth_apps_provider"),
    )


def downgrade() -> None:
    op.drop_table("platform_oauth_apps")
