"""Which connectors have been proven against a real account — the shared answer.

Contract (stable; other modules depend on it):

* ``status(db, slug) -> VerificationStatus`` — the latest live verification of
  a connector type, platform-wide.
* ``is_verified(db, slug) -> bool`` — passed a live check within
  ``VERIFIED_FOR_DAYS``.

Around those two:

* **Storage** — table ``connector_verifications`` (migration 0018), one row
  per run. Verified means: the *latest* run of that connector type passed,
  and it finished within ``VERIFIED_FOR_DAYS``. A later failure un-verifies
  it at once; a pass simply expires.
* **Cache** — every worker keeps a snapshot of the latest run per slug (and
  the platform settings) for :data:`CACHE_TTL_SECONDS`, read on a session of
  its own so a missing table can never spoil the caller's transaction. Until
  migration 0018 has run the snapshot is empty: everything reads unverified,
  with one warning in the log.
* **Runner** — :func:`run` builds a workspace's connector the way the agents
  do and runs its ``verify_steps()`` (``manage.py verify``, Portal Admin ->
  Verification).
* **Platform setting** — ``hide_unverified_connectors``: when on, connector
  types that are not verified are left out of workspace catalogues (see
  ``portal_features.hidden_connector_slugs``).
"""
from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import Any

from app.connectors.base.verification import (
    OPTION_PREFIX,
    StepResult,
    VerifyReport,
    VerifySkipped,
    VerifyStep,
)
from app.core.logging import get_logger

log = get_logger(__name__)

#: A pass older than this no longer counts — vendors change their APIs.
VERIFIED_FOR_DAYS = 30
#: How long a worker trusts its snapshot.
CACHE_TTL_SECONDS = 30.0

ENVIRONMENTS = ("live", "test_account", "sandbox")
ENVIRONMENT_LABELS = {"live": "Live account", "test_account": "Test account", "sandbox": "Sandbox"}

HIDE_UNVERIFIED_KEY = "hide_unverified_connectors"

NEVER_CHECKED = "Not yet checked against a real account"
BETA_LABEL = "Beta"
BETA_TOOLTIP = "Not yet confirmed against a live account. It may not work as expected."


@dataclass(frozen=True, slots=True)
class VerificationStatus:
    slug: str
    verified: bool
    #: "sandbox" | "test_account" | "live" | "" when never run.
    environment: str = ""
    last_run_at: datetime | None = None
    last_ok_at: datetime | None = None
    #: Plain words, e.g. "Not yet checked against a real account".
    summary: str = NEVER_CHECKED
    #: The latest run's result; None when never run.
    ok: bool | None = None
    #: Why the latest run failed (the failing step's detail), if it did.
    reason: str = ""


# ── Snapshot and cache ─────────────────────────────────────────────────────
@dataclass(frozen=True, slots=True)
class LatestRun:
    slug: str
    ok: bool
    environment: str
    finished_at: datetime | None
    started_at: datetime | None = None
    note: str = ""
    steps: tuple[dict[str, Any], ...] = ()
    run_by: str = ""
    workspace_id: str | None = None
    id: str = ""


@dataclass(frozen=True, slots=True)
class Snapshot:
    #: slug -> the latest run of that connector type.
    latest: dict[str, LatestRun] = field(default_factory=dict)
    #: slug -> when it last passed (any run).
    last_ok: dict[str, datetime] = field(default_factory=dict)
    #: Platform settings, ``{key: value}``.
    settings: dict[str, Any] = field(default_factory=dict)


def _row_to_run(row: Any) -> LatestRun:  # noqa: ANN401
    return LatestRun(
        slug=row.slug,
        ok=bool(row.ok),
        environment=row.environment or "live",
        finished_at=row.finished_at,
        started_at=row.started_at,
        note=row.note or "",
        steps=tuple(row.steps or ()),
        run_by=row.run_by or "",
        workspace_id=row.workspace_id,
        id=row.id,
    )


def read_snapshot(db: Any) -> Snapshot:  # noqa: ANN401
    """Latest run per slug, last pass per slug, and the platform settings."""
    from sqlalchemy import func, select

    from app.models.verification import ConnectorVerification, PlatformSetting

    rows = db.execute(
        select(ConnectorVerification)
        .distinct(ConnectorVerification.slug)
        .order_by(ConnectorVerification.slug, ConnectorVerification.finished_at.desc())
    ).scalars()
    latest = {row.slug: _row_to_run(row) for row in rows}
    last_ok = {
        slug: at
        for slug, at in db.execute(
            select(ConnectorVerification.slug, func.max(ConnectorVerification.finished_at))
            .where(ConnectorVerification.ok.is_(True))
            .group_by(ConnectorVerification.slug)
        ).all()
    }
    settings_ = {row.key: row.value for row in db.execute(select(PlatformSetting)).scalars()}
    return Snapshot(latest=latest, last_ok=last_ok, settings=settings_)


