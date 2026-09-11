"""Multi-workspace membership: one email across many organisations.

Revision ID: 0011_workspace_memberships
Revises: 0010_portal_feature_flags
Create Date: 2026-09-11
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011_workspace_memberships"
down_revision: str | None = "0010_portal_feature_flags"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _drop_unique_if_present(table: str, name: str) -> None:
    """Drop a unique constraint or unique index by name (idempotent)."""
    bind = op.get_bind()
    insp = sa.inspect(bind)
    uniques = {u["name"] for u in insp.get_unique_constraints(table)}
    if name in uniques:
        op.drop_constraint(name, table, type_="unique")
        return
    indexes = {
        i["name"]
        for i in insp.get_indexes(table)
        if i.get("unique") and i["name"] == name
    }
    if name in indexes:
        op.drop_index(name, table_name=table)


def upgrade() -> None:
    """Additive only: keep all existing org/user/agent rows; backfill memberships."""
    bind = op.get_bind()
    insp = sa.inspect(bind)

    # users: global email uniqueness -> per-tenant (data preserved)
    _drop_unique_if_present("users", "uq_users_email_index")
    insp = sa.inspect(bind)
    user_uniques = {u["name"] for u in insp.get_unique_constraints("users")}
    if "uq_users_tenant_email" not in user_uniques:
        op.create_unique_constraint(
            "uq_users_tenant_email", "users", ["tenant_id", "email_index"]
        )

    # auth_identities.user_id was unique (one email -> one user). Keep the
    # columns as last-active pointers; drop the uniqueness.
    insp = sa.inspect(bind)
    for uniq in insp.get_unique_constraints("auth_identities"):
        if uniq["column_names"] == ["user_id"]:
            _drop_unique_if_present("auth_identities", uniq["name"])
    for idx in insp.get_indexes("auth_identities"):
        if idx.get("unique") and idx["column_names"] == ["user_id"]:
            _drop_unique_if_present("auth_identities", idx["name"])

    insp = sa.inspect(bind)
    if "workspace_memberships" not in insp.get_table_names():
        op.create_table(
            "workspace_memberships",
            sa.Column("id", sa.UUID(as_uuid=False), primary_key=True),
            sa.Column("email_index", sa.String(length=44), nullable=False),
            sa.Column("tenant_id", sa.UUID(as_uuid=False), nullable=False),
            sa.Column("user_id", sa.UUID(as_uuid=False), nullable=False),
            sa.Column(
                "is_active",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("true"),
            ),
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
            sa.ForeignKeyConstraint(
                ["tenant_id"], ["organizations.id"], ondelete="CASCADE"
            ),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
            sa.UniqueConstraint(
                "email_index", "tenant_id", name="uq_membership_email_tenant"
            ),
        )
        op.create_index(
            "ix_membership_email", "workspace_memberships", ["email_index"]
        )
        op.create_index(
            "ix_membership_tenant", "workspace_memberships", ["tenant_id"]
        )
        op.create_index("ix_membership_user", "workspace_memberships", ["user_id"])

    # Backfill: every existing user is a membership of their org (safe to re-run).
    op.execute(
        sa.text(
            """
            INSERT INTO workspace_memberships (
                id, email_index, tenant_id, user_id, is_active, created_at, updated_at
            )
            SELECT
                    gen_random_uuid(),
                email_index,
                tenant_id,
                id,
                COALESCE(is_active, true),
                now(),
                now()
            FROM users
            ON CONFLICT (email_index, tenant_id) DO NOTHING
            """
        )
    )


def downgrade() -> None:
    op.drop_table("workspace_memberships")
    _drop_unique_if_present("users", "uq_users_tenant_email")
    op.create_unique_constraint("uq_users_email_index", "users", ["email_index"])
    op.create_unique_constraint(
        "auth_identities_user_id_key", "auth_identities", ["user_id"]
    )
