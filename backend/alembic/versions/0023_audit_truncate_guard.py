"""Refuse TRUNCATE on the audit log.

0020 made ``audit_log`` append-only with a row trigger, but row triggers do
not fire on ``TRUNCATE``, so the owner (or any role holding TRUNCATE) could
still empty the table in one statement. This adds a ``BEFORE TRUNCATE``
statement trigger that calls the same function, so it raises the same
error. See ``app/db/audit_guard.py``.

The statements are frozen copies, not imports: a migration must keep
producing the same schema however the application code changes later.

Revision ID: 0023_audit_truncate_guard
Revises: 0022_creative_tokens_nullable
Create Date: 2026-10-22
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# Kept under 32 characters, the width of alembic_version.version_num.
revision: str = "0023_audit_truncate_guard"
down_revision: str | None = "0022_creative_tokens_nullable"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UPGRADE: tuple[str, ...] = (
    "DROP TRIGGER IF EXISTS trg_audit_log_no_truncate ON audit_log",
    (
        "CREATE TRIGGER trg_audit_log_no_truncate BEFORE TRUNCATE ON audit_log "
        "FOR EACH STATEMENT EXECUTE FUNCTION audit_log_append_only()"
    ),
)

DOWNGRADE: tuple[str, ...] = (
    "DROP TRIGGER IF EXISTS trg_audit_log_no_truncate ON audit_log",
)


def upgrade() -> None:
    for statement in UPGRADE:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWNGRADE:
        op.execute(statement)