def _load_from_database() -> Snapshot:
    from app.db.session import SessionLocal

    with SessionLocal() as db:
        return read_snapshot(db)


_lock = threading.Lock()
_cached: Snapshot | None = None
_cached_at = 0.0
_warned = False
#: Replaced in tests (see tests/conftest.py), which have no database.
_loader: Callable[[], Snapshot] = _load_from_database


def set_loader(loader: Callable[[], Snapshot] | None) -> None:
    """Swap where snapshots come from (tests). ``None`` restores the database."""
    global _loader
    _loader = loader or _load_from_database
    invalidate_cache()


def invalidate_cache() -> None:
    global _cached, _cached_at
    with _lock:
        _cached, _cached_at = None, 0.0


def prime_cache(snap: Snapshot) -> None:
    """Use a snapshot just read on a request's own session."""
    global _cached, _cached_at
    with _lock:
        _cached, _cached_at = snap, time.monotonic()


def snapshot() -> Snapshot:
    global _cached, _cached_at, _warned
    with _lock:
        if _cached is not None and time.monotonic() - _cached_at < CACHE_TTL_SECONDS:
            return _cached
    try:
        fresh = _loader()
    except Exception as exc:  # noqa: BLE001 - unverified is the safe answer
        if not _warned:
            log.warning(
                "Connector verification results could not be read (%s); every connector "
                "reads as unverified. Run the database migrations (0018) if this persists.",
                type(exc).__name__,
            )
            _warned = True
        fresh = Snapshot()
    with _lock:
        _cached, _cached_at = fresh, time.monotonic()
    return fresh


# ── The contract ───────────────────────────────────────────────────────────
def _now() -> datetime:
    from app.db.base import utcnow

    return utcnow()


def _date(at: datetime | None) -> str:
    return at.strftime("%d %b %Y") if at else ""


def failure_reason(run: LatestRun) -> str:
    for step in run.steps:
        if not step.get("ok") and not step.get("skipped"):
            return str(step.get("detail") or "")
    return run.note


def status_from(snap: Snapshot, slug: str, *, now: datetime | None = None) -> VerificationStatus:
    """The pure rule, on a snapshot (tested without a database)."""
    now = now or _now()
    run = snap.latest.get(slug)
    last_ok = snap.last_ok.get(slug)
    if run is None:
        return VerificationStatus(slug=slug, verified=False, last_ok_at=last_ok)
    env_label = ENVIRONMENT_LABELS.get(run.environment, run.environment)
    if not run.ok:
        reason = failure_reason(run)
        return VerificationStatus(
            slug=slug,
            verified=False,
            environment=run.environment,
            last_run_at=run.finished_at,
            last_ok_at=last_ok,
            summary=f"Failed on {_date(run.finished_at)}"
            + (f": {reason}" if reason else ""),
            ok=False,
            reason=reason,
        )
    fresh = run.finished_at is not None and now - run.finished_at <= timedelta(
        days=VERIFIED_FOR_DAYS
    )
    if not fresh:
        return VerificationStatus(
            slug=slug,
            verified=False,
            environment=run.environment,
            last_run_at=run.finished_at,
            last_ok_at=last_ok or run.finished_at,
            summary=(
                f"Passed on {_date(run.finished_at)} ({env_label}), but that is more than "
                f"{VERIFIED_FOR_DAYS} days ago. Check it again."
            ),
            ok=True,
        )
    return VerificationStatus(
        slug=slug,
        verified=True,
        environment=run.environment,
        last_run_at=run.finished_at,
        last_ok_at=last_ok or run.finished_at,
        summary=f"Verified on {_date(run.finished_at)} ({env_label})",
        ok=True,
    )


def status(db: Any, slug: str) -> VerificationStatus:  # noqa: ANN401, ARG001
    """Latest verification for a connector type (cached, platform-wide).

    ``db`` is accepted for the contract; the lookup reads on a session of its
    own so it can never disturb the caller's transaction.
    """
    return status_from(snapshot(), slug)


def is_verified(db: Any, slug: str) -> bool:  # noqa: ANN401
    return status(db, slug).verified


