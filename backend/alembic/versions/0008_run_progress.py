"""Where a running pass has got to.

A pass is started and not awaited now, and it can take minutes — six model
calls for the analysis step alone. Without this the console could only show a
status of "running" and a summary from the *previous* pass, which for two
minutes is indistinguishable from stuck.

Revision ID: 0008_run_progress
Revises: 0007_cost_currency_neutral
Create Date: 2026-09-09
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008_run_progress"
down_revision: str | None = "0007_cost_currency_neutral"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    cols = {c["name"] for c in insp.get_columns("agent_runs")}
    if "step" in cols:
        return

    op.add_column(
        "agent_runs",
        sa.Column("step", sa.String(length=120), nullable=False, server_default=""),
    )
    op.add_column(
        "agent_runs",
        sa.Column("step_done", sa.Integer(), nullable=False, server_default="0"),
    )
    # 0 where the step has no countable total — "reading the sitemap".
    op.add_column(
        "agent_runs",
        sa.Column("step_total", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("agent_runs", "step_total")
    op.drop_column("agent_runs", "step_done")
    op.drop_column("agent_runs", "step")
