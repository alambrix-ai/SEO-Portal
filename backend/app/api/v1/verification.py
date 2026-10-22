"""Portal Admin -> Verification: live checks of each connector type.

Portal admins only (PORTAL_ADMIN_EMAILS). Lists every connector type with its
latest live verification, starts a run with one workspace's connection, and
holds the "hide unverified connectors" platform setting.

Runs happen in the background, the way the website analysis does: a live
check calls a vendor several times, each with its own timeout and retries, so
it can take longer than a request should wait. The POST returns at once; the
page polls the list, which shows the run as ``running`` until its result is
stored. A run lost to a server restart leaves nothing behind (results are only
written when a run finishes), so it can simply be started again.
"""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Any

from fastapi import APIRouter
from pydantic import Field
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from app.api.deps import ClientIp, DbSession, PortalAdminDep
from app.connectors.base import registry
from app.core.exceptions import AppError, ConflictError, InvalidInputError, NotFoundError
from app.core.logging import get_logger
from app.models.workspace import Organization
from app.schemas.common import ApiModel, Toast
from app.services import audit, verification

log = get_logger(__name__)

router = APIRouter(prefix="/portal/verification", tags=["portal"])


# ── Shapes ─────────────────────────────────────────────────────────────────
class VerificationStepOut(ApiModel):
    key: str
    label: str
    kind: str
    ok: bool
    skipped: bool = False
    detail: str = ""
    evidence: dict[str, Any] = Field(default_factory=dict)
    seconds: float = 0.0


class VerificationRunOut(ApiModel):
    ok: bool
    environment: str
    environment_label: str
    started_at: datetime | None = None
    finished_at: datetime | None = None
    run_by: str = ""
    workspace_id: str | None = None
    workspace_name: str = ""
    note: str = ""
    steps: list[VerificationStepOut] = Field(default_factory=list)


class VerificationRowOut(ApiModel):
    slug: str
    name: str
    category: str
    #: A live check has been written for this connector.
    has_steps: bool
    verified: bool
    #: verified | failed | expired | never | no_check
    state: str
    summary: str
    environment: str = ""
    environment_label: str = ""
    last_run_at: datetime | None = None
    last_ok_at: datetime | None = None
    reason: str = ""
    running: bool = False
    #: A background run that could not start (not connected, ...), until the next run.
    run_error: str = ""
    latest: VerificationRunOut | None = None


class VerificationListOut(ApiModel):
    hide_unverified_connectors: bool
    verified_for_days: int
    #: False before migration 0018 has run: nothing can be stored yet.
    storage_ready: bool = True
    items: list[VerificationRowOut] = Field(default_factory=list)


class VerificationWorkspaceOut(ApiModel):
    id: str
    name: str
    slug: str
    environment: str
    environment_label: str


class VerificationRunRequest(ApiModel):
    workspace_id: str = Field(min_length=1, max_length=64)
    allow_writes: bool = False
    #: Overrides what the connection says it points at, for this run only.
    environment: str = ""
    #: Run-only ``verify*`` options (verifyImageUrl, verifyAllowPaidPost),
    #: given to the connector in memory for this run and never saved.
    options: dict[str, str] = Field(default_factory=dict)


class VerificationRunStarted(ApiModel):
    status: str = "running"
    toast: Toast | None = None


class VerificationSettingsIn(ApiModel):
    hide_unverified_connectors: bool


class VerificationSettingsOut(ApiModel):
    hide_unverified_connectors: bool
    toast: Toast | None = None


# ── Background runs ────────────────────────────────────────────────────────
_pool: ThreadPoolExecutor | None = None
_pool_lock = threading.Lock()
#: slug -> who started it and when. In memory: a restart forgets it, which is
#: right, since the run itself stopped too.
_running: dict[str, dict[str, Any]] = {}
#: slug -> why the last background run could not run at all.
_errors: dict[str, str] = {}
_run_lock = threading.Lock()


def _executor() -> ThreadPoolExecutor:
    global _pool
    with _pool_lock:
        if _pool is None:
            _pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="verify")
        return _pool


