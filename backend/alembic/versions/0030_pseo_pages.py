"""Programmatic SEO templates, datasets, and page jobs.

Revision ID: 0030_pseo_pages
Revises: 0029_alert_rules
Create Date: 2026-10-23
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0030_pseo_pages"
down_revision: str | None = "0029_alert_rules"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=False)
JSONB = postgresql.JSONB()
TABLES = ("pseo_templates", "pseo_datasets", "pseo_page_jobs")


def upgrade() -> None:
    existing = set(sa.inspect(op.get_bind()).get_table_names())
    created: list[str] = []

    if "pseo_templates" not in existing:
        op.create_table(
            "pseo_templates",
            sa.Column("id", UUID, primary_key=True),
            sa.Column("tenant_id", UUID, nullable=False),
            sa.Column("slug", sa.String(80), nullable=False),
            sa.Column("name", sa.String(160), nullable=False),
            sa.Column("body_encrypted", sa.Text(), nullable=False, server_default=""),
            sa.Column("path_pattern", sa.String(255), nullable=False, server_default="/{slug}"),
            sa.Column("status", sa.String(16), nullable=False, server_default="active"),
            sa.Column(
                "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
            ),
            sa.Column(
                "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
            ),
            sa.ForeignKeyConstraint(["tenant_id"], ["organizations.id"], ondelete="CASCADE"),
            sa.UniqueConstraint("tenant_id", "slug", name="uq_pseo_template_slug"),
        )
        op.create_index("ix_pseo_templates_tenant_id", "pseo_templates", ["tenant_id"])
        op.create_index(
            "ix_pseo_template_tenant_status", "pseo_templates", ["tenant_id", "status"]
        )
        created.append("pseo_templates")

    if "pseo_datasets" not in existing:
        op.create_table(
            "pseo_datasets",
            sa.Column("id", UUID, primary_key=True),
            sa.Column("tenant_id", UUID, nullable=False),
            sa.Column("name", sa.String(160), nullable=False),
            sa.Column("rows_encrypted", sa.Text(), nullable=False, server_default=""),
            sa.Column("row_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("status", sa.String(16), nullable=False, server_default="ready"),
            sa.Column(
                "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
            ),
            sa.Column(
                "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
            ),
            sa.ForeignKeyConstraint(["tenant_id"], ["organizations.id"], ondelete="CASCADE"),
        )
        op.create_index("ix_pseo_datasets_tenant_id", "pseo_datasets", ["tenant_id"])
        op.create_index(
            "ix_pseo_dataset_tenant_status", "pseo_datasets", ["tenant_id", "status"]
        )
        created.append("pseo_datasets")

    if "pseo_page_jobs" not in existing:
        op.create_table(
            "pseo_page_jobs",
            sa.Column("id", UUID, primary_key=True),
            sa.Column("tenant_id", UUID, nullable=False),
            sa.Column("template_id", UUID, nullable=False),
            sa.Column("dataset_id", UUID, nullable=True),
            sa.Column("title", sa.String(255), nullable=False),
            sa.Column("path", sa.String(512), nullable=False),
            sa.Column("body_encrypted", sa.Text(), nullable=False, server_default=""),
            sa.Column("content_hash", sa.String(64), nullable=False, server_default=""),
            sa.Column("unique_tokens", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("status", sa.String(16), nullable=False, server_default="proposed"),
            sa.Column("skip_reason", sa.String(255), nullable=False, server_default=""),
            sa.Column("approval_item_id", UUID, nullable=True),
            sa.Column("proposed_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("row_data", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
            sa.Column(
                "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
            ),
            sa.Column(
                "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
            ),
            sa.ForeignKeyConstraint(["tenant_id"], ["organizations.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["template_id"], ["pseo_templates.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["dataset_id"], ["pseo_datasets.id"], ondelete="SET NULL"),
            sa.UniqueConstraint("tenant_id", "path", name="uq_pseo_page_path"),
        )
        op.create_index("ix_pseo_page_jobs_tenant_id", "pseo_page_jobs", ["tenant_id"])
        op.create_index("ix_pseo_page_jobs_template_id", "pseo_page_jobs", ["template_id"])
        op.create_index("ix_pseo_page_jobs_dataset_id", "pseo_page_jobs", ["dataset_id"])
        op.create_index("ix_pseo_job_tenant_status", "pseo_page_jobs", ["tenant_id", "status"])
        created.append("pseo_page_jobs")

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
