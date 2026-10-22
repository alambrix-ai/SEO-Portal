"""LLM tokens and estimated cost, grouped for the Insights screens.

Tokens are summed in SQL; cost is applied afterwards, per connector and
model, from :mod:`app.llm.pricing` — at read time, so a corrected price
corrects every report. Pricing is linear, so summing a model's tokens first
and pricing the sum gives the same figure as pricing each run.

Sources:

* ``agent``, ``model``, ``provider``, ``day`` and ``month`` read
  ``agent_runs``. Creative Studio's own runs are agent runs too, so they are
  counted here once.
* ``job`` reads ``creative_jobs``: one line per job, with its text tokens and
  the number of AI images it made. Image generation has no configured price,
  so it is counted, not costed (:data:`IMAGE_COST_NOTE`).

What the figures never do:

* show an unpriced model as free — its cost is ``None``, "No price set";
* show unknown tokens as zero — a run whose model reported no usage is
  counted in ``unknown_token_runs`` and its tokens read "unknown";
* present a cost as anything but an estimate (``is_estimate`` is always set).

Every query filters on the caller's tenant explicitly (RLS is the second
line), is bounded by ``insights_max_range_days``, and ranges over the
``(tenant_id, …, started_at)`` / ``(tenant_id, created_at)`` indexes. A page
costs a fixed number of queries whatever its size.
"""
from __future__ import annotations

import base64
import binascii
import json
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from enum import StrEnum
from typing import Any

from sqlalchemy import ColumnElement, DateTime, and_, func, literal_column, or_, select, tuple_
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.exceptions import InvalidInputError
from app.llm import pricing
from app.models.agent import AgentRun
from app.models.creative import CreativeJob


class UsageGroup(StrEnum):
    AGENT = "agent"
    #: One line per connector and model.
    MODEL = "model"
    #: One line per connector (the AI company or self-hosted server).
    PROVIDER = "provider"
    DAY = "day"
    MONTH = "month"
    #: One line per Creative Studio job.
    JOB = "job"


class CostStatus(StrEnum):
    #: Every token in the line is priced.
    ESTIMATE = "estimate"
    #: Some tokens are priced; others have no price or no known count.
    PARTIAL = "partial"
    #: Nothing in the line has a price. ``cost`` is ``None``, never 0.
    NO_PRICE = "no_price"
    #: Priced, but no token count was reported.
    UNKNOWN = "unknown"


COST_LABELS: dict[CostStatus, str] = {
    CostStatus.ESTIMATE: "Estimate",
    CostStatus.PARTIAL: "Partial estimate",
    CostStatus.NO_PRICE: "No price set",
    CostStatus.UNKNOWN: "Unknown",
}
#: Runs from before the connector and model were recorded on each run.
MODEL_NOT_RECORDED = "Model not recorded"
#: How a token count the model did not report is shown.
TOKENS_UNKNOWN = "unknown"
#: A run whose model returned no usage block may say so in ``detail`` under
#: this key (``True``); its zero token columns are then not read as zero.
TOKENS_UNKNOWN_DETAIL_KEY = "tokens_unknown"
IMAGE_COST_NOTE = "AI image costs are not included: no image price is set."

DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 100
#: Longest cursor accepted; a real one is well under half this.
CURSOR_MAX_LENGTH = 400

_BAD_CURSOR = "This page link is no longer valid. Please reload the usage report."
_SLUG_MAX = 64
_MODEL_MAX = 128