def _run_in_background(
    slug: str,
    org_id: str,
    allow_writes: bool,
    environment: str,
    run_by: str,
    options: dict[str, str] | None = None,
) -> None:
    from app.db.session import session_scope

    try:
        with session_scope(org_id) as db:
            org = db.get(Organization, org_id)
            if org is None:
                raise NotFoundError("That workspace was not found.")
            verification.run(
                db,
                org,
                slug,
                allow_writes=allow_writes,
                environment=environment or None,
                options=options,
                run_by=run_by,
            )
        _errors.pop(slug, None)
    except AppError as exc:
        _errors[slug] = exc.message
    except SQLAlchemyError:
        log.exception("Verification of %s could not be stored", slug)
        _errors[slug] = (
            "The result could not be saved. The database may need its latest update "
            "(migration 0018)."
        )
    except Exception:  # noqa: BLE001 - one run must never take the worker down
        log.exception("Verification of %s failed unexpectedly", slug)
        _errors[slug] = "The check stopped unexpectedly. The server log has the detail."
    finally:
        _running.pop(slug, None)
        verification.invalidate_cache()


# ── Helpers ────────────────────────────────────────────────────────────────
def _fresh_snapshot(db) -> tuple[verification.Snapshot, bool]:  # noqa: ANN001
    """Read on this request's session (inside a savepoint, so a missing table
    cannot spoil the request), and share it with this worker's cache."""
    try:
        with db.begin_nested():
            snap = verification.read_snapshot(db)
    except SQLAlchemyError:
        return verification.Snapshot(), False
    verification.prime_cache(snap)
    return snap, True


def _environment_of(hints: dict | None) -> str:
    value = str((hints or {}).get("accountEnvironment") or "").strip().lower()
    return value if value in verification.ENVIRONMENTS else "live"


def _row(
    spec,  # noqa: ANN001
    snap: verification.Snapshot,
    workspace_names: dict[str, str],
) -> VerificationRowOut:
    current = verification.status_from(snap, spec.slug)
    has = verification.has_steps(spec.slug)
    latest_run = snap.latest.get(spec.slug)
    if current.verified:
        state = "verified"
    elif latest_run is not None and not latest_run.ok:
        state = "failed"
    elif latest_run is not None:
        state = "expired"
    elif has:
        state = "never"
    else:
        state = "no_check"
    summary = current.summary
    if state == "no_check":
        summary = "No live check written yet"
    elif state == "never":
        summary = "Never checked"
    latest = None
    if latest_run is not None:
        latest = VerificationRunOut(
            ok=latest_run.ok,
            environment=latest_run.environment,
            environment_label=verification.ENVIRONMENT_LABELS.get(
                latest_run.environment, latest_run.environment
            ),
            started_at=latest_run.started_at,
            finished_at=latest_run.finished_at,
            run_by=latest_run.run_by,
            workspace_id=latest_run.workspace_id,
            workspace_name=workspace_names.get(latest_run.workspace_id or "", ""),
            note=latest_run.note,
            steps=[VerificationStepOut(**s) for s in latest_run.steps if isinstance(s, dict)],
        )
    return VerificationRowOut(
        slug=spec.slug,
        name=spec.name,
        category=spec.category,
        has_steps=has,
        verified=current.verified,
        state=state,
        summary=summary,
        environment=current.environment,
        environment_label=verification.ENVIRONMENT_LABELS.get(current.environment, ""),
        last_run_at=current.last_run_at,
        last_ok_at=current.last_ok_at,
        reason=current.reason,
        running=spec.slug in _running,
        run_error=_errors.get(spec.slug, ""),
        latest=latest,
    )


# ── Routes ─────────────────────────────────────────────────────────────────
@router.get("", response_model=VerificationListOut)
def list_verification(_: PortalAdminDep, db: DbSession) -> VerificationListOut:
    """Every connector type: whether a live check exists, and its latest result."""
    snap, ready = _fresh_snapshot(db)
    names = {org.id: org.name for org in db.execute(select(Organization)).scalars()}
    return VerificationListOut(
        hide_unverified_connectors=bool(
            snap.settings.get(verification.HIDE_UNVERIFIED_KEY, False)
        ),
        verified_for_days=verification.VERIFIED_FOR_DAYS,
        storage_ready=ready,
        items=[_row(spec, snap, names) for spec in registry.all_specs()],
    )


@router.get("/{slug}/workspaces", response_model=list[VerificationWorkspaceOut])
def connected_workspaces(
    slug: str, current: PortalAdminDep, db: DbSession
) -> list[VerificationWorkspaceOut]:
    """Workspaces with this connector connected: where a run can borrow a connection."""
    from app.db.session import bind_tenant
    from app.models.connector import ConnectorRecord

    if registry.get_spec(slug) is None:
        raise NotFoundError("That connector was not found")
    out: list[VerificationWorkspaceOut] = []
    orgs = list(db.execute(select(Organization).order_by(Organization.name)).scalars())
    try:
        for org in orgs:
            bind_tenant(db, org.id)
            record = db.execute(
                select(ConnectorRecord).where(
                    ConnectorRecord.tenant_id == org.id,
                    ConnectorRecord.slug == slug,
                    ConnectorRecord.connected.is_(True),
                )
            ).scalar_one_or_none()
            if record is None:
                continue
            env = _environment_of(record.credential_hints)
            out.append(
                VerificationWorkspaceOut(
                    id=org.id,
                    name=org.name,
                    slug=org.slug,
                    environment=env,
                    environment_label=verification.ENVIRONMENT_LABELS[env],
                )
            )
    finally:
        bind_tenant(db, current.tenant_id)
    return out


