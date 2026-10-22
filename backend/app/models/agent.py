"""Agent registration, per-organisation configuration, and run history."""
from __future__ import annotations

import re
from datetime import datetime, timezone
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
    # Which connected AI model writes for this agent. Empty until Configure
    # is saved — agents that need a model refuse to run without it.
    llm_connector: Mapped[str] = mapped_column(String(64), default="", nullable=False)
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


#: How many timeline entries a run keeps. A pass over hundreds of pages
#: reports a step per page; the console needs the shape, not every line.
TIMELINE_LIMIT = 40

_COUNT_SUFFIX = re.compile(r"\s*\(\d+ of \d+\)\s*$")


def record_timeline_step(
    detail: dict | None, step: str, *, done: int = 0, total: int = 0
) -> dict:
    """Return ``detail`` with ``step`` recorded on its ``timeline``.

    A step that only moves its own counter ("Applying (3 of 9)" after
    "Applying (2 of 9)") updates the current entry instead of adding one.
    Each entry keeps when it started and when it was last updated, so the
    console can tell a long step from a pass that has stopped reporting.
    """
    now = datetime.now(timezone.utc).isoformat()
    out = dict(detail or {})
    timeline = [dict(entry) for entry in (out.get("timeline") or [])]
    label = _COUNT_SUFFIX.sub("", step).strip()[:160] or step[:160]
    if timeline and timeline[-1].get("label") == label:
        timeline[-1].update(text=step[:160], done=done, total=total, updated_at=now)
    else:
        if timeline:
            timeline[-1]["ended_at"] = now
        timeline.append(
            {
                "label": label,
                "text": step[:160],
                "done": done,
                "total": total,
                "at": now,
                "updated_at": now,
            }
        )
    out["timeline"] = timeline[-TIMELINE_LIMIT:]
    return out


def close_timeline(detail: dict | None) -> dict:
    """Mark the last timeline entry as ended, when the pass finishes."""
    out = dict(detail or {})
    timeline = [dict(entry) for entry in (out.get("timeline") or [])]
    if timeline and not timeline[-1].get("ended_at"):
        timeline[-1]["ended_at"] = datetime.now(timezone.utc).isoformat()
    if timeline:
        out["timeline"] = timeline
    return out


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
    # Which connected AI model actually wrote for this run: the connector slug
    # and the model id it used. Empty when the run made no model call. Kept
    # on the run (not read from the agent) because the agent's choice can
    # change afterwards, and usage reports must price what was really used.
    llm_connector: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    llm_model: Mapped[str] = mapped_column(String(128), default="", nullable=False)

    # Structural detail only (counts, ids, timings). Anything that could carry
    # customer content goes through the organisation's own key instead.
    detail: Mapped[dict] = mapped_column(JSONB, default=dict)
