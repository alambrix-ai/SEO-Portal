"""Which connected AI model an agent should write with.

Agents no longer use process-wide LLM_PROVIDER / LLM_MODEL credentials.
Each agent that needs a model stores the connector slug the operator picked.

Revision ID: 0009_agent_llm_connector
Revises: 0008_run_progress
Create Date: 2026-09-11
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009_agent_llm_connector"
down_revision: str | None = "0008_run_progress"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    cols = {c["name"] for c in insp.get_columns("agents")}
    if "llm_connector" in cols:
        return

    op.add_column(
        "agents",
        sa.Column(
            "llm_connector",
            sa.String(length=64),
            nullable=False,
            server_default="",
        ),
    )


def downgrade() -> None:
    op.drop_column("agents", "llm_connector")