@router.post("/{slug}/run", response_model=VerificationRunStarted, status_code=202)
def start_run(
    slug: str,
    payload: VerificationRunRequest,
    current: PortalAdminDep,
    db: DbSession,
    ip: ClientIp,
) -> VerificationRunStarted:
    """Start a live check in the background with one workspace's connection."""
    spec = registry.get_spec(slug)
    if spec is None:
        raise NotFoundError("That connector was not found")
    if not verification.has_steps(slug):
        raise InvalidInputError(f"No live check has been written for {spec.name} yet.")
    if payload.environment and payload.environment not in verification.ENVIRONMENTS:
        raise InvalidInputError("Environment must be live, test_account or sandbox.")
    try:
        options = verification.clean_options(payload.options)
    except ValueError as exc:
        # Authored copy only; the exception text is not echoed back.
        log.info("Verification options for %s rejected (%s)", slug, type(exc).__name__)
        raise InvalidInputError(
            f"Run options must have names starting with {verification.OPTION_PREFIX!r}, "
            "use only letters, digits and underscores, and have values of at most "
            f"{verification.MAX_OPTION_LENGTH} characters."
        ) from exc
    org = db.get(Organization, payload.workspace_id)
    if org is None:
        raise NotFoundError("That workspace was not found")
    actor = current.user.email or current.user.name
    with _run_lock:
        if slug in _running:
            raise ConflictError(
                f"A check of {spec.name} is already running. Wait for it to finish."
            )
        _running[slug] = {"workspace_id": org.id, "by": actor}
    _errors.pop(slug, None)
    audit.record(
        db,
        tenant_id=current.tenant_id,
        actor=actor,
        actor_id=current.user.id,
        action=f"portal started a live check of {spec.name} with {org.name}'s connection",
        module="portal",
        ip_address=ip,
        context={
            "connector": slug,
            "workspace_id": org.id,
            "allow_writes": payload.allow_writes,
            "environment": payload.environment,
            # Which options, never their values.
            "options": sorted(options),
        },
    )
    db.commit()
    try:
        _executor().submit(
            _run_in_background,
            slug,
            org.id,
            payload.allow_writes,
            payload.environment,
            actor,
            options,
        )
    except RuntimeError:
        _running.pop(slug, None)
        raise
    return VerificationRunStarted(
        toast=Toast(
            message=f"Checking {spec.name} with {org.name}'s connection. The result appears here in a moment.",
            kind="info",
        )
    )


@router.put("/settings", response_model=VerificationSettingsOut)
def save_settings(
    payload: VerificationSettingsIn, current: PortalAdminDep, db: DbSession, ip: ClientIp
) -> VerificationSettingsOut:
    """Hide (or show again) unverified connectors in every workspace's catalogue."""
    actor = current.user.email or current.user.name
    try:
        verification.set_setting(
            db,
            key=verification.HIDE_UNVERIFIED_KEY,
            value=payload.hide_unverified_connectors,
            actor=actor,
        )
    except SQLAlchemyError as exc:
        db.rollback()
        raise InvalidInputError(
            "Saving needs the latest database update (migration 0018). Ask whoever runs "
            "the server to apply it, then try again."
        ) from exc
    state = "on" if payload.hide_unverified_connectors else "off"
    audit.record(
        db,
        tenant_id=current.tenant_id,
        actor=actor,
        actor_id=current.user.id,
        action=f"portal turned {state} hiding unverified connectors",
        module="portal",
        ip_address=ip,
        context={"hide_unverified_connectors": payload.hide_unverified_connectors},
    )
    db.commit()
    verification.invalidate_cache()
    return VerificationSettingsOut(
        hide_unverified_connectors=payload.hide_unverified_connectors,
        toast=Toast(
            message=(
                "Unverified connectors are now hidden from customers. Connections already "
                "made keep working."
                if payload.hide_unverified_connectors
                else "Unverified connectors are offered to customers again, labelled Beta."
            ),
            kind="success",
        ),
    )
