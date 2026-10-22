"""Creative Studio monthly quotas: jobs, AI images and model tokens per workspace.

Defaults come from settings (``CREATIVE_JOBS_PER_MONTH``,
``CREATIVE_IMAGES_PER_MONTH``, ``CREATIVE_TOKENS_PER_MONTH``; 0 = no cap). A
portal admin may override them for one organisation; the override lives in
``Organization.settings["creative_limits"]`` and is written only through
:func:`set_overrides`, which the portal route calls.

Every check runs under a per-workspace transaction-scoped advisory lock
(``pg_advisory_xact_lock``), and the usage it admits is written in the same
transaction, so two requests arriving together cannot both take the last
slot. The lock is never held for long:

* :func:`admit_job` runs in the request's own transaction (the job row must
  be inserted under the lock) and waits at most ``REQUEST_LOCK_TIMEOUT`` for
  it; a workspace that busy gets a friendly 429 (:class:`QuotaBusy`). The
  request commits straight after.
* Reservations and settlements made while a job runs (images, tokens) each
  use a short session of their own that commits at once, so a job that runs
  for minutes never holds the lock between model calls. The job row is
  updated there under ``SELECT … FOR UPDATE``, and the caller's copy of the
  job is refreshed without being marked dirty, so the worker's own session
  never writes these counters.

Usage is counted from ``creative_jobs`` in the calendar month (UTC) the job
was created in: every job counts, whatever its status, so cancelling does not
hand quota back. Images and tokens are reserved on the job *before* the call
(tokens at the per-call ceiling) and settled to the real figure afterwards.

A server that reports no token counts (a keyless self-hosted vLLM or Ollama
may leave ``usage`` out) never settles below its reservation: the ceiling
stays charged, so such a server cannot be used to write past the monthly
token quota. The job's input count is then recorded as unknown (NULL) where
the column allows it, so usage reports say "unknown" rather than a number.
"""
from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import set_committed_value

from app.core.config import settings
from app.core.exceptions import AppError, NotFoundError
from app.core.logging import get_logger
from app.db.base import utcnow
from app.db.session import session_scope
from app.models.creative import CreativeJob
from app.models.workspace import Organization

log = get_logger(__name__)

#: Where a portal admin's override is kept on the organisation.
SETTINGS_KEY = "creative_limits"
FIELDS: tuple[str, ...] = ("jobs_per_month", "images_per_month", "tokens_per_month")
#: Highest value an override may set (0 = no cap).
MAX_LIMIT = 100_000_000

_LOCK_SQL = text("SELECT pg_advisory_xact_lock(hashtext(:k))")
#: Transaction-local ``lock_timeout`` (``SET LOCAL`` cannot take a parameter).
_SET_LOCK_TIMEOUT = text("SELECT set_config('lock_timeout', :v, true)")
_RESET_LOCK_TIMEOUT = text("SET LOCAL lock_timeout TO DEFAULT")
#: PostgreSQL's ``lock_not_available``: the ``lock_timeout`` ran out.
_LOCK_NOT_AVAILABLE = "55P03"
#: How long a request waits for another quota decision in its workspace.
REQUEST_LOCK_TIMEOUT = "5s"
#: How long a running job's short reservation transaction waits.
WORKER_LOCK_TIMEOUT = "30s"

#: The session factory for the short reservation transactions. Replaced in tests.
_session_scope: Callable[[str | None], AbstractContextManager[Session]] = session_scope

BUSY_MESSAGE = "Creative Studio is busy right now. Please try again in a moment."


class QuotaBusy(AppError):
    """The workspace's quota lock was not free in time. Safe to retry."""

    status_code = 429
    code = "busy"

    def __init__(self) -> None:
        super().__init__(BUSY_MESSAGE, details={"reason": "busy"})


class QuotaExceeded(AppError):
    """This month's allowance for the workspace is used up."""

    status_code = 429
    code = "quota_exceeded"

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message, details={"quota": kind})
        self.kind = kind


_MESSAGES: dict[str, str] = {
    "jobs_per_month": (
        "Your workspace has used all of this month's Creative Studio designs. "
        "The allowance resets on the 1st, or ask your administrator about a higher limit."
    ),
    "images_per_month": (
        "Your workspace has used all of this month's AI images. Choose "
        "\"No AI image\" to keep going, or ask your administrator about a higher limit."
    ),
    "tokens_per_month": (
        "Your workspace has used all of this month's AI writing allowance. "
        "It resets on the 1st, or ask your administrator about a higher limit."
    ),
}


