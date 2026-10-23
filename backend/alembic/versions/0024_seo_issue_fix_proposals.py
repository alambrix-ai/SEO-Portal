"""Link technical audit findings to approval-gated fix proposals.

The auditor stays read-only. A separate ``technical_fix_applier`` agent
(and the Propose-fix API) create rows here that point at ``approval_items``.

Revision ID: 0024_seo_issue_fix_proposals
Revises: 0023_audit_truncate_guard
Create Date: 2026-10-23
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0024_seo_issue_fix_proposals"
down_revision: str | None = "0023_audit_truncate_guard"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=False)
TABLE = "seo_issue_fix_proposals"


def upgrade() -> None:
    existing = set(sa.inspect(op.get_bind()).get_table_names())
    if TABLE in existing:
        return

    op.create_table(
        TABLE,
        sa.Column("id", UUID, primary_key=True),
        sa.Column("tenant_id", UUID, nullable=False),
        sa.Column("issue_id", UUID, nullable=False),
        sa.Column("page_id", UUID, nullable=False),
        sa.Column("approval_item_id", UUID, nullable=True),
        sa.Column("fix_kind", sa.String(48), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="proposed"),
        sa.Column("proposed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["issue_id"], ["seo_issues.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["page_id"], ["seo_pages.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("tenant_id", "issue_id", name="uq_fix_proposal_issue"),
    )
    op.create_index(f"ix_{TABLE}_tenant_id", TABLE, ["tenant_id"])
    op.create_index(f"ix_{TABLE}_issue_id", TABLE, ["issue_id"])
    op.create_index(f"ix_{TABLE}_page_id", TABLE, ["page_id"])
    op.create_index("ix_fix_proposal_tenant_status", TABLE, ["tenant_id", "status"])
    op.create_index("ix_fix_proposal_approval", TABLE, ["tenant_id", "approval_item_id"])

    from app.db.rls import apply_policies

    apply_policies(op.get_bind(), tables=(TABLE,))


def downgrade() -> None:
    existing = set(sa.inspect(op.get_bind()).get_table_names())
    if TABLE not in existing:
        return
    op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {TABLE}")
    op.drop_table(TABLE)