def label_for(slug: str) -> dict[str, Any]:
    """What a workspace sees: ``{verified, label, tooltip}``. Label empty when verified."""
    verified = status(None, slug).verified
    return {
        "verified": verified,
        "label": "" if verified else BETA_LABEL,
        "tooltip": "" if verified else BETA_TOOLTIP,
    }


# ── Platform settings ──────────────────────────────────────────────────────
def hide_unverified() -> bool:
    return bool(snapshot().settings.get(HIDE_UNVERIFIED_KEY, False))


def set_setting(db: Any, *, key: str, value: Any, actor: str) -> None:  # noqa: ANN401
    """Upsert one platform setting. The caller commits, then calls
    :func:`invalidate_cache`."""
    from sqlalchemy import select

    from app.models.verification import PlatformSetting

    row = db.execute(select(PlatformSetting).where(PlatformSetting.key == key)).scalar_one_or_none()
    if row is None:
        row = PlatformSetting(key=key, value=value, updated_by=actor[:255])
        db.add(row)
    else:
        row.value = value
        row.updated_by = actor[:255]
    db.flush()


# ── Running ────────────────────────────────────────────────────────────────
#: Credential keys whose values are markers, not account data.
_MARKER_KEYS = frozenset({"accountEnvironment", "keySource"})


def _scrub_values(connector: Any) -> list[tuple[str, str]]:  # noqa: ANN401
    """(value, replacement) for every credential value worth hiding, longest first."""
    spec = getattr(connector, "spec", None)
    secret = {f.key for f in getattr(spec, "fields", ()) if f.is_secret}
    pairs: list[tuple[str, str]] = []
    for key, value in (connector.credentials.values or {}).items():
        if key in _MARKER_KEYS or not isinstance(value, str) or len(value) < 6:
            continue
        hidden = "[hidden]" if key in secret or "token" in key.lower() or "secret" in key.lower() else f"[{key}]"
        pairs.append((value, hidden))
    pairs.sort(key=lambda p: len(p[0]), reverse=True)
    return pairs


