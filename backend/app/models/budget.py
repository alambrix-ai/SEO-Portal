"""Cross-channel budget reallocation proposals (Google / Meta / TikTok / LinkedIn)."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import FK, JSONDoc, PK, Base, TenantScopedMixin, TimestampMixin, new_id


class CrossChannelBudgetProposal(Base, TimestampMixin, TenantScopedMixin):
    __tablename__ = "cross_channel_budget_proposals"
    __table_args__ = (
        Index("ix_cc_budget_tenant_status", "tenant_id", "status"),
    )

    id: Mapped[str] = mapped_column(PK, primary_key=True, default=new_id)
    #: Encrypted channel snapshot (spend, revenue, roas, current %).
    snapshot_encrypted: Mapped[str] = mapped_column(Text, default="")
    current_shares: Mapped[dict] = mapped_column(JSONDoc, default=dict)
    proposed_shares: Mapped[dict] = mapped_column(JSONDoc, default=dict)
    shifts: Mapped[dict] = mapped_column(JSONDoc, default=dict)
    rationale: Mapped[str] = mapped_column(Text, default="")
    blended_roas_before: Mapped[str] = mapped_column(String(32), default="")
    blended_roas_after: Mapped[str] = mapped_column(String(32), default="")
    #: Points moved this proposal (for daily caps).
    moved_points: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    #: proposed | applied | rejected
    status: Mapped[str] = mapped_column(String(16), default="proposed", nullable=False)
    approval_item_id: Mapped[str | None] = mapped_column(FK, nullable=True)
    proposed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
