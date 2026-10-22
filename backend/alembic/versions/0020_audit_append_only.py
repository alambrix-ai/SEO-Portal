"""Append-only audit log.

A trigger rejects UPDATE and DELETE on ``audit_log`` for every role. A guarded
REVOKE removes UPDATE, DELETE and TRUNCATE from ``automarket_app`` where that
role exists. See ``app/db/audit_guard.py`` for the reasoning and its
consequences (an organisation can no longer be deleted by cascade).

The statements are frozen copies, not imports: a migration must keep
producing the same schema however the application code changes later.

Revision ID: 0020_audit_append_only
Revises: 0019_creative_studio
Create Date: 2026-10-21
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# Kept under 32 characters, the width of alembic_version.version_num.
revision: str = "0020_audit_append_only"
down_revision: str | None = "0019_creative_studio"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_GUARDED = (
    "DO $$ BEGIN "
    "IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'automarket_app') THEN "
    "{statement}; "
    "END IF; "
    "END $$"
)

UPGRADE: tuple[str, ...] = (
    (
        "CREATE OR REPLACE FUNCTION audit_log_append_only() RETURNS trigger "
        "LANGUAGE plpgsql AS $$ BEGIN "
        "RAISE EXCEPTION 'audit_log is append-only: % is not allowed', TG_OP "
        "USING ERRCODE = '42501'; "
        "END $$"
    ),
    "DROP TRIGGER IF EXISTS trg_audit_log_append_only ON audit_log",
    (
        "CREATE TRIGGER trg_audit_log_append_only BEFORE UPDATE OR DELETE ON audit_log "
        "FOR EACH ROW EXECUTE FUNCTION audit_log_append_only()"
    ),
    _GUARDED.format(statement="REVOKE UPDATE, DELETE, TRUNCATE ON audit_log FROM automarket_app"),
)

DOWNGRADE: tuple[str, ...] = (
    "DROP TRIGGER IF EXISTS trg_audit_log_append_only ON audit_log",
    "DROP FUNCTION IF EXISTS audit_log_append_only()",
    _GUARDED.format(statement="GRANT UPDATE, DELETE ON audit_log TO automarket_app"),
)


def upgrade() -> None:
    for statement in UPGRADE:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWNGRADE:
        op.execute(statement)
