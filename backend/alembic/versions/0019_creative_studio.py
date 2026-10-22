"""Creative Studio: stored files, brand kits, products and creative jobs.

All four tables are tenant-scoped and go under row-level security as they are
created. Colours, fonts and file types are pinned by CHECK constraints, and a
partial unique index allows at most one default brand kit per workspace.

The allow-lists below are frozen copies, not imports: a migration must keep
producing the same schema however the application code changes later.

Revision ID: 0019_creative_studio
Revises: 0018_connector_verifications
Create Date: 2026-10-21
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# Kept under 32 characters, the width of alembic_version.version_num.
revision: str = "0019_creative_studio"
down_revision: str | None = "0018_connector_verifications"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=False)
JSONB = postgresql.JSONB()

FONT_IDS: tuple[str, ...] = (
    "inter",
    "lato",
    "poppins",
    "montserrat",
    "lora",
    "playfair_display",
    "oswald",
    "bebas_neue",
)
STORED_FILE_MIMES: tuple[str, ...] = (
    "image/png",
    "image/jpeg",
    "image/webp",
    "application/pdf",
)
HEX_COLOUR_SQL = "^#[0-9a-f]{6}$"
KIT_COLOURS: tuple[str, ...] = (
    "primary_color",
    "secondary_color",
    "accent_color",
    "text_color",
    "background_color",
)
KIT_FONTS: tuple[str, ...] = ("heading_font", "body_font")

#: Creation order; dropped in reverse.
TABLES: tuple[str, ...] = ("stored_files", "brand_kits", "products", "creative_jobs")


def _sql_in(column: str, values: tuple[str, ...]) -> str:
    quoted = ", ".join(f"'{v}'" for v in values)
    return f"{column} IN ({quoted})"


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    ]


def upgrade() -> None:
    existing = set(sa.inspect(op.get_bind()).get_table_names())
    created: list[str] = []

    if "stored_files" not in existing:
        op.create_table(
            "stored_files",
            sa.Column("id", UUID, primary_key=True),
            sa.Column("tenant_id", UUID, nullable=False),
            sa.Column("kind", sa.String(24), nullable=False),
            sa.Column("source", sa.String(16), nullable=False),
            sa.Column("mime", sa.String(40), nullable=False),
            sa.Column("size_bytes", sa.Integer(), nullable=False),
            sa.Column("width", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("height", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("sha256", sa.String(64), nullable=False),
            sa.Column("display_name", sa.String(200), nullable=False, server_default=""),
            sa.Column("created_by", sa.String(36), nullable=False, server_default=""),
            sa.Column("job_id", sa.String(36), nullable=False, server_default=""),
            sa.Column("storage_backend", sa.String(16), nullable=False, server_default="postgres"),
            sa.Column("storage_key", sa.String(255), nullable=False, server_default=""),
            sa.Column("data", sa.LargeBinary(), nullable=True),
            *_timestamps(),
            sa.ForeignKeyConstraint(["tenant_id"], ["organizations.id"], ondelete="CASCADE"),
            sa.CheckConstraint(_sql_in("mime", STORED_FILE_MIMES), name="ck_stored_files_mime"),
            sa.CheckConstraint("size_bytes >= 0", name="ck_stored_files_size"),
            sa.CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name="ck_stored_files_sha256"),
        )
        op.create_index("ix_stored_files_tenant_id", "stored_files", ["tenant_id"])
        op.create_index("ix_stored_files_tenant_kind", "stored_files", ["tenant_id", "kind"])
        op.create_index("ix_stored_files_tenant_sha256", "stored_files", ["tenant_id", "sha256"])
        created.append("stored_files")

    if "brand_kits" not in existing:
        op.create_table(
            "brand_kits",
            sa.Column("id", UUID, primary_key=True),
            sa.Column("tenant_id", UUID, nullable=False),
            sa.Column("name", sa.String(120), nullable=False),
            sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("logo_file_id", UUID, nullable=True),
            sa.Column("logo_dark_file_id", UUID, nullable=True),
            sa.Column("primary_color", sa.String(7), nullable=False, server_default="#1f2937"),
            sa.Column("secondary_color", sa.String(7), nullable=False, server_default="#4b5563"),
            sa.Column("accent_color", sa.String(7), nullable=False, server_default="#2563eb"),
            sa.Column("text_color", sa.String(7), nullable=False, server_default="#111827"),
            sa.Column("background_color", sa.String(7), nullable=False, server_default="#ffffff"),
            sa.Column("heading_font", sa.String(32), nullable=False, server_default="inter"),
            sa.Column("body_font", sa.String(32), nullable=False, server_default="inter"),
            sa.Column("voice", sa.String(500), nullable=False, server_default=""),
            sa.Column("banned_phrases", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
            sa.Column("disclaimer", sa.String(500), nullable=False, server_default=""),
            *_timestamps(),
            sa.ForeignKeyConstraint(["tenant_id"], ["organizations.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["logo_file_id"], ["stored_files.id"], ondelete="SET NULL"),
            sa.ForeignKeyConstraint(["logo_dark_file_id"], ["stored_files.id"], ondelete="SET NULL"),
            *(
                sa.CheckConstraint(f"{col} ~ '{HEX_COLOUR_SQL}'", name=f"ck_brand_kits_{col}")
                for col in KIT_COLOURS
            ),
            *(
                sa.CheckConstraint(_sql_in(col, FONT_IDS), name=f"ck_brand_kits_{col}")
                for col in KIT_FONTS
            ),
        )
        op.create_index("ix_brand_kits_tenant_id", "brand_kits", ["tenant_id"])
        op.create_index(
            "uq_brand_kits_one_default",
            "brand_kits",
            ["tenant_id"],
            unique=True,
            postgresql_where=sa.text("is_default"),
        )
        created.append("brand_kits")

    if "products" not in existing:
        op.create_table(
            "products",
            sa.Column("id", UUID, primary_key=True),
            sa.Column("tenant_id", UUID, nullable=False),
            sa.Column("name", sa.String(200), nullable=False),
            sa.Column("description", sa.Text(), nullable=False, server_default=""),
            sa.Column("url", sa.String(1000), nullable=False, server_default=""),
            sa.Column("price_text", sa.String(64), nullable=False, server_default=""),
            sa.Column("approved_claims", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
            sa.Column("image_file_ids", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
            sa.Column("disclaimer", sa.String(500), nullable=False, server_default=""),
            sa.Column("is_archived", sa.Boolean(), nullable=False, server_default=sa.false()),
            *_timestamps(),
            sa.ForeignKeyConstraint(["tenant_id"], ["organizations.id"], ondelete="CASCADE"),
        )
        op.create_index("ix_products_tenant_id", "products", ["tenant_id"])
        op.create_index("ix_products_tenant_name", "products", ["tenant_id", "name"])
        created.append("products")

    if "creative_jobs" not in existing:
        op.create_table(
            "creative_jobs",
            sa.Column("id", UUID, primary_key=True),
            sa.Column("tenant_id", UUID, nullable=False),
            sa.Column("created_by", sa.String(36), nullable=False, server_default=""),
            sa.Column("status", sa.String(16), nullable=False, server_default="queued"),
            sa.Column("brand_kit_id", UUID, nullable=True),
            sa.Column("product_id", UUID, nullable=True),
            sa.Column("formats", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
            sa.Column("text_connector", sa.String(64), nullable=False, server_default=""),
            sa.Column("text_model", sa.String(120), nullable=False, server_default=""),
            sa.Column("image_model", sa.String(120), nullable=False, server_default=""),
            sa.Column("brief_encrypted", sa.Text(), nullable=False, server_default=""),
            sa.Column("feedback_encrypted", sa.Text(), nullable=False, server_default=""),
            sa.Column("revision_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("outputs", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
            sa.Column("tokens_in", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("tokens_out", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("images_generated", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("error_code", sa.String(64), nullable=False, server_default=""),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
            *_timestamps(),
            sa.ForeignKeyConstraint(["tenant_id"], ["organizations.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["brand_kit_id"], ["brand_kits.id"], ondelete="SET NULL"),
            sa.ForeignKeyConstraint(["product_id"], ["products.id"], ondelete="SET NULL"),
            sa.CheckConstraint("revision_count >= 0", name="ck_creative_jobs_revisions"),
        )
        op.create_index("ix_creative_jobs_tenant_id", "creative_jobs", ["tenant_id"])
        op.create_index("ix_creative_jobs_tenant_status", "creative_jobs", ["tenant_id", "status"])
        op.create_index("ix_creative_jobs_tenant_created", "creative_jobs", ["tenant_id", "created_at"])
        created.append("creative_jobs")

    if created:
        from app.db.rls import apply_policies

        apply_policies(op.get_bind(), tables=tuple(created))


def downgrade() -> None:
    existing = set(sa.inspect(op.get_bind()).get_table_names())
    # Reverse creation order, so no table is dropped while another still
    # references it. Indexes and constraints go with their table.
    for table in reversed(TABLES):
        if table not in existing:
            continue
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
        op.drop_table(table)