def scrub(value: Any, pairs: list[tuple[str, str]]) -> Any:  # noqa: ANN401
    """``value`` (a string, or JSON-like) with every credential value replaced."""
    if isinstance(value, str):
        for original, hidden in pairs:
            if original in value:
                value = value.replace(original, hidden)
        return value
    if isinstance(value, dict):
        return {scrub(str(k), pairs): scrub(v, pairs) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [scrub(v, pairs) for v in value]
    return value


def _failure_detail(connector: Any, exc: BaseException) -> str:  # noqa: ANN401
    from app.core.user_messages import public_error_message

    try:
        diagnosed = connector.diagnose(exc) if isinstance(exc, Exception) else None
    except Exception:  # noqa: BLE001
        diagnosed = None
    return diagnosed or public_error_message(
        exc, fallback="This step failed. The server log has the technical detail."
    )


SKIP_NO_WRITES = (
    "Skipped: this step changes something at the vendor (and undoes it). "
    "It runs only when test writes are allowed."
)
SKIP_LIVE_ACCOUNT = (
    "Skipped: this step runs only on a test or sandbox account, and this "
    "connection is a live account. Mark the connection as a test account to run it."
)
SKIP_AFTER_FAILURE = "Not run, because an earlier step failed."

#: Longest run-only option value accepted (an image address, a "yes").
MAX_OPTION_LENGTH = 2000


def clean_options(options: dict[str, Any] | None) -> dict[str, str]:
    """Run-only options, checked: keys must start with ``verify`` (so nothing
    can overwrite a real credential), values are short strings.

    Raises ValueError with a plain message naming what was wrong.
    """
    out: dict[str, str] = {}
    for raw_key, raw_value in (options or {}).items():
        key = str(raw_key or "").strip()
        if not key.startswith(OPTION_PREFIX) or len(key) <= len(OPTION_PREFIX) or len(key) > 64:
            raise ValueError(
                f"Option {key or '(blank)'!r} is not allowed. Only options whose names start "
                f"with {OPTION_PREFIX!r} can be set for a run (for example verifyImageUrl)."
            )
        if not key.replace("_", "").isalnum():
            raise ValueError(f"Option {key!r} may use only letters, digits and underscores.")
        value = "" if raw_value is None else str(raw_value).strip()
        if len(value) > MAX_OPTION_LENGTH:
            raise ValueError(f"The value of {key} is too long.")
        out[key] = value
    return out


def execute(
    connector: Any,  # noqa: ANN401
    *,
    allow_writes: bool = False,
    environment: str | None = None,
    options: dict[str, Any] | None = None,
    recorder: Any = None,  # noqa: ANN401
    clock: Callable[[], float] = time.perf_counter,
) -> VerifyReport:
    """Run one connector's ``verify_steps()`` in order. No database.

    ``environment`` is the operator's word for what the connection points at
    (``--environment``); when given it replaces ``account_environment()``
    for this run, and is also set on the instance's credentials (in memory
    only) so the connector's own checks agree.

    ``options`` are run-only ``verify*`` values (``--set``), put on the
    instance's credentials the same way: in memory, for this run only.

    A step that raises :class:`VerifySkipped` (an opt-in not given) is
    recorded as skipped with its message, and the run carries on.
    """
    for key, value in clean_options(options).items():
        connector.credentials.values[key] = value
    if environment:
        if environment not in ENVIRONMENTS:
            raise ValueError(f"environment must be one of {', '.join(ENVIRONMENTS)}")
        connector.credentials.values["accountEnvironment"] = environment
    env = environment or connector.account_environment()
    pairs = _scrub_values(connector)
    try:
        steps: list[VerifyStep] = list(connector.verify_steps() or [])
    except Exception as exc:  # noqa: BLE001
        return VerifyReport(
            slug=connector.slug,
            environment=env,
            ok=False,
            note=scrub("The checks could not be prepared: " + _failure_detail(connector, exc), pairs),
        )
    if not steps:
        return VerifyReport(
            slug=connector.slug,
            environment=env,
            ok=False,
            note="No live check has been written for this connector yet.",
        )

    results: list[StepResult] = []
    notes: list[str] = []
    failed = False
    for step in steps:
        result = StepResult(key=step.key, label=step.label, kind=step.kind, ok=False)
        if failed:
            result.skipped, result.detail = True, SKIP_AFTER_FAILURE
            results.append(result)
            continue
        if step.kind == "write":
            if not allow_writes:
                result.skipped, result.detail = True, SKIP_NO_WRITES
            elif step.requires_test_account and env == "live":
                result.skipped, result.detail = True, SKIP_LIVE_ACCOUNT
            if result.skipped:
                if result.detail not in notes:
                    notes.append(result.detail)
                results.append(result)
                continue

        started = clock()
        try:
            if recorder is not None:
                with recorder.step(step.key):
                    detail, evidence = step.run()
            else:
                detail, evidence = step.run()
            result.ok = True
            result.detail = scrub(str(detail or ""), pairs)
            result.evidence = scrub(dict(evidence or {}), pairs)
        except VerifySkipped as exc:
            result.skipped = True
            result.detail = scrub(str(exc) or "Skipped by the check itself.", pairs)
            if result.detail not in notes:
                notes.append(result.detail)
        except Exception as exc:  # noqa: BLE001 - recorded as the failure reason
            log.warning("verify %s/%s failed: %s", connector.slug, step.key, type(exc).__name__)
            result.ok = False
            result.detail = scrub(_failure_detail(connector, exc), pairs)
            failed = True
        result.seconds = round(clock() - started, 3)
        results.append(result)

    ran = [r for r in results if not r.skipped]
    ok = bool(ran) and all(r.ok for r in ran)
    if not ran:
        notes.insert(0, "Nothing was checked: every step was skipped.")
    return VerifyReport(
        slug=connector.slug, environment=env, ok=ok, steps=results, note=" ".join(notes)
    )


def has_steps(slug: str) -> bool:
    """Whether a live check has been written for this connector type."""
    from app.connectors.base import registry
    from app.connectors.base.connector import BaseConnector

    klass = registry.get_class(slug)
    if klass is None:
        return False
    if getattr(klass, "verify_steps", None) is BaseConnector.verify_steps:
        return False
    try:
        from app.connectors.base.credentials import Credentials

        return bool(klass(Credentials()).verify_steps())
    except Exception:  # noqa: BLE001 - written, but needs real credentials to build
        return True


def store(
    db: Any,  # noqa: ANN401
    report: VerifyReport,
    *,
    started_at: datetime,
    finished_at: datetime,
    run_by: str = "",
    workspace_id: str | None = None,
) -> Any:  # noqa: ANN401
    """Add the run as a row. The caller commits, then calls :func:`invalidate_cache`."""
    from app.models.verification import ConnectorVerification

    row = ConnectorVerification(
        slug=report.slug,
        environment=report.environment,
        ok=report.ok,
        steps=[asdict(s) for s in report.steps],
        note=report.note,
        run_by=(run_by or "")[:255],
        workspace_id=workspace_id,
        started_at=started_at,
        finished_at=finished_at,
    )
    db.add(row)
    db.flush()
    return row


@dataclass(slots=True)
class RunOutcome:
    report: VerifyReport | None = None
    stored: bool = False
    cassettes: list[str] = field(default_factory=list)


def run(
    db: Any,  # noqa: ANN401
    org: Any,  # noqa: ANN401
    slug: str,
    *,
    allow_writes: bool = False,
    record: bool = False,
    environment: str | None = None,
    options: dict[str, Any] | None = None,
    run_by: str = "cli",
    cassette_dir: Any = None,  # noqa: ANN401
    outcome: RunOutcome | None = None,
) -> VerifyReport:
    """Verify one connector type with one workspace's connection, and store it.

    Builds the connector exactly as the agents do
    (``connectors.build_connector_bundle``), runs its steps (see
    :func:`execute`), stores the report (the caller commits), and with
    ``record`` writes the scrubbed HTTP exchanges as cassettes.

    Raises ``NotFoundError`` for an unknown connector type and
    ``InvalidInputError`` when this workspace has no usable connection. A
    connector with no steps returns a failed report that is *not* stored.
    """
    from app.connectors.base import registry
    from app.core.exceptions import InvalidInputError, NotFoundError
    from app.services.connectors import build_connector_bundle

    if registry.get_spec(slug) is None:
        raise NotFoundError(f"There is no connector called {slug!r}.")
    if environment and environment not in ENVIRONMENTS:
        raise InvalidInputError("Environment must be live, test_account or sandbox.")
    try:
        options = clean_options(options)
    except ValueError as exc:
        raise InvalidInputError(str(exc)) from exc

    bundle = build_connector_bundle(db, org)
    try:
        connector = bundle.instances.get(slug)
        if connector is None:
            raise InvalidInputError(
                f"{bundle.names.get(slug, slug)} is not connected in {org.name}, or its setup "
                "is incomplete. Connect it there first."
            )
        started_at = _now()
        recorder = None
        if record:
            from app.connectors.base.cassettes import Recorder

            recorder = Recorder()
        if recorder is not None:
            with recorder.active():
                report = execute(
                    connector,
                    allow_writes=allow_writes,
                    environment=environment,
                    options=options,
                    recorder=recorder,
                )
        else:
            report = execute(
                connector, allow_writes=allow_writes, environment=environment, options=options
            )
        finished_at = _now()

        if not report.steps:
            if outcome is not None:
                outcome.report = report
            return report

        store(
            db,
            report,
            started_at=started_at,
            finished_at=finished_at,
            run_by=run_by,
            workspace_id=org.id,
        )
        written: list[str] = []
        if recorder is not None and report.ok:
            written = _write_cassettes(connector, report, recorder, cassette_dir)
        elif recorder is not None:
            report.note = (report.note + " No cassettes were written, because the run failed.").strip()
        if outcome is not None:
            outcome.report, outcome.stored, outcome.cassettes = report, True, written
        return report
    finally:
        bundle.close()


def _write_cassettes(connector: Any, report: VerifyReport, recorder: Any, directory: Any) -> list[str]:  # noqa: ANN401
    from app.connectors.base.cassettes import Scrubber
    from app.connectors.base.credentials import FieldKind

    spec = connector.spec
    scrubber = Scrubber.for_credentials(
        dict(connector.credentials.values or {}),
        secret_keys={f.key for f in spec.fields if f.is_secret},
        keep_keys={f.key for f in spec.fields if f.kind in (FieldKind.NUMBER, FieldKind.MODEL)},
        defaults={f.key: f.default for f in spec.fields if f.default},
    )
    steps = [
        {"key": s.key, "label": s.label, "kind": s.kind}
        for s in report.steps
        if s.ok and not s.skipped
    ]
    paths = recorder.write(
        slug=report.slug,
        steps=steps,
        environment=report.environment,
        scrubber=scrubber,
        recorded_at=_now().date().isoformat(),
        oauth_completed=bool(connector.credentials.oauth_completed),
        directory=directory,
    )
    return [str(p) for p in paths]


def report_to_dict(report: VerifyReport) -> dict[str, Any]:
    return {
        "slug": report.slug,
        "environment": report.environment,
        "ok": report.ok,
        "note": report.note,
        "steps": [asdict(s) for s in report.steps],
    }
