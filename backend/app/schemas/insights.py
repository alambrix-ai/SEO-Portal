"""Response shapes for Insights (``/insights/*``) and the extended Reports reply.

They match ``frontend/src/api/insights.ts`` field for field. A number is
either measured or absent: every figure that can be missing is ``None`` with
a ``note`` saying why ("Not tracked yet", "Not reported by Instagram"),
never 0. A zero on these screens means "measured, and none". Each section
also carries ``available`` and a plain ``connect_hint`` naming what to
connect (or do) to fill it.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from pydantic import Field, SerializerFunctionWrapHandler, model_serializer

from app.schemas.common import ApiModel
from app.schemas.workspace import ReportsOut as LegacyReportsOut

#: How a value is written (``InsightUnit`` in the frontend).
Unit = Literal["count", "currency", "percent", "position", "tokens", "score"]


# ── Reports (GET /reports) ─────────────────────────────────────────────────
class ReportsOut(LegacyReportsOut):
    """``/reports`` as before, plus what tells "no data yet" from a zero.

    Every legacy field is kept, with the same type, except three that can be
    ``None``, each with a note saying why: ``backlinks_won`` when the
    workspace tracks no backlinks, ``ad_spend`` when no ad account was
    measured in the range, and ``blended_cac`` then too (or when nothing
    converted).
    """

    #: False when no day in the range has measured website visits.
    available: bool
    #: What to connect to fill the screen; "" when there is data.
    connect_hint: str
    #: ``None`` when nothing in the workspace tracks backlinks.
    backlinks_won: int | None  # type: ignore[assignment]
    #: "Not tracked yet" when ``backlinks_won`` is ``None``, else "".
    backlinks_won_note: str = ""
    #: ``None`` when no ad account was measured, or nothing converted.
    blended_cac: float | None  # type: ignore[assignment]
    #: "Not tracked yet" or "No conversions yet" when ``blended_cac`` is ``None``.
    blended_cac_note: str = ""
    #: Formatted spend; ``None`` when no ad account was measured in the range.
    ad_spend: str | None  # type: ignore[assignment]
    #: "Not tracked yet" when ``ad_spend`` is ``None``, else "".
    ad_spend_note: str = ""
    #: ``trend``'s buckets averaged over measured days only; ``None`` = a gap.
    trend_with_gaps: list[int | None]
    #: Calendar days the range covers (the trend has six buckets whatever it is).
    trend_days: int


# ── Shared pieces ──────────────────────────────────────────────────────────
class MetricOut(ApiModel):
    """One figure for the chosen period, with the period before it."""

    key: str
    label: str
    #: ``None`` = not measured (see ``note``); 0 is a measured zero.
    value: float | None
    previous: float | None
    #: ``None`` when either value is missing or ``previous`` is 0.
    change_pct: float | None
    unit: Unit = "count"
    #: Which direction is good news.
    better: Literal["up", "down"] = "up"
    is_estimate: bool = False
    note: str = ""


class PointOut(ApiModel):
    #: ``YYYY-MM-DD``.
    date: date
    #: ``None`` for a day with no data: drawn as a gap, not as 0.
    value: float | None


class SeriesOut(ApiModel):
    key: str
    label: str
    unit: Unit = "count"
    points: list[PointOut]


class SectionOut(ApiModel):
    available: bool
    #: Plain English: what to connect or do to see this section. "" when full.
    connect_hint: str = ""
    metrics: list[MetricOut]
    series: list[SeriesOut] = Field(default_factory=list)


class BreakdownItemOut(ApiModel):
    key: str
    label: str
    value: float | None
    unit: Unit = "count"
    note: str = ""


# ── Sections ───────────────────────────────────────────────────────────────
class SeoTopQueryOut(ApiModel):
    query: str
    clicks: float | None
    impressions: float | None
    position: float | None


class SeoSectionOut(SectionOut):
    #: Empty until a source stores per-query figures; never made up.
    top_queries: list[SeoTopQueryOut] = Field(default_factory=list)


class AdsChannelOut(ApiModel):
    channel: str
    label: str
    spend: float | None
    clicks: float | None
    conversions: float | None
    cac: float | None
    note: str = ""


class AdsSectionOut(SectionOut):
    channels: list[AdsChannelOut] = Field(default_factory=list)


class SocialPlatformOut(ApiModel):
    platform: str
    label: str
    #: Each ``None`` when the platform did not report it.
    impressions: float | None
    reach: float | None
    engagement: float | None
    clicks: float | None
    note: str = ""


class SocialTopPostOut(ApiModel):
    id: str
    platform: str
    title: str
    #: The published post's https link, or ``None``.
    url: str | None
    posted_at: datetime | None
    impressions: int | None
    engagement: int | None


class SocialSectionOut(SectionOut):
    platforms: list[SocialPlatformOut] = Field(default_factory=list)
    top_posts: list[SocialTopPostOut] = Field(default_factory=list)


class ContentSectionOut(SectionOut):
    by_kind: list[BreakdownItemOut] = Field(default_factory=list)


class LlmSectionOut(SectionOut):
    currency_symbol: str
    note: str = ""


class SectionsOut(ApiModel):
    """The sections the caller may see. One the role cannot see is left out
    of the JSON entirely (not sent as ``null``)."""

    seo: SeoSectionOut | None = None
    ads: AdsSectionOut | None = None
    social: SocialSectionOut | None = None
    content: ContentSectionOut | None = None
    agents: SectionOut | None = None
    llm: LlmSectionOut | None = None

    @model_serializer(mode="wrap")
    def _drop_hidden(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        data = handler(self)
        return {key: value for key, value in data.items() if value is not None}


class InsightsOverviewOut(ApiModel):
    from_: date = Field(alias="from")
    to: date
    #: The period of equal length just before, used for every comparison.
    previous_from: date
    previous_to: date
    days: int
    sections: SectionsOut


# ── LLM usage (app.services.llm_usage dataclasses) ─────────────────────────
class UsageLineOut(ApiModel):
    key: str
    label: str
    runs: int
    tokens_in: int | None
    tokens_out: int | None
    tokens_in_text: str
    tokens_out_text: str
    unknown_token_runs: int
    cost: float | None
    cost_status: str
    cost_label: str
    connector: str = ""
    model: str = ""
    is_estimate: bool = True


class JobUsageLineOut(ApiModel):
    job_id: str
    created_at: datetime
    status: str
    text_connector: str
    text_model: str
    model_label: str
    image_model: str
    tokens_in: int | None
    tokens_out: int | None
    tokens_in_text: str
    tokens_out_text: str
    images_generated: int
    cost: float | None
    cost_status: str
    cost_label: str
    is_estimate: bool = True


class UsageTotalsOut(ApiModel):
    runs: int
    tokens_in: int | None
    tokens_out: int | None
    tokens_in_text: str
    tokens_out_text: str
    unknown_token_runs: int
    cost: float | None
    cost_status: str
    cost_label: str
    images_generated: int | None = None
    is_estimate: bool = True


class LlmUsageOut(ApiModel):
    """One page of ``llm_usage.summary``, as-is. Costs are always estimates.

    ``lines`` are job lines when ``group`` is ``job``.
    """

    group: str
    start: date
    end: date
    lines: list[UsageLineOut] | list[JobUsageLineOut]
    totals: UsageTotalsOut
    next_cursor: str | None
    currency_symbol: str
    note: str = ""
