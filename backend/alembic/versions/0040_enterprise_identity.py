"""SSO connections, SCIM tokens, custom roles, IP allowlists, approval scopes.

Revision ID: 0040_enterprise_identity
Revises: 0039_playbook_installations
Create Date: 2026-10-23
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0040_enterprise_identity"
down_revision: str | None = "0039_playbook_installations"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=False)
JSONB = postgresql.JSONB()
TABLES = (
    "sso_connections",
    "scim_tokens",
    "custom_roles",
    "ip_allowlist_entries",
)


def upgrade() -> None:
    existing = set(sa.inspect(op.get_bind()).get_table_names())
    created: list[str] = []

    if "sso_connections" not in existing:
        op.create_table(
            "sso_connections",
            sa.Column("id", UUID, primary_key=True),
            sa.Column("tenant_id", UUID, nullable=False),
            sa.Column("slug", sa.String(64), nullable=False),
            sa.Column("name", sa.String(160), nullable=False),
            sa.Column("protocol", sa.String(16), nullable=False),
            sa.Column("issuer", sa.String(512), nullable=False, server_default=""),
            sa.Column("metadata_url", sa.String(1000), nullable=False, server_default=""),
            sa.Column("config_encrypted", sa.Text(), nullable=False, server_default=""),
            sa.Column("jit_provision", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("default_role", sa.String(32), nullable=False, server_default="client"),
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("subject_claim", sa.String(80), nullable=False, server_default="sub"),
            sa.Column(
                "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
            ),
            sa.Column(
                "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
            ),
            sa.ForeignKeyConstraint(["tenant_id"], ["organizations.id"], ondelete="CASCADE"),
            sa.UniqueConstraint("tenant_id", "slug", name="uq_sso_connection_slug"),
        )
        op.create_index("ix_sso_connections_tenant_id", "sso_connections", ["tenant_id"])
        op.create_index(
            "ix_sso_connection_tenant_protocol", "sso_connections", ["tenant_id", "protocol"]
        )
        created.append("sso_connections")

    if "scim_tokens" not in existing:
        op.create_table(
            "scim_tokens",
            sa.Column("id", UUID, primary_key=True),
            sa.Column("tenant_id", UUID, nullable=False),
            sa.Column("label", sa.String(120), nullable=False),
            sa.Column("token_hash", sa.String(64), nullable=False),
            sa.Column("token_prefix", sa.String(12), nullable=False, server_default=""),
            sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_by_id", UUID, nullable=True),
            sa.Column(
                "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
            ),
            sa.Column(
                "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
            ),
            sa.ForeignKeyConstraint(["tenant_id"], ["organizations.id"], ondelete="CASCADE"),
            sa.UniqueConstraint("token_hash", name="uq_scim_token_hash"),
        )
        op.create_index("ix_scim_tokens_tenant_id", "scim_tokens", ["tenant_id"])
        op.create_index("ix_scim_token_tenant_status", "scim_tokens", ["tenant_id", "revoked_at"])
        created.append("scim_tokens")

    if "custom_roles" not in existing:
        op.create_table(
            "custom_roles",
            sa.Column("id", UUID, primary_key=True),
            sa.Column("tenant_id", UUID, nullable=False),
            sa.Column("slug", sa.String(64), nullable=False),
            sa.Column("name", sa.String(120), nullable=False),
            sa.Column("description", sa.Text(), nullable=False, server_default=""),
            sa.Column("permissions", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
            sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column(
                "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
            ),
            sa.Column(
                "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
            ),
            sa.ForeignKeyConstraint(["tenant_id"], ["organizations.id"], ondelete="CASCADE"),
            sa.UniqueConstraint("tenant_id", "slug", name="uq_custom_role_slug"),
        )
        op.create_index("ix_custom_roles_tenant_id", "custom_roles", ["tenant_id"])
        created.append("custom_roles")

    if "ip_allowlist_entries" not in existing:
        op.create_table(
            "ip_allowlist_entries",
            sa.Column("id", UUID, primary_key=True),
            sa.Column("tenant_id", UUID, nullable=False),
            sa.Column("cidr", sa.String(64), nullable=False),
            sa.Column("label", sa.String(120), nullable=False, server_default=""),
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column(
                "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
            ),
            sa.Column(
                "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
            ),
            sa.ForeignKeyConstraint(["tenant_id"], ["organizations.id"], ondelete="CASCADE"),
            sa.UniqueConstraint("tenant_id", "cidr", name="uq_ip_allowlist_cidr"),
        )
        op.create_index("ix_ip_allowlist_entries_tenant_id", "ip_allowlist_entries", ["tenant_id"])
        created.append("ip_allowlist_entries")

    # Scoped approvals + custom role on users
    user_cols = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("users")}
    if "custom_role_id" not in user_cols:
        op.add_column("users", sa.Column("custom_role_id", UUID, nullable=True))
        op.create_foreign_key(
            "fk_users_custom_role",
            "users",
            "custom_roles",
            ["custom_role_id"],
            ["id"],
            ondelete="SET NULL",
        )
        op.create_index("ix_users_custom_role_id", "users", ["custom_role_id"])

    approval_cols = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("approval_items")}
    for col, length in (("scope_brand", 80), ("scope_region", 80), ("scope_channel", 80)):
        if col not in approval_cols:
            op.add_column(
                "approval_items",
                sa.Column(col, sa.String(length), nullable=False, server_default=""),
            )

    # MFA step-up policy flag on orgs
    org_cols = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("organizations")}
    if "ip_allowlist_enabled" not in org_cols:
        op.add_column(
            "organizations",
            sa.Column("ip_allowlist_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        )
    if "mfa_required" not in org_cols:
        op.add_column(
            "organizations",
            sa.Column("mfa_required", sa.Boolean(), nullable=False, server_default=sa.false()),
        )

    if created:
        from app.db.rls import apply_policies

        apply_policies(op.get_bind(), tables=tuple(created))


def downgrade() -> None:
    bind = op.get_bind()
    existing = set(sa.inspect(bind).get_table_names())
    org_cols = (
        {c["name"] for c in sa.inspect(bind).get_columns("organizations")}
        if "organizations" in existing
        else set()
    )
    if "mfa_required" in org_cols:
        op.drop_column("organizations", "mfa_required")
    if "ip_allowlist_enabled" in org_cols:
        op.drop_column("organizations", "ip_allowlist_enabled")

    approval_cols = (
        {c["name"] for c in sa.inspect(bind).get_columns("approval_items")}
        if "approval_items" in existing
        else set()
    )
    for col in ("scope_channel", "scope_region", "scope_brand"):
        if col in approval_cols:
            op.drop_column("approval_items", col)

    user_cols = (
        {c["name"] for c in sa.inspect(bind).get_columns("users")} if "users" in existing else set()
    )
    if "custom_role_id" in user_cols:
        op.drop_index("ix_users_custom_role_id", table_name="users")
        op.drop_constraint("fk_users_custom_role", "users", type_="foreignkey")
        op.drop_column("users", "custom_role_id")

    for table in reversed(TABLES):
        if table not in existing:
            continue
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
        op.drop_table(table)
