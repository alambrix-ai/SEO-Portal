"""Multi-location entities and location_id on local rank checks.

Revision ID: 0037_locations
Revises: 0036_competitor_ads
Create Date: 2026-10-23
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0037_locations"
down_revision: str | None = "0036_competitor_ads"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=False)


def upgrade() -> None:
    existing = set(sa.inspect(op.get_bind()).get_table_names())
    created: list[str] = []

    if "locations" not in existing:
        op.create_table(
            "locations",
            sa.Column("id", UUID, primary_key=True),
            sa.Column("tenant_id", UUID, nullable=False),
            sa.Column("slug", sa.String(80), nullable=False),
            sa.Column("name", sa.String(200), nullable=False),
            sa.Column("brand", sa.String(80), nullable=False, server_default=""),
            sa.Column("region", sa.String(80), nullable=False, server_default=""),
            sa.Column("address_line", sa.String(300), nullable=False, server_default=""),
            sa.Column("city", sa.String(120), nullable=False, server_default=""),
            sa.Column("region_code", sa.String(32), nullable=False, server_default=""),
            sa.Column("postal_code", sa.String(24), nullable=False, server_default=""),
            sa.Column("country_code", sa.String(2), nullable=False, server_default=""),
            sa.Column("place_id", sa.String(255), nullable=False, server_default=""),
            sa.Column("gbp_location_id", sa.String(255), nullable=False, server_default=""),
            sa.Column("lat", sa.Float(), nullable=True),
            sa.Column("lng", sa.Float(), nullable=True),
            sa.Column("notes_encrypted", sa.Text(), nullable=False, server_default=""),
            sa.Column("status", sa.String(16), nullable=False, server_default="active"),
            sa.Column(
                "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
            ),
            sa.Column(
                "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
            ),
            sa.ForeignKeyConstraint(["tenant_id"], ["organizations.id"], ondelete="CASCADE"),
            sa.UniqueConstraint("tenant_id", "slug", name="uq_location_slug"),
        )
        op.create_index("ix_locations_tenant_id", "locations", ["tenant_id"])
        op.create_index("ix_location_tenant_status", "locations", ["tenant_id", "status"])
        op.create_index("ix_location_tenant_region", "locations", ["tenant_id", "region"])
        created.append("locations")

    cols = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("local_rank_checks")}
    if "location_id" not in cols:
        op.add_column("local_rank_checks", sa.Column("location_id", UUID, nullable=True))
        op.create_foreign_key(
            "fk_local_rank_location",
            "local_rank_checks",
            "locations",
            ["location_id"],
            ["id"],
            ondelete="SET NULL",
        )
        op.create_index(
            "ix_local_rank_checks_location_id", "local_rank_checks", ["location_id"]
        )
        op.create_index(
            "ix_local_rank_tenant_location",
            "local_rank_checks",
            ["tenant_id", "location_id"],
        )

    if created:
        from app.db.rls import apply_policies

        apply_policies(op.get_bind(), tables=tuple(created))


def downgrade() -> None:
    bind = op.get_bind()
    existing = set(sa.inspect(bind).get_table_names())
    cols = (
        {c["name"] for c in sa.inspect(bind).get_columns("local_rank_checks")}
        if "local_rank_checks" in existing
        else set()
    )
    if "location_id" in cols:
        op.drop_index("ix_local_rank_tenant_location", table_name="local_rank_checks")
        op.drop_index("ix_local_rank_checks_location_id", table_name="local_rank_checks")
        op.drop_constraint("fk_local_rank_location", "local_rank_checks", type_="foreignkey")
        op.drop_column("local_rank_checks", "location_id")
    if "locations" in existing:
        op.execute("DROP POLICY IF EXISTS tenant_isolation ON locations")
        op.drop_table("locations")
