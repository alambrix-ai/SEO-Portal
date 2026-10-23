"""Ask Willy grounding traces for AI governance."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import JSONDoc, PK, Base, TenantScopedMixin, TimestampMixin, new_id


class WillyGroundingTrace(Base, TimestampMixin, TenantScopedMixin):
    """One assistant turn's grounding footprint (no free-form customer prose)."""

    __tablename__ = "willy_grounding_traces"
    __table_args__ = (Index("ix_willy_trace_tenant_at", "tenant_id", "observed_at"),)

    id: Mapped[str] = mapped_column(PK, primary_key=True, default=new_id)
    prompt_hash: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    tool_calls: Mapped[list] = mapped_column(JSONDoc, default=list)
    cost_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
