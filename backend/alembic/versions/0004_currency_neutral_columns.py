"""Drop the ``_usd`` suffix from the columns that hold the customer's money.

The platform reports in the currency the customer budgets in — rupees by
default (``CURRENCY_SYMBOL``). Columns named ``ad_spend_usd`` holding rupees
are the kind of thing that produces a confidently wrong number in front of a
customer two years from now, so the names are corrected rather than left as a
comment somebody has to find.

Renames, not drops: the data is the same numbers, so nothing is lost and no
back-fill is needed.

``agent_runs.cost_usd`` is deliberately **left alone**. That column is what
Anthropic charges for a model call, Anthropic bills in US dollars, and
relabelling it would either be a lie or an invented conversion rate.

Revision ID: 0004_currency_neutral
Revises: 0003_one_pending_per_target
Create Date: 2026-09-08
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0004_currency_neutral"
down_revision: str | None = "0003_one_pending_per_target"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_RENAMES: tuple[tuple[str, str, str], ...] = (
    ("budget_allocations", "spend_usd", "spend"),
    ("daily_metrics", "ad_spend_usd", "ad_spend"),
    ("daily_metrics", "spend_saved_usd", "spend_saved"),
    ("fraud_events", "spend_saved_usd", "spend_saved"),
)


def upgrade() -> None:
    for table, old, new in _RENAMES:
        op.alter_column(table, old, new_column_name=new)


def downgrade() -> None:
    for table, old, new in _RENAMES:
        op.alter_column(table, new, new_column_name=old)
