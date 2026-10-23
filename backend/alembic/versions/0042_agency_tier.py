"""Agency parent accounts, client links, and white-label settings.

Revision ID: 0042_agency_tier
Revises: 0041_compliance_residency
Create Date: 2026-10-23

Agency tables are platform-level (no tenant_id). Client data remains under
per-tenant RLS; agency access is explicit membership + AgencyClientLink,
never a shared tenant_id (see PLAN.md Phase 11).
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0042_agency_tier"
down_revision: str | None = "0041_compliance_residency"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=False)
JSONB = postgresql.JSONB()
TABLES = (
    "agencies",
    "agency_memberships",
    "agency_client_links",
    "white_label_settings",
)


def upgrade() -> None:
    existing = set(sa.inspect(op.get_bind()).get_table_names())

    if "agencies" not in existing:
        op.create_table(
            "agencies",
            sa.Column("id", UUID, primary_key=True),
            sa.Column("name", sa.String(160), nullable=False),
            sa.Column("slug", sa.String(80), nullable=False),
            sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column(
                "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
            ),
            sa.Column(
                "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
            ),
            sa.UniqueConstraint("slug", name="uq_agency_slug"),
        )
        op.create_index("ix_agencies_slug", "agencies", ["slug"])

    if "agency_memberships" not in existing:
        op.create_table(
            "agency_memberships",
            sa.Column("id", UUID, primary_key=True),
            sa.Column("agency_id", UUID, nullable=False),
            sa.Column("email_index", sa.String(64), nullable=False),
            sa.Column("role", sa.String(32), nullable=False, server_default="member"),
            sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column(
                "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
            ),
            sa.Column(
                "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
            ),
            sa.ForeignKeyConstraint(["agency_id"], ["agencies.id"], ondelete="CASCADE"),
            sa.UniqueConstraint("agency_id", "email_index", name="uq_agency_member"),
        )
        op.create_index("ix_agency_memberships_agency_id", "agency_memberships", ["agency_id"])
        op.create_index("ix_agency_membership_email", "agency_memberships", ["email_index"])

    if "agency_client_links" not in existing:
        op.create_table(
            "agency_client_links",
            sa.Column("id", UUID, primary_key=True),
            sa.Column("agency_id", UUID, nullable=False),
            sa.Column("client_tenant_id", UUID, nullable=False),
            sa.Column("access_level", sa.String(16), nullable=False, server_default="view"),
            sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column(
                "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
            ),
            sa.Column(
                "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
            ),
            sa.ForeignKeyConstraint(["agency_id"], ["agencies.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["client_tenant_id"], ["organizations.id"], ondelete="CASCADE"),
            sa.UniqueConstraint("agency_id", "client_tenant_id", name="uq_agency_client"),
        )
        op.create_index("ix_agency_client_links_agency_id", "agency_client_links", ["agency_id"])
        op.create_index("ix_agency_client_tenant", "agency_client_links", ["client_tenant_id"])

    if "white_label_settings" not in existing:
        op.create_table(
            "white_label_settings",
            sa.Column("id", UUID, primary_key=True),
            sa.Column("agency_id", UUID, nullable=False),
            sa.Column("custom_domain", sa.String(255), nullable=False, server_default=""),
            sa.Column("branding_encrypted", sa.Text(), nullable=False, server_default=""),
            sa.Column("email_from_name", sa.String(120), nullable=False, server_default=""),
            sa.Column(
                "brand_safety", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")
            ),
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column(
                "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
            ),
            sa.Column(
                "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
            ),
            sa.ForeignKeyConstraint(["agency_id"], ["agencies.id"], ondelete="CASCADE"),
            sa.UniqueConstraint("agency_id", name="uq_white_label_agency"),
        )


def downgrade() -> None:
    existing = set(sa.inspect(op.get_bind()).get_table_names())
    for table in reversed(TABLES):
        if table not in existing:
            continue
        op.drop_table(table)
