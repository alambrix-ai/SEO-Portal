"""Warehouse export job rows."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import FK, PK, Base, TenantScopedMixin, TimestampMixin, new_id


class WarehouseExportJob(Base, TimestampMixin, TenantScopedMixin):
    __tablename__ = "warehouse_export_jobs"
    __table_args__ = (
        Index("ix_warehouse_export_tenant_status", "tenant_id", "status"),
    )

    id: Mapped[str] = mapped_column(PK, primary_key=True, default=new_id)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    dataset: Mapped[str] = mapped_column(String(80), default="metrics", nullable=False)
    destination: Mapped[str] = mapped_column(String(512), default="", nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="queued", nullable=False)
    row_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error_code: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    #: Optional destination credential (OrgCipher); never logged.
    secret_encrypted: Mapped[str] = mapped_column(Text, default="", nullable=False)
    created_by_id: Mapped[str | None] = mapped_column(FK, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
