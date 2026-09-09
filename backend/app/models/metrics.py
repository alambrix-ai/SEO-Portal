"""Daily metric rollups behind the dashboard KPIs and the Reports screen.

One row per organisation per day. Every range the console offers (7d / 30d /
90d) is an aggregate over these rows, so the KPI tiles and the trend line
always agree with each other and with what the agents actually did.
"""
from __future__ import annotations

from datetime import date

from sqlalchemy import Date, Float, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import PK, Base, TenantScopedMixin, TimestampMixin, new_id


class DailyMetric(Base, TimestampMixin, TenantScopedMixin):
    __tablename__ = "daily_metrics"
    __table_args__ = (
        UniqueConstraint("tenant_id", "day", name="uq_metric_tenant_day"),
        Index("ix_metric_tenant_day_desc", "tenant_id", "day"),
    )

    id: Mapped[str] = mapped_column(PK, primary_key=True, default=new_id)
    day: Mapped[date] = mapped_column(Date, nullable=False, index=True)

    # ── Organic / AEO ──────────────────────────────────────────────────────
    organic_sessions: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # Times an answer engine cited one of this organisation's injected blocks.
    aeo_citations: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    pages_optimised: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    backlinks_won: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    pitches_sent: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # ── Paid ───────────────────────────────────────────────────────────────
    ad_spend: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    conversions: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    fraud_blocked: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    spend_saved: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    referral_spam_blocked: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # ── Agent activity ─────────────────────────────────────────────────────
    agent_runs: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    actions_autonomous: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    actions_approved: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    actions_rejected: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    @property
    def blended_cac(self) -> float:
        return round(self.ad_spend / self.conversions, 2) if self.conversions else 0.0
