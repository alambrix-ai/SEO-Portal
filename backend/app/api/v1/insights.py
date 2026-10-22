"""Insights: the enterprise overview and LLM usage.

* ``GET /insights/overview?from=&to=``  every section the caller may see, for
  an inclusive date range, compared with the period of equal length before it
* ``GET /insights/llm-usage?from=&to=&group=&cursor=&limit=``  one page of
  tokens and estimated cost, grouped by agent, model, provider, day, month
  or Creative Studio job

Both need the Reports module. Each overview section also needs its own
module (``app.services.insights``); a section the role cannot see is left
out. LLM usage needs the agents or admin module and is never shown to a
Client Viewer. Ranges are capped at ``insights_max_range_days`` (422 beyond).
"""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Query

from app.api.deps import CurrentUserDep, DbSession
from app.core.exceptions import ForbiddenError
from app.core.rbac import Module
from app.db.base import utcnow
from app.schemas.insights import InsightsOverviewOut, LlmUsageOut
from app.services import insights, llm_usage

router = APIRouter(prefix="/insights", tags=["insights"])

_NO_USAGE_ACCESS = "Your role can't see AI usage and cost. Ask an administrator if you need it."


def _range(start: date | None, end: date | None) -> tuple[date, date]:
    default_start, default_end = insights.default_range(utcnow().date())
    end = end or default_end
    if start is None:
        start = end - (default_end - default_start)
    return start, end


@router.get("/overview", response_model=InsightsOverviewOut)
def overview(
    current: CurrentUserDep,
    db: DbSession,
    start: date | None = Query(default=None, alias="from"),
    end: date | None = Query(default=None, alias="to"),
) -> InsightsOverviewOut:
    current.require_view(Module.REPORTS)
    start, end = _range(start, end)
    return insights.overview(db, current, start=start, end=end)


@router.get("/llm-usage", response_model=LlmUsageOut)
def llm_usage_report(
    current: CurrentUserDep,
    db: DbSession,
    start: date | None = Query(default=None, alias="from"),
    end: date | None = Query(default=None, alias="to"),
    group: llm_usage.UsageGroup = Query(default=llm_usage.UsageGroup.AGENT),
    cursor: str | None = Query(
        default=None, max_length=llm_usage.CURSOR_MAX_LENGTH, pattern=r"^[A-Za-z0-9_\-=]+$"
    ),
    limit: int = Query(default=llm_usage.DEFAULT_PAGE_SIZE, ge=1, le=llm_usage.MAX_PAGE_SIZE),
) -> LlmUsageOut:
    current.require_view(Module.REPORTS)
    if not insights.can_see_llm_cost(current):
        raise ForbiddenError(_NO_USAGE_ACCESS)
    start, end = _range(start, end)
    return insights.usage(
        db, current, start=start, end=end, group=group.value, cursor=cursor, limit=limit
    )
