"""Creative job token counts may be unknown.

A self-hosted model server (vLLM, Ollama) may answer without token counts.
``creative_jobs.tokens_in`` / ``tokens_out`` become nullable so a job can
record "not reported" as NULL instead of a made-up 0; the quota keeps the
token reservation in ``tokens_out`` (``quotas.settle_tokens``), so NULL never
hides usage.

Downgrade turns NULL back into 0 and restores NOT NULL. ``creative_jobs`` is
under FORCE row-level security, which would hide every row from that UPDATE
(no tenant is pinned during a migration), so FORCE is lifted for the one
statement inside the migration's transaction and put straight back, as 0021
does for its backfill.

Revision ID: 0022_creative_tokens_nullable
Revises: 0021_metrics_usage_versions
Create Date: 2026-10-22
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# Kept under 32 characters, the width of alembic_version.version_num.
revision: str = "0022_creative_tokens_nullable"
down_revision: str | None = "0021_metrics_usage_versions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "creative_jobs"
COLUMNS: tuple[str, ...] = ("tokens_in", "tokens_out")

COALESCE_SQL = (
    "UPDATE creative_jobs SET tokens_in = COALESCE(tokens_in, 0), "
    "tokens_out = COALESCE(tokens_out, 0) "
    "WHERE tokens_in IS NULL OR tokens_out IS NULL"
)


def _present() -> list[str]:
    inspector = sa.inspect(op.get_bind())
    if TABLE not in set(inspector.get_table_names()):
        return []
    have = {c["name"] for c in inspector.get_columns(TABLE)}
    return [c for c in COLUMNS if c in have]


def upgrade() -> None:
    for column in _present():
        op.alter_column(TABLE, column, existing_type=sa.Integer(), nullable=True)


def downgrade() -> None:
    columns = _present()
    if not columns:
        return
    op.execute(f"ALTER TABLE {TABLE} NO FORCE ROW LEVEL SECURITY")
    op.execute(COALESCE_SQL)
    op.execute(f"ALTER TABLE {TABLE} FORCE ROW LEVEL SECURITY")
    for column in columns:
        op.alter_column(TABLE, column, existing_type=sa.Integer(), nullable=False)
