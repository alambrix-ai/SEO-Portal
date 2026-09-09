"""Rename ``agent_runs.cost_usd`` to ``cost``.

The last column named for a currency it does not necessarily hold. It was
deliberately exempted in 0004 on the grounds that a model call is billed in
US dollars — which stopped being true of *this* column when the rates moved
into configuration. ``LLM_PRICE_INPUT_PER_MTOK`` and its output counterpart
are whatever the operator is contracted at, in whatever currency they think
in, so what lands here is rupees for a deployment that entered rupees.

A rename, not a drop: same numbers, no back-fill.

Revision ID: 0007_cost_currency_neutral
Revises: 0006_seo_issues
Create Date: 2026-09-09
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007_cost_currency_neutral"
down_revision: str | None = "0006_seo_issues"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    cols = {c["name"] for c in insp.get_columns("agent_runs")}
    if "cost_usd" not in cols:
        return

    op.alter_column("agent_runs", "cost_usd", new_column_name="cost")


def downgrade() -> None:
    op.alter_column("agent_runs", "cost", new_column_name="cost_usd")
