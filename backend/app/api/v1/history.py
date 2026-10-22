"""Version history: numbered versions, what changed, compare any two, restore settings.

* ``GET  /history/retention``                      how long each kind is kept (admin view)
* ``PUT  /history/retention``                      change it (admin write)
* ``GET  /history/{kind}?subject=&cursor=&limit=`` newest first, each with the change since the one before
* ``GET  /history/{kind}/compare?a=&b=``           two versions: new, better, worse, with counts
* ``GET  /history/{kind}/{entry_id}``              one version, compared with the one before it
* ``POST /history/brand_profile/{entry_id}/restore``  make a brand profile version current again
* ``POST /history/{kind}/{entry_id}/restore``      the same for the other restorable kinds

Reading a kind needs view access to its module (``history.KINDS``: analyses
the SEO or Off-Page module, brand kits and products ``brand_kit``, agent
settings ``agents``, workspace policy ``admin``, brand profile and creative
text ``content``); restoring needs write access to it. Every query is
filtered by the caller's workspace, and a version of another workspace is
reported as not found.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query
from pydantic import BaseModel, ConfigDict, Field

from app.api.deps import CurrentUserDep, DbSession
from app.core.rbac import Module
from app.models.history import SnapshotKind
from app.services import audit, brand_profile, history
from app.services.history import brand, store

router = APIRouter(prefix="/history", tags=["history"])

#: Subject ids are entity UUIDs or ``site-<hash>``; nothing else reaches a query.
_SUBJECT_PATTERN = r"^[A-Za-z0-9._:-]{1,64}$"
#: A version id (UUID) or a local ranking check id (digits).
_ENTRY_PATTERN = r"^[A-Za-z0-9-]{1,64}$"
#: Most kinds one retention update may change.
_MAX_RETENTION_KINDS = 32


class RestoreIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: Required to restore a workspace policy that publishes automatically.
    confirm: bool = False


class KindRetentionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: None goes back to the workspace default.
    keep_versions: int | None = Field(default=None, ge=1, le=store.MAX_KEEP_VERSIONS)
    #: 0 keeps versions for ever; None goes back to the default.
    keep_days: int | None = Field(default=None, ge=0, le=store.MAX_KEEP_DAYS)


class RetentionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kinds: dict[str, KindRetentionIn] = Field(max_length=_MAX_RETENTION_KINDS)


# ── Retention (declared before /{kind} so "retention" is not read as a kind) ─
@router.get("/retention")
def read_retention(current: CurrentUserDep, db: DbSession) -> dict[str, Any]:
    current.require_view(Module.ADMIN)
    store.plan_cap(db, current.organization)
    return history.retention_view(current.organization)


@router.put("/retention")
def update_retention(payload: RetentionIn, current: CurrentUserDep, db: DbSession) -> dict[str, Any]:
    current.require_write(Module.ADMIN)
    store.plan_cap(db, current.organization)
    changes = {kind: values.model_dump() for kind, values in payload.kinds.items()}
    out = history.set_retention(db, current.organization, changes, user=current.user)
    db.commit()
    return out


# ── Reading ─────────────────────────────────────────────────────────────────
@router.get("/{kind}")
def list_history(
    kind: str,
    current: CurrentUserDep,
    db: DbSession,
    limit: int = Query(default=60, ge=1, le=100),
    subject: str | None = Query(default=None, pattern=_SUBJECT_PATTERN),
    cursor: str | None = Query(default=None, min_length=1, max_length=store.CURSOR_MAX_LENGTH),
) -> dict[str, Any]:
    current.require_view(history.module_for(kind))
    org = current.organization
    cap = store.plan_cap(db, org)
    shown = limit if cap is None else max(1, min(limit, cap))
    if cap is not None:
        # The plan shows only the newest versions; there is no next page.
        cursor = None
    baseline = False
    if kind != history.LOCAL_RANKS and cursor is None:
        # First visit: keep the current state as the starting point.
        baseline = bool(store.guarded(db, f"{kind} baseline", history.ensure_baseline, db, org, kind))
        if baseline:
            db.commit()
    out = history.history(db, org, kind, shown, subject=subject, cursor=cursor)
    if cap is not None:
        out["next_cursor"] = None
    out["baseline_recorded"] = baseline
    out["limit"] = shown
    return out


@router.get("/{kind}/compare")
def compare_versions(
    kind: str,
    current: CurrentUserDep,
    db: DbSession,
    a: str = Query(pattern=_ENTRY_PATTERN),
    b: str = Query(pattern=_ENTRY_PATTERN),
) -> dict[str, Any]:
    current.require_view(history.module_for(kind))
    store.plan_cap(db, current.organization)
    return history.compare(db, current.organization, kind, a, b)


@router.get("/{kind}/{entry_id}")
def read_history_entry(kind: str, entry_id: str, current: CurrentUserDep, db: DbSession) -> dict[str, Any]:
    current.require_view(history.module_for(kind))
    store.visible_limit(db, current.organization, 1)
    return history.detail(db, current.organization, kind, entry_id[:64])


# ── Restoring ───────────────────────────────────────────────────────────────
@router.post("/brand_profile/{entry_id}/restore")
def restore_brand_profile(entry_id: str, current: CurrentUserDep, db: DbSession) -> dict[str, Any]:
    current.require_write(Module.CONTENT)
    org = current.organization
    store.visible_limit(db, org, 1)
    row = store.get(db, org.id, SnapshotKind.BRAND_PROFILE, entry_id)
    profile = brand.restore(db, org, row, user=current.user)
    audit.record_user_action(
        db,
        user=current.user,
        action=f"restored version {row.version} of the brand profile (from {brand._when(row.created_at)})",
        module="content",
        context={"kind": SnapshotKind.BRAND_PROFILE, "restored_version": row.version, "snapshot_id": row.id},
    )
    db.commit()
    from app.core.exceptions import ConflictError

    try:
        brand_profile.workspace_llm(db, org)
        can_generate = True
    except ConflictError:
        can_generate = False
    return {
        "profile": profile,
        "can_generate": can_generate,
        "suggested_url": profile.get("source_url", ""),
        "timezone": brand_profile.timezone_of(org),
    }


@router.post("/{kind}/{entry_id}/restore")
def restore_version(
    kind: str,
    entry_id: str,
    current: CurrentUserDep,
    db: DbSession,
    payload: RestoreIn | None = None,
) -> dict[str, Any]:
    current.require_write(history.module_for(kind))
    org = current.organization
    store.plan_cap(db, org)
    out = history.restore(
        db, org, kind, entry_id[:64], user=current.user, confirm=bool(payload and payload.confirm)
    )
    db.commit()
    return out
