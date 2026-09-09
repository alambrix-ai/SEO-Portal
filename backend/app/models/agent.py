"""Agent registration, per-organisation configuration, and run history."""
from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import PK, Base, TenantScopedMixin, TimestampMixin, new_id


class AgentStatus(StrEnum):
    RUNNING = "running"
    PAUSED = "paused"
    ERROR = "error"


class RunStatus(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"


class AgentRecord(Base, TimestampMixin, TenantScopedMixin):
    """One organisation's instance of one registered agent.

    ``slug`` matches the package name under ``app/agents/`` — that is the join
    between this row and the executable agent class.
    """

    __tablename__ = "agents"
    __table_args__ = (
        UniqueConstraint("tenant_id", "slug", name="uq_agent_tenant_slug"),
        Index("ix_agents_due", "status", "next_run_at"),
    )

    id: Mapped[str] = mapped_column(PK, primary_key=True, default=new_id)
    slug: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    category: Mapped[str] = mapped_column(String(32), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")

    status: Mapped[str] = mapped_column(
        # Paused by default: an agent that starts running because a caller
        # forgot to say otherwise is the wrong way round for a fleet that
        # publishes to live websites and spends money.
        String(16), default=AgentStatus.PAUSED.value, nullable=False
    )
    autonomy: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # ── Configure dialog ───────────────────────────────────────────────────
    schedule: Mapped[str] = mapped_column(String(32), default="Daily", nullable=False)
    scope: Mapped[str] = mapped_column(String(255), default="")
    # None by default: a workspace has nothing connected on its first day,
    # and a default channel that cannot deliver is worse than no channel.
    notify_channel: Mapped[str] = mapped_column(String(32), default="None")
    max_actions_per_day: Mapped[int] = mapped_column(Integer, default=20, nullable=False)
    configured: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # ── Live telemetry shown on the agent cards ────────────────────────────
    metric_label: Mapped[str] = mapped_column(String(120), default="")
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    next_run_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    actions_today: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # ISO date the counter belongs to, so the daily cap resets without a cron.
    actions_today_date: Mapped[str] = mapped_column(String(10), default="")
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_error: Mapped[str] = mapped_column(Text, default="")

    # Agent-specific knobs that do not deserve their own column.
    settings: Mapped[dict] = mapped_column(JSONB, default=dict)

    @property
    def is_running(self) -> bool:
        return self.status == AgentStatus.RUNNING.value


class AgentRun(Base, TenantScopedMixin):
    """One execution of one agent, and the actions it produced."""

    __tablename__ = "agent_runs"
    __table_args__ = (Index("ix_runs_tenant_agent_started", "tenant_id", "agent_slug", "started_at"),)

    id: Mapped[str] = mapped_column(PK, primary_key=True, default=new_id)
    agent_slug: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    # schedule | manual | webhook
    trigger: Mapped[str] = mapped_column(String(16), default="schedule", nullable=False)
    triggered_by: Mapped[str] = mapped_column(String(160), default="")
    status: Mapped[str] = mapped_column(
        String(16), default=RunStatus.RUNNING.value, nullable=False
    )

    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    actions_taken: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    actions_queued: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    summary: Mapped[str] = mapped_column(Text, default="")
    error: Mapped[str] = mapped_column(Text, default="")

    # Where the pass has got to, updated as it goes. Written on a separate
    # session from the run's own transaction, so it is visible to the console
    # while the pass is still open — see AgentContext.progress.
    step: Mapped[str] = mapped_column(String(120), default="")
    step_done: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # 0 when the step has no countable total ("reading the sitemap").
    step_total: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # Token accounting when the run went through a real LLM provider.
    tokens_in: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    tokens_out: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cost: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)

    # Structural detail only (counts, ids, timings). Anything that could carry
    # customer content goes through the organisation's own key instead.
    detail: Mapped[dict] = mapped_column(JSONB, default=dict)
