"""Merge the four parallel Phase 1–3b migration heads.

Each of these revised ``0023_audit_truncate_guard`` independently:

* ``0024_ai_visibility`` — AI visibility tracker (Phase 1)
* ``0024_content_refresh`` — content refresh candidates (Phase 2a)
* ``0024_seo_issue_fix_proposals`` — technical fix proposals (Phase 2b)
* ``0024_internal_link_suggestions`` — internal linking (Phase 3b)

This revision is a no-op schema change: it only unifies Alembic heads so
later migrations (topic clusters, Willy grounding, …) can use a single
``down_revision``.

See ``docs/roadmap/MIGRATION-ORDER.md`` for the recommended GitHub merge
order of the feature branches.

Revision ID: 0025_merge_phase1_to_3b
Revises: 0024_ai_visibility, 0024_content_refresh,
         0024_seo_issue_fix_proposals, 0024_internal_link_suggestions
Create Date: 2026-10-23
"""
from __future__ import annotations

from collections.abc import Sequence

revision: str = "0025_merge_phase1_to_3b"
down_revision: tuple[str, ...] = (
    "0024_ai_visibility",
    "0024_content_refresh",
    "0024_seo_issue_fix_proposals",
    "0024_internal_link_suggestions",
)
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """No schema changes — heads only."""


def downgrade() -> None:
    """No schema changes — heads only."""
