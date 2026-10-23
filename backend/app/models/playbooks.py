"""Playbook installation records (catalogue itself is code-defined)."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import FK, JSONDoc, PK, Base, TenantScopedMixin, TimestampMixin, new_id


class PlaybookInstallation(Base, TimestampMixin, TenantScopedMixin):
    __tablename__ = "playbook_installations"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "playbook_slug", name="uq_playbook_installation"
        ),
        Index("ix_playbook_install_tenant_status", "tenant_id", "status"),
    )

    id: Mapped[str] = mapped_column(PK, primary_key=True, default=new_id)
    playbook_slug: Mapped[str] = mapped_column(String(80), nullable=False)
    #: Encrypted operator overrides (schedules, scopes) — no secrets on argv.
    settings_encrypted: Mapped[str] = mapped_column(Text, default="")
    agent_slugs: Mapped[list] = mapped_column(JSONDoc, default=list)
    #: installed | disabled
    status: Mapped[str] = mapped_column(String(16), default="installed", nullable=False)
    installed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    approval_item_id: Mapped[str | None] = mapped_column(FK, nullable=True)
