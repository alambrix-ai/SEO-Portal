"""Technical SEO findings, as rows that can be worked through.

A findings *table* rather than a report blob, because the three things a
customer needs from an audit all require identity across runs:

* working through it — a finding needs a state, and an "ignored" decision has
  to survive the next run or the same argument happens weekly;
* proving the work — ``first_seen_at`` and ``resolved_at`` are what let the
  console say "31 issues fixed this month", which is the number that shows a
  retainer earning its fee;
* not double-counting — the unique constraint is the identity of a finding:
  the same problem, on the same page, about the same target is one issue seen
  twice, not two issues.

Revision ID: 0006_seo_issues
Revises: 0005_notifications_seen
Create Date: 2026-09-08
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006_seo_issues"
down_revision: str | None = "0005_notifications_seen"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "seo_issues" in insp.get_table_names():
        return

    op.create_table(
        "seo_issues",
        sa.Column("id", postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("page_id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("url", sa.String(length=512), nullable=False),
        sa.Column("kind", sa.String(length=48), nullable=False),
        sa.Column("severity", sa.String(length=8), nullable=False),
        sa.Column("target", sa.String(length=512), nullable=False, server_default=""),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("recommendation", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "evidence", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        sa.Column("status", sa.String(length=12), nullable=False, server_default="open"),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ignore_note", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["page_id"], ["seo_pages.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("tenant_id", "page_id", "kind", "target", name="uq_issue_identity"),
    )
    op.create_index("ix_seo_issues_tenant_id", "seo_issues", ["tenant_id"])
    op.create_index("ix_seo_issues_page_id", "seo_issues", ["page_id"])
    op.create_index("ix_seo_issues_kind", "seo_issues", ["kind"])
    op.create_index("ix_issue_tenant_status", "seo_issues", ["tenant_id", "status"])
    op.create_index("ix_issue_tenant_severity", "seo_issues", ["tenant_id", "severity"])

    # Tenant-scoped like every business table, so a query that forgets its
    # WHERE returns nothing rather than another customer's audit.
    from app.db.rls import apply_policies

    apply_policies(op.get_bind(), tables=("seo_issues",))


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON seo_issues")
    op.drop_table("seo_issues")
