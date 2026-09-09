"""Track when each person last looked at their notifications.

Without this the bell can only say "there is activity in this workspace",
which is true from the first minute and therefore tells nobody anything. The
marker is what lets it say "there is something you have not seen".

Per user and on the server, not in the browser: reading something on a laptop
should not leave it unread on a phone.

Nullable, and null means "never opened" — so on a first visit everything
visible counts as new, which is the right answer rather than a special case.

Revision ID: 0005_notifications_seen
Revises: 0004_currency_neutral
Create Date: 2026-09-08
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_notifications_seen"
down_revision: str | None = "0004_currency_neutral"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    cols = {c["name"] for c in insp.get_columns("users")}
    if "notifications_seen_at" in cols:
        return

    op.add_column(
        "users",
        sa.Column("notifications_seen_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("users", "notifications_seen_at")
