"""Topic clusters, membership rows, and encrypted content briefs.

Revision ID: 0026_topic_clusters
Revises: 0025_merge_phase1_to_3b
Create Date: 2026-10-23
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0026_topic_clusters"
down_revision: str | None = "0025_merge_phase1_to_3b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=False)
JSONB = postgresql.JSONB()
TABLES: tuple[str, ...] = ("topic_clusters", "topic_cluster_pages", "content_briefs")


def upgrade() -> None:
    existing = set(sa.inspect(op.get_bind()).get_table_names())
    created: list[str] = []

    if "topic_clusters" not in existing:
        op.create_table(
            "topic_clusters",
            sa.Column("id", UUID, primary_key=True),
            sa.Column("tenant_id", UUID, nullable=False),
            sa.Column("name", sa.String(160), nullable=False),
            sa.Column("seed_terms", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
            sa.Column("gap_score", sa.Float(), nullable=False, server_default="0"),
            sa.Column("page_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("status", sa.String(16), nullable=False, server_default="open"),
            sa.Column("detected_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column(
                "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
            ),
            sa.Column(
                "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
            ),
            sa.ForeignKeyConstraint(["tenant_id"], ["organizations.id"], ondelete="CASCADE"),
        )
        op.create_index("ix_topic_clusters_tenant_id", "topic_clusters", ["tenant_id"])
        op.create_index("ix_topic_cluster_tenant_status", "topic_clusters", ["tenant_id", "status"])
        created.append("topic_clusters")

    if "topic_cluster_pages" not in existing:
        op.create_table(
            "topic_cluster_pages",
            sa.Column("id", UUID, primary_key=True),
            sa.Column("tenant_id", UUID, nullable=False),
            sa.Column("cluster_id", UUID, nullable=False),
            sa.Column("page_id", UUID, nullable=False),
            sa.Column("relevance", sa.Float(), nullable=False, server_default="0"),
            sa.ForeignKeyConstraint(["tenant_id"], ["organizations.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["cluster_id"], ["topic_clusters.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["page_id"], ["seo_pages.id"], ondelete="CASCADE"),
            sa.UniqueConstraint("tenant_id", "cluster_id", "page_id", name="uq_topic_cluster_page"),
        )
        op.create_index("ix_topic_cluster_pages_tenant_id", "topic_cluster_pages", ["tenant_id"])
        op.create_index("ix_topic_cluster_pages_cluster_id", "topic_cluster_pages", ["cluster_id"])
        op.create_index("ix_topic_cluster_pages_page_id", "topic_cluster_pages", ["page_id"])
        op.create_index(
            "ix_topic_cluster_pages_cluster", "topic_cluster_pages", ["tenant_id", "cluster_id"]
        )
        created.append("topic_cluster_pages")

    if "content_briefs" not in existing:
        op.create_table(
            "content_briefs",
            sa.Column("id", UUID, primary_key=True),
            sa.Column("tenant_id", UUID, nullable=False),
            sa.Column("cluster_id", UUID, nullable=True),
            sa.Column("title", sa.String(255), nullable=False),
            sa.Column(
                "target_keywords", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")
            ),
            sa.Column("body_encrypted", sa.Text(), nullable=False, server_default=""),
            sa.Column("status", sa.String(16), nullable=False, server_default="draft"),
            sa.Column("approval_item_id", UUID, nullable=True),
            sa.Column(
                "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
            ),
            sa.Column(
                "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
            ),
            sa.ForeignKeyConstraint(["tenant_id"], ["organizations.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["cluster_id"], ["topic_clusters.id"], ondelete="SET NULL"),
        )
        op.create_index("ix_content_briefs_tenant_id", "content_briefs", ["tenant_id"])
        op.create_index("ix_content_brief_tenant_status", "content_briefs", ["tenant_id", "status"])
        op.create_index("ix_content_brief_cluster", "content_briefs", ["tenant_id", "cluster_id"])
        created.append("content_briefs")

    if created:
        from app.db.rls import apply_policies

        apply_policies(op.get_bind(), tables=tuple(created))


def downgrade() -> None:
    existing = set(sa.inspect(op.get_bind()).get_table_names())
    for table in reversed(TABLES):
        if table not in existing:
            continue
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
        op.drop_table(table)
