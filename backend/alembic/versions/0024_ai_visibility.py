"""AI visibility tracker tables.

Revision ID: 0024_ai_visibility
Revises: 0023_audit_truncate_guard
Create Date: 2026-10-23
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0024_ai_visibility"
down_revision: str | None = "0023_audit_truncate_guard"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=False)
JSONB = postgresql.JSONB()

TABLES: tuple[str, ...] = (
    "visibility_queries",
    "visibility_settings",
    "visibility_observations",
)


def upgrade() -> None:
    insp = sa.inspect(op.get_bind())
    existing = set(insp.get_table_names())
    created: list[str] = []

    if "visibility_queries" not in existing:
        op.create_table(
            "visibility_queries",
            sa.Column("id", UUID, primary_key=True),
            sa.Column("tenant_id", UUID, nullable=False),
            sa.Column("question_encrypted", sa.Text(), nullable=False),
            sa.Column("label", sa.String(length=120), nullable=False, server_default=""),
            sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
            sa.Column(
                "tags", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")
            ),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.ForeignKeyConstraint(["tenant_id"], ["organizations.id"], ondelete="CASCADE"),
        )
        op.create_index(
            "ix_visibility_queries_tenant_id", "visibility_queries", ["tenant_id"]
        )
        op.create_index(
            "ix_visibility_queries_tenant_active",
            "visibility_queries",
            ["tenant_id", "active"],
        )
        created.append("visibility_queries")

    if "visibility_settings" not in existing:
        op.create_table(
            "visibility_settings",
            sa.Column("id", UUID, primary_key=True),
            sa.Column("tenant_id", UUID, nullable=False),
            sa.Column(
                "competitor_domains",
                JSONB,
                nullable=False,
                server_default=sa.text("'[]'::jsonb"),
            ),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.ForeignKeyConstraint(["tenant_id"], ["organizations.id"], ondelete="CASCADE"),
            sa.UniqueConstraint("tenant_id", name="uq_visibility_settings_tenant"),
        )
        op.create_index(
            "ix_visibility_settings_tenant_id", "visibility_settings", ["tenant_id"]
        )
        created.append("visibility_settings")

    if "visibility_observations" not in existing:
        op.create_table(
            "visibility_observations",
            sa.Column("id", UUID, primary_key=True),
            sa.Column("tenant_id", UUID, nullable=False),
            sa.Column("query_id", UUID, nullable=True),
            sa.Column("question_encrypted", sa.Text(), nullable=False),
            sa.Column("engine", sa.String(length=32), nullable=False),
            sa.Column(
                "brand_cited", sa.Boolean(), nullable=False, server_default=sa.text("false")
            ),
            sa.Column("brand_position", sa.Integer(), nullable=True),
            sa.Column(
                "cited_domains",
                JSONB,
                nullable=False,
                server_default=sa.text("'[]'::jsonb"),
            ),
            sa.Column(
                "competitors_cited",
                JSONB,
                nullable=False,
                server_default=sa.text("'[]'::jsonb"),
            ),
            sa.Column(
                "answer_excerpt_encrypted",
                sa.Text(),
                nullable=False,
                server_default="",
            ),
            sa.Column("run_id", sa.String(length=36), nullable=False, server_default=""),
            sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["tenant_id"], ["organizations.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(
                ["query_id"], ["visibility_queries.id"], ondelete="SET NULL"
            ),
        )
        op.create_index(
            "ix_visibility_observations_tenant_id",
            "visibility_observations",
            ["tenant_id"],
        )
        op.create_index(
            "ix_visibility_obs_tenant_observed",
            "visibility_observations",
            ["tenant_id", "observed_at"],
        )
        op.create_index(
            "ix_visibility_obs_tenant_engine",
            "visibility_observations",
            ["tenant_id", "engine", "observed_at"],
        )
        op.create_index(
            "ix_visibility_obs_query",
            "visibility_observations",
            ["tenant_id", "query_id"],
        )
        created.append("visibility_observations")

    if created:
        from app.db.rls import apply_policies

        apply_policies(op.get_bind(), tables=tuple(created))


def downgrade() -> None:
    for table in reversed(TABLES):
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
        op.drop_table(table)