# ── Results ────────────────────────────────────────────────────────────────
@dataclass(frozen=True, slots=True)
class UsageLine:
    """One group's tokens and estimated cost."""

    #: Stable id of the group: agent slug, ``connector`` or
    #: ``connector:model``, ``YYYY-MM-DD`` or ``YYYY-MM``.
    key: str
    #: Plain-English name for the group.
    label: str
    runs: int
    #: ``None`` when no run in the group reported a token count.
    tokens_in: int | None
    tokens_out: int | None
    #: The two counts as shown: a number, or "unknown".
    tokens_in_text: str
    tokens_out_text: str
    #: Runs whose model reported no usage; their tokens are not in the sums.
    unknown_token_runs: int
    #: Currency units, or ``None`` when no price is set or no count is known.
    cost: float | None
    cost_status: CostStatus
    cost_label: str
    #: Set on ``model`` and ``provider`` lines; ``""`` otherwise.
    connector: str = ""
    model: str = ""
    is_estimate: bool = True


@dataclass(frozen=True, slots=True)
class JobUsageLine:
    """One Creative Studio job's tokens, AI images and estimated text cost."""

    job_id: str
    created_at: datetime
    status: str
    text_connector: str
    text_model: str
    #: ``text_connector · text_model``, or "Model not recorded".
    model_label: str
    image_model: str
    tokens_in: int | None
    tokens_out: int | None
    tokens_in_text: str
    tokens_out_text: str
    images_generated: int
    cost: float | None
    cost_status: CostStatus
    cost_label: str
    is_estimate: bool = True


@dataclass(frozen=True, slots=True)
class UsageTotals:
    """The whole range, across every group (not only this page)."""

    runs: int
    tokens_in: int | None
    tokens_out: int | None
    tokens_in_text: str
    tokens_out_text: str
    unknown_token_runs: int
    cost: float | None
    cost_status: CostStatus
    cost_label: str
    #: Set for the ``job`` group only.
    images_generated: int | None = None
    is_estimate: bool = True


@dataclass(frozen=True, slots=True)
class UsageSummary:
    group: UsageGroup
    start: date
    end: date
    lines: list[UsageLine] | list[JobUsageLine]
    totals: UsageTotals
    next_cursor: str | None
    #: Prices are in currency units; this is the symbol to show with them.
    currency_symbol: str
    #: Shown with the ``job`` group, where AI images are counted, not costed.
    note: str = ""


# ── Pure helpers ───────────────────────────────────────────────────────────
@dataclass(frozen=True, slots=True)
class ModelBucket:
    """Token sums for one connector and model within one group.

    ``tokens_in`` / ``tokens_out`` sum the runs whose counts are known, and
    are ``None`` when there are none.
    """

    connector: str
    model: str
    runs: int
    tokens_in: int | None
    tokens_out: int | None
    unknown_runs: int


@dataclass(frozen=True, slots=True)
class RollUp:
    runs: int
    tokens_in: int | None
    tokens_out: int | None
    unknown_runs: int
    cost: float | None
    status: CostStatus


def tokens_text(value: int | None) -> str:
    """A token count as shown: the number, or "unknown" — never a made-up 0."""
    return TOKENS_UNKNOWN if value is None else f"{value:,}"


def roll_up(buckets: Iterable[ModelBucket]) -> RollUp:
    """Sum one group's per-model buckets and price them.

    The rules, in order: nothing priced → "No price set"; priced but no
    count known → "Unknown"; anything with tokens left unpriced or uncounted
    → "Partial estimate" over the priced part; otherwise "Estimate".
    """
    runs = unknown = 0
    tokens_in: int | None = None
    tokens_out: int | None = None
    cost = 0.0
    any_priced = gap = False
    for bucket in buckets:
        runs += bucket.runs
        unknown += bucket.unknown_runs
        if bucket.unknown_runs:
            gap = True
        known = bucket.tokens_in is not None and bucket.tokens_out is not None
        if known:
            tokens_in = (tokens_in or 0) + int(bucket.tokens_in or 0)
            tokens_out = (tokens_out or 0) + int(bucket.tokens_out or 0)
        line_cost, priced = pricing.estimate(
            bucket.connector,
            bucket.model,
            bucket.tokens_in if known else None,
            bucket.tokens_out if known else None,
        )
        if priced:
            any_priced = True
            if line_cost is not None:
                cost += line_cost
        elif known and (int(bucket.tokens_in or 0) + int(bucket.tokens_out or 0)) > 0:
            gap = True

    if not any_priced:
        return RollUp(runs, tokens_in, tokens_out, unknown, None, CostStatus.NO_PRICE)
    if tokens_in is None:
        return RollUp(runs, None, None, unknown, None, CostStatus.UNKNOWN)
    status = CostStatus.PARTIAL if gap else CostStatus.ESTIMATE
    return RollUp(runs, tokens_in, tokens_out, unknown, round(cost, 6), status)