@dataclass(frozen=True, slots=True)
class Limits:
    """Monthly caps in force. 0 means no cap."""

    jobs_per_month: int
    images_per_month: int
    tokens_per_month: int

    def as_dict(self) -> dict[str, int]:
        return {name: getattr(self, name) for name in FIELDS}


@dataclass(frozen=True, slots=True)
class Usage:
    jobs: int = 0
    images: int = 0
    tokens: int = 0

    def as_dict(self) -> dict[str, int]:
        return {"jobs": self.jobs, "images": self.images, "tokens": self.tokens}


LimitValue = Annotated[int, Field(strict=True, ge=0, le=MAX_LIMIT)]


class CreativeLimitsUpdate(BaseModel):
    """A portal admin's override. A field left out is unchanged; ``null``
    removes the override so the platform default applies again."""

    model_config = ConfigDict(extra="forbid")

    jobs_per_month: LimitValue | None = None
    images_per_month: LimitValue | None = None
    tokens_per_month: LimitValue | None = None


# ── Limits ────────────────────────────────────────────────────────────────
def defaults() -> Limits:
    return Limits(
        jobs_per_month=settings.creative_jobs_per_month,
        images_per_month=settings.creative_images_per_month,
        tokens_per_month=settings.creative_tokens_per_month,
    )


