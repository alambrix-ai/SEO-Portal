"""Website analyses run from the public marketing site.

Not tenant data: a visitor has no workspace yet. Nothing identifying is kept
in the clear — the device id, network address and browser are stored only as
keyed hashes, enough to enforce "one analysis per device" and nothing more.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Index, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import PK, Base, TenantScopedMixin, TimestampMixin, new_id


class DemoStatus:
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    #: Could not finish (site unreachable, model error). Does not use up the
    #: device's analysis, so the visitor can try again.
    FAILED = "failed"


class DemoAnalysis(Base, TimestampMixin):
    __tablename__ = "demo_analyses"
    __table_args__ = (
        Index("ix_demo_device", "device_hash"),
        Index("ix_demo_fingerprint", "fingerprint_hash"),
        Index("ix_demo_ip_created", "ip_hash", "created_at"),
    )

    id: Mapped[str] = mapped_column(PK, primary_key=True, default=new_id)
    #: Keyed hash of the id the website keeps in the visitor's browser.
    device_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    #: Keyed hash of network address + browser, for when that id is reset.
    fingerprint_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    ip_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    #: Keyed hash of the secret the visitor reads the report with.
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    website_url: Mapped[str] = mapped_column(String(500), nullable=False)
    business_url: Mapped[str] = mapped_column(String(1000), default="", nullable=False)
    status: Mapped[str] = mapped_column(String(16), default=DemoStatus.QUEUED, nullable=False)
    #: [{"key", "label", "state": pending|running|done|skipped|failed, "detail"}]
    progress: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list, nullable=False)
    report: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    error: Mapped[str] = mapped_column(Text, default="", nullable=False)
    model: Mapped[str] = mapped_column(String(80), default="", nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class SiteAnalysis(Base, TimestampMixin, TenantScopedMixin):
    """The same analysis, run by a workspace from inside the portal.

    Kept per workspace so the history shows how the site changes over time.
    Written with the workspace's own connected AI model (never the platform
    key), and ``context`` records what was connected when it ran, so a later
    reader can tell why two reports differ.
    """

    __tablename__ = "site_analyses"
    __table_args__ = (Index("ix_site_analyses_tenant_created", "tenant_id", "created_at"),)

    id: Mapped[str] = mapped_column(PK, primary_key=True, default=new_id)
    requested_by: Mapped[str] = mapped_column(String(36), default="", nullable=False)
    requested_by_name: Mapped[str] = mapped_column(String(160), default="", nullable=False)
    website_url: Mapped[str] = mapped_column(String(500), nullable=False)
    business_url: Mapped[str] = mapped_column(String(1000), default="", nullable=False)
    status: Mapped[str] = mapped_column(String(16), default=DemoStatus.QUEUED, nullable=False)
    progress: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list, nullable=False)
    report: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    #: {"connectors": [...], "agents": [...], "llm": {...}, "pagespeed": "..."}
    context: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    error: Mapped[str] = mapped_column(Text, default="", nullable=False)
    model: Mapped[str] = mapped_column(String(80), default="", nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