def usage_window(start: date, end: date) -> tuple[datetime, datetime]:
    """The UTC window ``[start 00:00, end + 1 day 00:00)``, after checking bounds.

    Both dates are inclusive, matching the date pickers, and the range is
    capped by ``insights_max_range_days``.
    """
    if end < start:
        raise InvalidInputError("The end date must be on or after the start date.")
    days = (end - start).days + 1
    if days > settings.insights_max_range_days:
        raise InvalidInputError(
            f"Choose a range of {settings.insights_max_range_days} days or fewer."
        )
    return (
        datetime.combine(start, time.min, tzinfo=UTC),
        datetime.combine(end + timedelta(days=1), time.min, tzinfo=UTC),
    )


def _page_size(limit: int | None) -> int:
    if limit is None:
        return DEFAULT_PAGE_SIZE
    if not 1 <= limit <= MAX_PAGE_SIZE:
        raise InvalidInputError(f"Choose a page size from 1 to {MAX_PAGE_SIZE}.")
    return limit


def encode_cursor(parts: Sequence[str]) -> str:
    raw = json.dumps(list(parts), separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_cursor(cursor: str, group: UsageGroup) -> tuple[Any, ...]:
    """Parse a cursor from :func:`encode_cursor`, refusing anything else.

    A cursor is only a position in the caller's own tenant-filtered,
    date-bounded query, so a forged one can do no more than move the page.
    It is still parsed strictly into the types the query compares against.
    """
    if not cursor or len(cursor) > CURSOR_MAX_LENGTH:
        raise InvalidInputError(_BAD_CURSOR)
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        parts = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
        if not isinstance(parts, list) or not all(isinstance(p, str) for p in parts):
            raise ValueError("cursor shape")
        if group in (UsageGroup.AGENT, UsageGroup.PROVIDER):
            (value,) = parts
            if len(value) > _SLUG_MAX:
                raise ValueError("cursor length")
            return (value,)
        if group is UsageGroup.MODEL:
            connector, model = parts
            if len(connector) > _SLUG_MAX or len(model) > _MODEL_MAX:
                raise ValueError("cursor length")
            return (connector, model)
        if group in (UsageGroup.DAY, UsageGroup.MONTH):
            (value,) = parts
            return (datetime.combine(date.fromisoformat(value), time.min),)
        at_text, id_text = parts
        at = datetime.fromisoformat(at_text)
        if at.tzinfo is None:
            raise ValueError("cursor timezone")
        return (at, str(uuid.UUID(id_text)))
    except (ValueError, TypeError, UnicodeError, binascii.Error) as exc:
        raise InvalidInputError(_BAD_CURSOR) from exc


def _agent_label(slug: str) -> str:
    from app.agents.base.registry import get_agent

    agent = get_agent(slug)
    return agent.spec.name if agent is not None else slug


def _model_label(connector: str, model: str) -> str:
    if not model:
        return MODEL_NOT_RECORDED
    return f"{connector} · {model}" if connector else model


def _totals(rolled: RollUp, *, images: int | None = None) -> UsageTotals:
    return UsageTotals(
        runs=rolled.runs,
        tokens_in=rolled.tokens_in,
        tokens_out=rolled.tokens_out,
        tokens_in_text=tokens_text(rolled.tokens_in),
        tokens_out_text=tokens_text(rolled.tokens_out),
        unknown_token_runs=rolled.unknown_runs,
        cost=rolled.cost,
        cost_status=rolled.status,
        cost_label=COST_LABELS[rolled.status],
        images_generated=images,
    )


# ── Agent runs ─────────────────────────────────────────────────────────────
def _bucket_expr(unit: str) -> ColumnElement[datetime]:
    # Literal SQL, not bound parameters: Postgres matches a GROUP BY
    # expression textually, and two binds of 'day' are not the same text.
    return func.date_trunc(
        literal_column(f"'{unit}'"),
        func.timezone(literal_column("'UTC'"), AgentRun.started_at),
        type_=DateTime(),
    )


def _run_keys(group: UsageGroup) -> tuple[ColumnElement[Any], ...]:
    if group is UsageGroup.AGENT:
        return (AgentRun.agent_slug,)
    if group is UsageGroup.PROVIDER:
        return (AgentRun.llm_connector,)
    if group is UsageGroup.MODEL:
        return (AgentRun.llm_connector, AgentRun.llm_model)
    if group is UsageGroup.DAY:
        return (_bucket_expr("day"),)
    return (_bucket_expr("month"),)


def _run_known() -> ColumnElement[bool]:
    """True for runs whose token counts were reported."""
    return and_(
        AgentRun.tokens_in.is_not(None),
        AgentRun.tokens_out.is_not(None),
        func.coalesce(AgentRun.detail[TOKENS_UNKNOWN_DETAIL_KEY].astext, "") != "true",
    )


def _run_measures() -> tuple[ColumnElement[Any], ...]:
    known = _run_known()
    return (
        func.count(AgentRun.id),
        func.count(AgentRun.id).filter(known),
        func.sum(AgentRun.tokens_in).filter(known),
        func.sum(AgentRun.tokens_out).filter(known),
    )


def _bucket(
    connector: str | None,
    model: str | None,
    runs: int | None,
    known: int | None,
    t_in: int | None,
    t_out: int | None,
) -> ModelBucket:
    """One SQL row of per-model sums. ``known`` counts the runs whose token
    counts were reported; with none, the sums are unknown, not zero."""
    runs_n = int(runs or 0)
    known_n = int(known or 0)
    return ModelBucket(
        connector=connector or "",
        model=model or "",
        runs=runs_n,
        tokens_in=int(t_in or 0) if known_n else None,
        tokens_out=int(t_out or 0) if known_n else None,
        unknown_runs=runs_n - known_n,
    )


def _key_text(group: UsageGroup, key: tuple[Any, ...]) -> str:
    if group is UsageGroup.DAY:
        return key[0].date().isoformat()
    if group is UsageGroup.MONTH:
        return key[0].strftime("%Y-%m")
    if group is UsageGroup.MODEL:
        return f"{key[0] or ''}:{key[1] or ''}"
    return str(key[0] or "")


def _cursor_parts(group: UsageGroup, key: tuple[Any, ...]) -> list[str]:
    if group in (UsageGroup.DAY, UsageGroup.MONTH):
        return [key[0].date().isoformat()]
    return [str(k or "") for k in key]


def _line(group: UsageGroup, key: tuple[Any, ...], rolled: RollUp) -> UsageLine:
    connector = model = ""
    if group is UsageGroup.AGENT:
        label = _agent_label(str(key[0]))
    elif group is UsageGroup.PROVIDER:
        connector = str(key[0] or "")
        label = connector or MODEL_NOT_RECORDED
    elif group is UsageGroup.MODEL:
        connector, model = str(key[0] or ""), str(key[1] or "")
        label = _model_label(connector, model)
    else:
        label = _key_text(group, key)
    return UsageLine(
        key=_key_text(group, key),
        label=label,
        runs=rolled.runs,
        tokens_in=rolled.tokens_in,
        tokens_out=rolled.tokens_out,
        tokens_in_text=tokens_text(rolled.tokens_in),
        tokens_out_text=tokens_text(rolled.tokens_out),
        unknown_token_runs=rolled.unknown_runs,
        cost=rolled.cost,
        cost_status=rolled.status,
        cost_label=COST_LABELS[rolled.status],
        connector=connector,
        model=model,
    )


def _run_summary(
    db: Session,
    *,
    tenant_id: str,
    group: UsageGroup,
    since: datetime,
    until: datetime,
    after: tuple[Any, ...] | None,
    page_size: int,
) -> tuple[list[UsageLine], UsageTotals, str | None]:
    in_range = (
        AgentRun.tenant_id == tenant_id,
        AgentRun.started_at >= since,
        AgentRun.started_at < until,
    )
    keys = _run_keys(group)

    # 1. This page's group keys.
    page_stmt = select(*keys).where(*in_range).group_by(*keys).order_by(*keys).limit(page_size + 1)
    if after is not None:
        page_stmt = page_stmt.where(tuple_(*keys) > tuple_(*after))
    page = [tuple(row) for row in db.execute(page_stmt).all()]
    more = len(page) > page_size
    page = page[:page_size]

    lines: list[UsageLine] = []
    if page:
        # 2. Per-model sums for exactly those keys, in one query.
        conn, model = AgentRun.llm_connector, AgentRun.llm_model
        member = keys[0].in_([k[0] for k in page]) if len(keys) == 1 else tuple_(*keys).in_(page)
        detail_stmt = (
            select(*keys, conn, model, *_run_measures())
            .where(*in_range, member)
            .group_by(*keys, conn, model)
        )
        grouped: dict[tuple[Any, ...], list[ModelBucket]] = {k: [] for k in page}
        width = len(keys)
        for row in db.execute(detail_stmt).all():
            key = tuple(row[:width])
            grouped.setdefault(key, []).append(_bucket(*row[width:width + 6]))
        lines = [_line(group, key, roll_up(grouped[key])) for key in page]

    # 3. The whole range, per model, for the totals.
    totals_stmt = (
        select(AgentRun.llm_connector, AgentRun.llm_model, *_run_measures())
        .where(*in_range)
        .group_by(AgentRun.llm_connector, AgentRun.llm_model)
    )
    totals = _totals(roll_up(_bucket(*row) for row in db.execute(totals_stmt).all()))

    next_cursor = encode_cursor(_cursor_parts(group, page[-1])) if more and page else None
    return lines, totals, next_cursor


# ── Creative jobs ──────────────────────────────────────────────────────────
def _job_known() -> ColumnElement[bool]:
    return and_(CreativeJob.tokens_in.is_not(None), CreativeJob.tokens_out.is_not(None))


def _job_line(job: Any) -> JobUsageLine:  # noqa: ANN401
    known = job.tokens_in is not None and job.tokens_out is not None
    t_in = int(job.tokens_in) if known else None
    t_out = int(job.tokens_out) if known else None
    rolled = roll_up(
        [
            ModelBucket(
                connector=job.text_connector or "",
                model=job.text_model or "",
                runs=1,
                tokens_in=t_in,
                tokens_out=t_out,
                unknown_runs=0 if known else 1,
            )
        ]
    )
    return JobUsageLine(
        job_id=str(job.id),
        created_at=job.created_at,
        status=job.status,
        text_connector=job.text_connector or "",
        text_model=job.text_model or "",
        model_label=_model_label(job.text_connector or "", job.text_model or ""),
        image_model=job.image_model or "",
        tokens_in=t_in,
        tokens_out=t_out,
        tokens_in_text=tokens_text(t_in),
        tokens_out_text=tokens_text(t_out),
        images_generated=int(job.images_generated or 0),
        cost=rolled.cost,
        cost_status=rolled.status,
        cost_label=COST_LABELS[rolled.status],
    )


def _job_summary(
    db: Session,
    *,
    tenant_id: str,
    since: datetime,
    until: datetime,
    after: tuple[Any, ...] | None,
    page_size: int,
) -> tuple[list[JobUsageLine], UsageTotals, str | None]:
    in_range = (
        CreativeJob.tenant_id == tenant_id,
        CreativeJob.created_at >= since,
        CreativeJob.created_at < until,
    )
    # Newest first, keyset on (created_at, id). Only the columns shown:
    # the encrypted brief and outputs are never loaded.
    page_stmt = (
        select(
            CreativeJob.id,
            CreativeJob.created_at,
            CreativeJob.status,
            CreativeJob.text_connector,
            CreativeJob.text_model,
            CreativeJob.image_model,
            CreativeJob.tokens_in,
            CreativeJob.tokens_out,
            CreativeJob.images_generated,
        )
        .where(*in_range)
        .order_by(CreativeJob.created_at.desc(), CreativeJob.id.desc())
        .limit(page_size + 1)
    )
    if after is not None:
        after_at, after_id = after
        page_stmt = page_stmt.where(
            or_(
                CreativeJob.created_at < after_at,
                and_(CreativeJob.created_at == after_at, CreativeJob.id < after_id),
            )
        )
    rows = db.execute(page_stmt).all()
    more = len(rows) > page_size
    rows = rows[:page_size]
    lines = [_job_line(row) for row in rows]

    known = _job_known()
    totals_stmt = (
        select(
            CreativeJob.text_connector,
            CreativeJob.text_model,
            func.count(CreativeJob.id),
            func.count(CreativeJob.id).filter(known),
            func.sum(CreativeJob.tokens_in).filter(known),
            func.sum(CreativeJob.tokens_out).filter(known),
            func.coalesce(func.sum(CreativeJob.images_generated), 0),
        )
        .where(*in_range)
        .group_by(CreativeJob.text_connector, CreativeJob.text_model)
    )
    total_rows = db.execute(totals_stmt).all()
    images = sum(int(row[6] or 0) for row in total_rows)
    totals = _totals(roll_up(_bucket(*row[:6]) for row in total_rows), images=images)

    next_cursor = None
    if more and rows:
        last = rows[-1]
        next_cursor = encode_cursor([last.created_at.astimezone(UTC).isoformat(), str(last.id)])
    return lines, totals, next_cursor


# ── Entry point ────────────────────────────────────────────────────────────
def summary(
    db: Session,
    tenant_id: str,
    start: date,
    end: date,
    group: UsageGroup | str = UsageGroup.AGENT,
    cursor: str | None = None,
    limit: int | None = None,
) -> UsageSummary:
    """One page of LLM usage for one workspace, grouped as asked.

    ``start`` and ``end`` are inclusive UTC dates, at most
    ``insights_max_range_days`` apart. ``totals`` covers the whole range,
    not just the page. The caller authorises; ``tenant_id`` must be the
    caller's own workspace.
    """
    if not tenant_id:
        raise ValueError("tenant_id is required")
    try:
        group = UsageGroup(group)
    except ValueError as exc:
        raise InvalidInputError("Choose how to group the usage report.") from exc
    since, until = usage_window(start, end)
    page_size = _page_size(limit)
    after = decode_cursor(cursor, group) if cursor else None

    if group is UsageGroup.JOB:
        job_lines, totals, next_cursor = _job_summary(
            db, tenant_id=tenant_id, since=since, until=until, after=after, page_size=page_size
        )
        return UsageSummary(
            group=group,
            start=start,
            end=end,
            lines=job_lines,
            totals=totals,
            next_cursor=next_cursor,
            currency_symbol=settings.currency_symbol,
            note=IMAGE_COST_NOTE,
        )

    lines, totals, next_cursor = _run_summary(
        db,
        tenant_id=tenant_id,
        group=group,
        since=since,
        until=until,
        after=after,
        page_size=page_size,
    )
    return UsageSummary(
        group=group,
        start=start,
        end=end,
        lines=lines,
        totals=totals,
        next_cursor=next_cursor,
        currency_symbol=settings.currency_symbol,
    )
