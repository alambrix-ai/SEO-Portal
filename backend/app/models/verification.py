"""Live verification results, and small platform-wide settings.

Both tables are platform-level: no ``tenant_id``, outside row-level security,
read and written only by portal-admin routes, the CLI and the verification
service. ``connector_verifications.workspace_id`` records which workspace's
connection a run used; it is a plain reference, not a tenant scope.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, Index, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import PK, Base, TimestampMixin, new_id, utcnow


class ConnectorVerification(Base):
    """One live verification run of one connector type."""

    __tablename__ = "connector_verifications"
    __table_args__ = (
        Index("ix_connector_verifications_slug_finished", "slug", "finished_at"),
    )

    id: Mapped[str] = mapped_column(PK, primary_key=True, default=new_id)
    slug: Mapped[str] = mapped_column(String(64), nullable=False)
    #: "live" | "test_account" | "sandbox"
    environment: Mapped[str] = mapped_column(String(32), nullable=False, default="live")
    ok: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    #: The StepResults, as dicts (key, label, kind, ok, skipped, detail,
    #: evidence, seconds). Scrubbed of credential values before storing.
    steps: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, default=list)
    note: Mapped[str] = mapped_column(Text, nullable=False, default="")
    run_by: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    #: The workspace whose connection was used. Nullable: kept when the
    #: workspace is later deleted.
    workspace_id: Mapped[str | None] = mapped_column(PK, nullable=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    finished_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )


class PlatformSetting(Base, TimestampMixin):
    """One platform-wide setting set in Portal Admin (``hide_unverified_connectors``).

    A key and a JSON value. Missing row means the default.
    """

    __tablename__ = "platform_settings"
    __table_args__ = (UniqueConstraint("key", name="uq_platform_settings_key"),)

    id: Mapped[str] = mapped_column(PK, primary_key=True, default=new_id)
    key: Mapped[str] = mapped_column(String(64), nullable=False)
    value: Mapped[Any] = mapped_column(JSONB, nullable=True)
    updated_by: Mapped[str] = mapped_column(String(255), nullable=False, default="")
