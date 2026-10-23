"""Programmatic SEO templates, datasets, and page generation jobs."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import FK, JSONDoc, PK, Base, TenantScopedMixin, TimestampMixin, new_id


class PseoTemplate(Base, TimestampMixin, TenantScopedMixin):
    """A sandboxed Jinja body template for programmatic pages."""

    __tablename__ = "pseo_templates"
    __table_args__ = (
        UniqueConstraint("tenant_id", "slug", name="uq_pseo_template_slug"),
        Index("ix_pseo_template_tenant_status", "tenant_id", "status"),
    )

    id: Mapped[str] = mapped_column(PK, primary_key=True, default=new_id)
    slug: Mapped[str] = mapped_column(String(80), nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    #: Encrypted Jinja source — never logged.
    body_encrypted: Mapped[str] = mapped_column(Text, default="")
    #: Path pattern with ``{slug}`` / field placeholders, e.g. ``/locations/{city}``.
    path_pattern: Mapped[str] = mapped_column(String(255), nullable=False, default="/{slug}")
    #: active | archived
    status: Mapped[str] = mapped_column(String(16), default="active", nullable=False)


class PseoDataset(Base, TimestampMixin, TenantScopedMixin):
    """Rows that feed a template (CSV/JSON upload stored encrypted)."""

    __tablename__ = "pseo_datasets"
    __table_args__ = (Index("ix_pseo_dataset_tenant_status", "tenant_id", "status"),)

    id: Mapped[str] = mapped_column(PK, primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    #: Encrypted JSON array of row objects.
    rows_encrypted: Mapped[str] = mapped_column(Text, default="")
    row_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    #: ready | archived
    status: Mapped[str] = mapped_column(String(16), default="ready", nullable=False)


class PseoPageJob(Base, TimestampMixin, TenantScopedMixin):
    """One proposed programmatic page awaiting approval or published."""

    __tablename__ = "pseo_page_jobs"
    __table_args__ = (
        UniqueConstraint("tenant_id", "path", name="uq_pseo_page_path"),
        Index("ix_pseo_job_tenant_status", "tenant_id", "status"),
    )

    id: Mapped[str] = mapped_column(PK, primary_key=True, default=new_id)
    template_id: Mapped[str] = mapped_column(
        FK, ForeignKey("pseo_templates.id", ondelete="CASCADE"), nullable=False, index=True
    )
    dataset_id: Mapped[str | None] = mapped_column(
        FK, ForeignKey("pseo_datasets.id", ondelete="SET NULL"), nullable=True, index=True
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    path: Mapped[str] = mapped_column(String(512), nullable=False)
    #: Encrypted rendered HTML/Markdown body.
    body_encrypted: Mapped[str] = mapped_column(Text, default="")
    content_hash: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    unique_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    #: proposed | rejected | published | skipped
    status: Mapped[str] = mapped_column(String(16), default="proposed", nullable=False)
    skip_reason: Mapped[str] = mapped_column(String(255), default="")
    approval_item_id: Mapped[str | None] = mapped_column(FK, nullable=True)
    proposed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    row_data: Mapped[dict] = mapped_column(JSONDoc, default=dict)