def _valid(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= MAX_LIMIT


def overrides_in(org_settings: dict[str, Any] | None) -> dict[str, int]:
    """The valid overrides stored on an organisation. Anything malformed is ignored."""
    raw = (org_settings or {}).get(SETTINGS_KEY)
    if not isinstance(raw, dict):
        return {}
    return {name: raw[name] for name in FIELDS if _valid(raw.get(name))}


def limits_from(org_settings: dict[str, Any] | None) -> Limits:
    base = defaults().as_dict()
    base.update(overrides_in(org_settings))
    return Limits(**base)


def _fresh_settings(db: Session, tenant_id: str) -> dict[str, Any]:
    """The organisation's settings as committed now, so an override saved
    after this request loaded the row still applies."""
    value = db.execute(
        select(Organization.settings).where(Organization.id == tenant_id)
    ).scalar_one_or_none()
    return dict(value) if isinstance(value, dict) else {}


# ── Usage ─────────────────────────────────────────────────────────────────
def month_window(now: datetime | None = None) -> tuple[datetime, datetime]:
    """``[start, end)`` of the calendar month (UTC) containing ``now``."""
    moment = (now or utcnow()).astimezone(UTC)
    start = moment.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    end = start.replace(year=start.year + 1, month=1) if start.month == 12 else start.replace(month=start.month + 1)
    return start, end


def usage(db: Session, tenant_id: str, *, now: datetime | None = None) -> Usage:
    """This month's jobs, AI images and tokens for one workspace."""
    start, end = month_window(now)
    row = db.execute(
        select(
            func.count(CreativeJob.id),
            func.coalesce(func.sum(CreativeJob.images_generated), 0),
            # NULL is "not reported" (see settle_tokens): the reservation in
            # tokens_out still counts, so a NULL never hides usage.
            func.coalesce(
                func.sum(
                    func.coalesce(CreativeJob.tokens_in, 0) + func.coalesce(CreativeJob.tokens_out, 0)
                ),
                0,
            ),
        ).where(
            CreativeJob.tenant_id == tenant_id,
            CreativeJob.created_at >= start,
            CreativeJob.created_at < end,
        )
    ).one()
    return Usage(jobs=int(row[0] or 0), images=int(row[1] or 0), tokens=int(row[2] or 0))


def _lock(db: Session, tenant_id: str) -> None:
    """Serialise quota decisions for one workspace until the transaction ends."""
    db.execute(_LOCK_SQL, {"k": f"creative_quota:{tenant_id}"})


def _is_lock_timeout(exc: DBAPIError) -> bool:
    return getattr(exc.orig, "sqlstate", None) == _LOCK_NOT_AVAILABLE


def _lock_or_busy(db: Session, tenant_id: str) -> None:
    """:func:`_lock`, turning a ``lock_timeout`` into :class:`QuotaBusy` (429)."""
    try:
        _lock(db, tenant_id)
    except DBAPIError as exc:
        if not _is_lock_timeout(exc):
            raise
        log.warning("Creative quota lock busy (tenant %s)", tenant_id)
        raise QuotaBusy() from exc


@contextmanager
def _short_transaction(tenant_id: str) -> Iterator[Session]:
    """A session of its own, pinned to the workspace, that commits on exit.

    A lock taken inside is released within milliseconds, not when the job's
    long-running transaction ends.
    """
    with _session_scope(tenant_id) as session:
        session.execute(_SET_LOCK_TIMEOUT, {"v": WORKER_LOCK_TIMEOUT})
        yield session


def _job_for_update(session: Session, job: CreativeJob) -> CreativeJob:
    """The committed job row, locked for this short transaction."""
    row = session.execute(
        select(CreativeJob)
        .where(CreativeJob.id == job.id, CreativeJob.tenant_id == job.tenant_id)
        .with_for_update()
    ).scalars().first()
    if row is None:
        raise NotFoundError("That design request could not be found")
    return row


def _mirror(job: CreativeJob, row: CreativeJob, *names: str) -> None:
    """Copy committed counters onto the caller's copy without dirtying it."""
    for name in names:
        set_committed_value(job, name, getattr(row, name))


def _refuse(kind: str, tenant_id: str) -> QuotaExceeded:
    log.info("Creative quota reached (tenant %s, %s)", tenant_id, kind)
    return QuotaExceeded(kind, _MESSAGES[kind])


# ── Admission ─────────────────────────────────────────────────────────────
def admit_job(db: Session, job: CreativeJob, *, now: datetime | None = None) -> Usage:
    """Add ``job`` if the workspace has a job left this month.

    Takes the lock, counts, then adds and flushes the job in the same
    transaction. The caller commits. Raises :class:`QuotaExceeded` otherwise,
    and :class:`QuotaBusy` (429) when the lock is not free within
    ``REQUEST_LOCK_TIMEOUT``, so a request thread never queues for long.
    """
    tenant_id = job.tenant_id
    if not tenant_id:
        raise ValueError("job.tenant_id must be set before admission")
    db.execute(_SET_LOCK_TIMEOUT, {"v": REQUEST_LOCK_TIMEOUT})
    _lock_or_busy(db, tenant_id)
    # Only the wait for the lock is bounded; the rest of the request is not.
    db.execute(_RESET_LOCK_TIMEOUT)
    limits = limits_from(_fresh_settings(db, tenant_id))
    used = usage(db, tenant_id, now=now)
    if limits.jobs_per_month and used.jobs >= limits.jobs_per_month:
        raise _refuse("jobs_per_month", tenant_id)
    db.add(job)
    db.flush()
    return Usage(jobs=used.jobs + 1, images=used.images, tokens=used.tokens)


# A running job's reservations. ``db`` is the job's own long session: it is
# never written here, so it never holds the quota lock. Each call commits in a
# short transaction of its own (:func:`_short_transaction`); ``job`` must be
# committed already (the worker claimed it).
def reserve_images(
    db: Session, job: CreativeJob, count: int, *, now: datetime | None = None
) -> None:
    """Reserve ``count`` AI images on ``job`` before generating them."""
    _ = db
    if count < 0:
        raise ValueError("count must not be negative")
    if count == 0:
        return
    tenant_id = job.tenant_id
    with _short_transaction(tenant_id) as session:
        _lock_or_busy(session, tenant_id)
        limits = limits_from(_fresh_settings(session, tenant_id))
        used = usage(session, tenant_id, now=now)
        if limits.images_per_month and used.images + count > limits.images_per_month:
            raise _refuse("images_per_month", tenant_id)
        row = _job_for_update(session, job)
        row.images_generated = int(row.images_generated or 0) + count
        session.flush()
        _mirror(job, row, "images_generated")


def release_images(db: Session, job: CreativeJob, count: int) -> None:
    """Hand back images reserved but not made (the call failed)."""
    _ = db
    if count <= 0:
        return
    with _short_transaction(job.tenant_id) as session:
        row = _job_for_update(session, job)
        row.images_generated = max(0, int(row.images_generated or 0) - count)
        session.flush()
        _mirror(job, row, "images_generated")


def reserve_tokens(
    db: Session, job: CreativeJob, max_tokens: int, *, now: datetime | None = None
) -> int:
    """Reserve one model call's ceiling on ``job``; returns the amount reserved.

    Pass the result to :func:`settle_tokens` once the real usage is known.
    """
    _ = db
    if max_tokens <= 0:
        raise ValueError("max_tokens must be positive")
    tenant_id = job.tenant_id
    with _short_transaction(tenant_id) as session:
        _lock_or_busy(session, tenant_id)
        limits = limits_from(_fresh_settings(session, tenant_id))
        used = usage(session, tenant_id, now=now)
        if limits.tokens_per_month and used.tokens + max_tokens > limits.tokens_per_month:
            raise _refuse("tokens_per_month", tenant_id)
        row = _job_for_update(session, job)
        row.tokens_out = int(row.tokens_out or 0) + max_tokens
        session.flush()
        _mirror(job, row, "tokens_out")
    return max_tokens


def settle_tokens(
    db: Session,
    job: CreativeJob,
    reserved: int,
    *,
    tokens_in: int | None,
    tokens_out: int | None,
) -> None:
    """Replace a reservation with what the call really used.

    ``None`` for either count means the server did not report usage
    (``providers.usage_reported`` is False). The reservation is then **kept**
    as the charge, never refunded, so a keyless server cannot bypass the
    token quota. Previously counted input is folded into ``tokens_out``
    before the job's input count becomes NULL ("unknown"), so a later
    unreported call cannot wipe usage already counted. Once unknown, it
    stays unknown for the job. Settling never raises usage above the
    reservation, so it needs only the row lock, not the quota lock.
    """
    _ = db
    with _short_transaction(job.tenant_id) as session:
        row = _job_for_update(session, job)
        if tokens_in is None or tokens_out is None:
            counted_in = int(row.tokens_in) if row.tokens_in is not None else 0
            row.tokens_out = int(row.tokens_out or 0) + counted_in
            row.tokens_in = None
            log.info("Creative job %s: model reported no usage; reservation kept", job.id)
        else:
            row.tokens_out = max(0, int(row.tokens_out or 0) - max(0, reserved) + max(0, tokens_out))
            if row.tokens_in is not None:
                row.tokens_in = int(row.tokens_in) + max(0, tokens_in)
        session.flush()
        _mirror(job, row, "tokens_in", "tokens_out")


# ── Portal admin overrides ────────────────────────────────────────────────
def set_overrides(
    db: Session, org_id: str, update: CreativeLimitsUpdate
) -> tuple[Organization, dict[str, dict[str, int | None]]]:
    """Apply a portal admin's override to one organisation.

    The row is locked (``SELECT … FOR UPDATE``) so a concurrent settings
    write cannot drop the change. Returns the organisation and, per changed
    field, ``{"from": old, "to": new}`` (``None`` = platform default). The
    caller authorises (portal admin only), audits and commits.
    """
    org = db.execute(
        select(Organization).where(Organization.id == org_id).with_for_update()
    ).scalar_one_or_none()
    if org is None:
        raise NotFoundError("That workspace was not found")

    current = overrides_in(org.settings)
    updated = dict(current)
    for name in update.model_fields_set:
        value = getattr(update, name)
        if value is None:
            updated.pop(name, None)
        else:
            updated[name] = int(value)

    changed = {
        name: {"from": current.get(name), "to": updated.get(name)}
        for name in FIELDS
        if current.get(name) != updated.get(name)
    }
    if changed:
        stored = dict(org.settings or {})
        if updated:
            stored[SETTINGS_KEY] = updated
        else:
            stored.pop(SETTINGS_KEY, None)
        # A new dict, so the JSONB column is seen as changed.
        org.settings = stored
        db.flush()
    return org, changed


__all__ = [
    "FIELDS",
    "MAX_LIMIT",
    "SETTINGS_KEY",
    "CreativeLimitsUpdate",
    "Limits",
    "QuotaBusy",
    "QuotaExceeded",
    "Usage",
    "admit_job",
    "defaults",
    "limits_from",
    "month_window",
    "overrides_in",
    "release_images",
    "reserve_images",
    "reserve_tokens",
    "set_overrides",
    "settle_tokens",
    "usage",
]
