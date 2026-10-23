"""Ensure users.custom_role_id FK exists (create_all gap from 0001).

Revision ID: 0048_ensure_users_custom_role_fk
Revises: 0047_warehouse_exports
Create Date: 2026-10-23

0001_initial_schema builds tables via Base.metadata.create_all, so columns
added to models later can appear before their dedicated migration runs —
without the foreign key that migration intended. 0040 now creates the FK
when missing; this revision repairs databases already past 0040.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0048_ensure_users_custom_role_fk"
down_revision: str | None = "0047_warehouse_exports"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    tables = set(insp.get_table_names())
    if "users" not in tables or "custom_roles" not in tables:
        return
    cols = {c["name"] for c in insp.get_columns("users")}
    if "custom_role_id" not in cols:
        return
    fk_names = {fk.get("name") for fk in insp.get_foreign_keys("users")}
    if "fk_users_custom_role" not in fk_names:
        op.create_foreign_key(
            "fk_users_custom_role",
            "users",
            "custom_roles",
            ["custom_role_id"],
            ["id"],
            ondelete="SET NULL",
        )
    idx_names = {ix.get("name") for ix in insp.get_indexes("users")}
    if "ix_users_custom_role_id" not in idx_names:
        op.create_index("ix_users_custom_role_id", "users", ["custom_role_id"])


def downgrade() -> None:
    # Keep the FK; removing it would re-open the create_all gap. No-op.
    return
