"""Version history: numbered versions of analyses and settings, compared in plain words.

Kinds (``KINDS`` below says who may read each and whether it can be restored):

* Read-only analyses, a version after each run:
  ``technical_seo`` (Technical SEO Auditor), ``seo_scores`` (On-Page SEO
  Sync), ``backlink_scan`` (Backlink Node Discovery), ``competitor_scan``
  (Competitor Link Monitor), ``site_analysis`` (recorded by the site analysis
  service) and ``local_ranks`` (read from ``local_rank_checks``, already dated).
* Restorable settings and content, a version on each save:
  ``brand_profile`` (module ``brand``), ``brand_kit``, ``product``,
  ``agent_config``, ``workspace_policy`` and ``creative_text`` (module
  ``config``). Restoring writes the old version back through the owning
  service's applier (:func:`register_restorer`) and records a *new* version;
  history is never rewritten.

Owning services record their versions with :func:`record_version`. Any two
versions of a subject compare into one structure (``store`` "Comparison
shape"): what is new, what got better, what got worse, with counts.

History starts when this feature was deployed. So that a workspace with work
already done does not open an empty list, the first request for an analysis
kind with no versions records a baseline of the current state, labelled as such.
"""
from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.exceptions import ConflictError, InvalidInputError, NotFoundError
from app.core.logging import get_logger
from app.core.rbac import Module
from app.models.history import AuditSnapshot, SnapshotActor, SnapshotKind, SnapshotSource
from app.models.workspace import Organization
from app.services.history import config, offpage, seo_scores, site_analysis, store, technical

log = get_logger(__name__)

LOCAL_RANKS = "local_ranks"
#: ``AuditSnapshot.source`` for a version recorded by :func:`record_version`.
SAVED = "saved"


@dataclass(frozen=True, slots=True)
class KindSpec:
    kind: str
    #: Reading needs view access to this module; restoring needs write access.
    module: Module
    restorable: bool = False
    #: Each version belongs to one item (a kit, product, agent or website).
    has_subjects: bool = False
    #: The label of a version recorded by :func:`record_version`.
    saved_label: str = "Saved"


KINDS: dict[str, KindSpec] = {
    spec.kind: spec
    for spec in (
        # Analyses. Local business agents write into the SEO module
        # (rbac.AGENT_CATEGORY_MODULE); backlink work is Off-Page.
        KindSpec(SnapshotKind.TECHNICAL_SEO, Module.SEO),
        KindSpec(SnapshotKind.SEO_SCORES, Module.SEO),
        KindSpec(LOCAL_RANKS, Module.SEO),
        KindSpec(SnapshotKind.SITE_ANALYSIS, Module.SEO, has_subjects=True, saved_label="Site analysis"),
        KindSpec(SnapshotKind.BACKLINK_SCAN, Module.OFFPAGE),
        KindSpec(SnapshotKind.COMPETITOR_SCAN, Module.OFFPAGE),
        # Settings and content.
        KindSpec(SnapshotKind.BRAND_PROFILE, Module.CONTENT, restorable=True),
        KindSpec(SnapshotKind.BRAND_KIT, Module.BRAND_KIT, restorable=True, has_subjects=True, saved_label="Brand kit saved"),
        KindSpec(SnapshotKind.PRODUCT, Module.BRAND_KIT, restorable=True, has_subjects=True, saved_label="Product saved"),
        KindSpec(SnapshotKind.AGENT_CONFIG, Module.AGENTS, restorable=True, has_subjects=True, saved_label="Agent settings saved"),
        KindSpec(SnapshotKind.WORKSPACE_POLICY, Module.ADMIN, restorable=True, saved_label="Workspace policy changed"),
        KindSpec(SnapshotKind.CREATIVE_TEXT, Module.CONTENT, restorable=True, has_subjects=True, saved_label="Text edited"),
    )
}
#: Who may read each kind.
KIND_MODULE: dict[str, Module] = {kind: spec.module for kind, spec in KINDS.items()}

#: Kinds backed by snapshots built from the workspace's tables after an agent run.
SNAPSHOT_KINDS: dict[str, Any] = {
    SnapshotKind.TECHNICAL_SEO: technical,
    SnapshotKind.SEO_SCORES: seo_scores,
    SnapshotKind.BACKLINK_SCAN: offpage.BacklinkScan,
    SnapshotKind.COMPETITOR_SCAN: offpage.CompetitorScan,
}
#: Agent slug -> the kind recorded at the end of its successful runs.
AGENT_KINDS: dict[str, str] = {
    "technical_seo_auditor": SnapshotKind.TECHNICAL_SEO,
    "on_page_seo_sync": SnapshotKind.SEO_SCORES,
    "backlink_node_discovery": SnapshotKind.BACKLINK_SCAN,
    "competitor_link_monitor": SnapshotKind.COMPETITOR_SCAN,
}
#: Settings-like kinds whose document is kept encrypted and compared field by field.
CONFIG_KINDS = frozenset(config.FIELDS)
#: Kinds an owning service records through :func:`record_version`.
RECORDABLE_KINDS = CONFIG_KINDS | {SnapshotKind.SITE_ANALYSIS}
#: Kinds kept in ``audit_snapshots``, so retention applies to them.
RETAINED_KINDS: tuple[str, ...] = tuple(k for k in KINDS if k != LOCAL_RANKS)

_SUBJECT = re.compile(r"^[A-Za-z0-9._:-]{0,64}$")
_SLUG = re.compile(r"^[a-z0-9_]{1,64}$")

#: ``fn(db, org, subject_id, document, *, user) -> the document now in force, or None``.
Restorer = Callable[..., dict[str, Any] | None]
RESTORERS: dict[str, Restorer] = {}


def register_restorer(kind: str) -> Callable[[Restorer], Restorer]:
    """Register how the owning service writes a version of ``kind`` back.

    The applier MUST load the subject through a tenant-filtered query (404 if
    it is not this workspace's), re-validate the document with the service's
    own input schema (a stored version may predate today's rules), apply it,
    and flush. It does not commit and does not record a version; the history
    service does both.
    """
    if kind not in KINDS or not KINDS[kind].restorable:
        raise ValueError(f"{kind!r} is not a restorable history kind")

    def decorate(fn: Restorer) -> Restorer:
        RESTORERS[kind] = fn
        return fn

    return decorate


def spec_for(kind: str) -> KindSpec:
    if kind not in KINDS:
        raise NotFoundError("There is no history of that kind")
    return KINDS[kind]


def module_for(kind: str) -> Module:
    return spec_for(kind).module


def run_label(run: Any) -> str:  # noqa: ANN401 - an AgentRun
    trigger = getattr(run, "trigger", "") or ""
    return {"schedule": "Scheduled run", "manual": "Run now"}.get(trigger, "Agent run")


def _cipher(org: Organization) -> Any:  # noqa: ANN401 - an OrgCipher
    from app.services.encryption import OrgCipher

    return OrgCipher.for_org(org)


def _document(org: Organization, row: AuditSnapshot, cipher: Any = None) -> dict[str, Any]:  # noqa: ANN401
    """The customer content of one version, decrypted (``items`` for the brand profile)."""
    if row.kind == SnapshotKind.BRAND_PROFILE:
        return dict(row.items or {})
    if not row.payload_encrypted:
        return {}
    from app.services.encryption import Ctx

    value = (cipher or _cipher(org)).decrypt_json(row.payload_encrypted, context=Ctx.HISTORY_PAYLOAD, default={})
    return value if isinstance(value, dict) else {}


# ── Recording ───────────────────────────────────────────────────────────────
def record_after_run(db: Session, org: Organization, slug: str, run: Any) -> Any:  # noqa: ANN401
    """Snapshot the audit an agent just finished. Callers wrap this in ``store.guarded``."""
    kind = AGENT_KINDS.get(slug)
    if kind is None:
        return None
    builder = SNAPSHOT_KINDS[kind]
    metrics, items, total = builder.build(db, org.id, run)
    return store.record(
        db,
        tenant_id=org.id,
        kind=kind,
        source=SnapshotSource.AGENT_RUN,
        run_id=getattr(run, "id", "") or "",
        label=run_label(run),
        created_by_name=getattr(run, "triggered_by", "") or "",
        metrics=metrics,
        items=items,
        item_count=total,
        actor_type=SnapshotActor.AGENT,
        agent_slug=slug,
        model=getattr(run, "llm_model", "") or "",
    )


def ensure_baseline(db: Session, org: Organization, kind: str) -> bool:
    """Record the current state once, if this kind has no history yet and there is data."""
    if kind == SnapshotKind.BRAND_PROFILE:
        from app.services.history import brand

        return brand.ensure_baseline(db, org)
    builder = SNAPSHOT_KINDS.get(kind)
    if builder is None or store.count(db, org.id, kind) or not builder.has_data(db, org.id):
        return False
    metrics, items, total = builder.build(db, org.id, None)
    store.record(
        db,
        tenant_id=org.id,
        kind=kind,
        source=SnapshotSource.BASELINE,
        label=store.BASELINE_LABEL,
        metrics=metrics,
        items=items,
        item_count=total,
        actor_type=SnapshotActor.SYSTEM,
    )
    return True


def _snapshot_for(kind: str, payload: Any) -> store.Snapshot:  # noqa: ANN401 - caller's document
    if kind == SnapshotKind.SITE_ANALYSIS:
        return site_analysis.snapshot(payload)
    return config.snapshot(kind, payload)


def _same_document(kind: str, a: dict[str, Any], b: dict[str, Any]) -> bool:
    return kind in CONFIG_KINDS and config.same(kind, a, b)


def _write_version(
    db: Session,
    org: Organization,
    kind: str,
    subject_id: str,
    payload: Any,  # noqa: ANN401 - caller's document
    *,
    actor: Any,  # noqa: ANN401 - a User or None
    agent_slug: str | None,
    model: str | None,
    summary: str | None,
    source: str,
    force: bool,
) -> AuditSnapshot | None:
    if kind not in RECORDABLE_KINDS:
        raise ValueError(f"{kind!r} versions are not recorded through record_version")
    spec = KINDS[kind]
    subject_id = subject_id or ""
    if not _SUBJECT.match(subject_id) or (spec.has_subjects and not subject_id) or (not spec.has_subjects and subject_id):
        raise ValueError(f"Invalid subject id for history kind {kind!r}")
    if agent_slug is not None and agent_slug != "" and not _SLUG.match(agent_slug):
        raise ValueError("Invalid agent slug for a history version")
    snap = _snapshot_for(kind, payload)
    cipher = _cipher(org)
    if not force and kind in CONFIG_KINDS:
        latest = store.latest_of(db, org.id, kind, subject_id)
        if latest is not None and _same_document(kind, _document(org, latest, cipher), snap.secret or {}):
            return None
    from app.services.encryption import Ctx

    if actor is not None:
        actor_type = SnapshotActor.USER
    elif agent_slug:
        actor_type = SnapshotActor.AGENT
    else:
        actor_type = SnapshotActor.SYSTEM
    label = " ".join(str(summary or spec.saved_label).split())[:200]
    return store.record(
        db,
        tenant_id=org.id,
        kind=kind,
        subject_id=subject_id,
        source=source,
        label=label,
        created_by=getattr(actor, "id", "") or "",
        created_by_name=getattr(actor, "name", "") or "",
        actor_type=actor_type,
        agent_slug=agent_slug or "",
        model=model or "",
        inputs=snap.inputs,
        metrics=snap.metrics,
        items=snap.items,
        item_count=snap.item_count,
        payload_encrypted=cipher.encrypt_json(snap.secret, context=Ctx.HISTORY_PAYLOAD) if snap.secret else "",
    )


def record_version(
    db: Session,
    org: Organization,
    kind: str,
    subject_id: str,
    payload: dict[str, Any],
    *,
    actor: Any,  # noqa: ANN401 - the User who saved, or None for an agent or the system
    agent_slug: str | None = None,
    model: str | None = None,
    summary: str | None = None,
) -> AuditSnapshot | None:
    """Keep ``payload`` as the next version of one subject; the hook for owning services.

    ``kind`` is one of :data:`RECORDABLE_KINDS`. ``subject_id`` is the item's
    id (kit, product, agent record id, or ``site_analysis.subject_for(url)``),
    and "" for ``workspace_policy``. ``payload`` is the whole document as the
    owning service would restore it (``site_analysis``: ``{"analysis_id",
    "website_url", "business_url", "report"}``; ``workspace_policy``:
    ``{"guardrail", "autonomous"}``). It is stored encrypted; only structural
    fields are copied in the clear. ``summary`` is the short label shown in
    the list ("Brand kit saved" by default).

    A settings save identical to the latest version records nothing and
    returns None. Writes an audit entry and applies retention. Flushes only:
    the caller commits, and should wrap this in ``store.guarded`` so a history
    failure never fails the save itself.
    """
    return _write_version(
        db, org, kind, subject_id, payload,
        actor=actor, agent_slug=agent_slug, model=model, summary=summary, source=SAVED, force=False,
    )


# ── Reading: entries ────────────────────────────────────────────────────────
def _entry(kind: str, row: Any, previous: Any) -> dict[str, Any]:  # noqa: ANN401
    builder = SNAPSHOT_KINDS[kind]
    now = builder.headline(row.metrics or {})
    before = builder.headline(previous.metrics or {}) if previous is not None else None
    return {
        **store.base_entry(row),
        "headline": now,
        "change": store.change(now, before),
        "metrics": builder.entry_metrics(row.metrics or {}),
    }


def _brand_entry(row: AuditSnapshot, previous: AuditSnapshot | None, current: dict[str, Any] | None) -> dict[str, Any]:
    from app.services.history import brand

    return {
        **store.base_entry(row),
        "headline": None,
        "change": None,
        "metrics": row.metrics,
        "changed_fields": brand.changed_labels(row.items, previous.items) if previous else None,
        "is_current": brand.same(row.items, current),
    }


def _site_entry(row: AuditSnapshot, previous: AuditSnapshot | None) -> dict[str, Any]:
    now = site_analysis.headline(row.metrics or {})
    before = site_analysis.headline(previous.metrics or {}) if previous is not None else None
    return {
        **store.base_entry(row),
        "headline": now,
        "change": store.change(now, before),
        "metrics": site_analysis.entry_metrics(row.metrics or {}),
    }


def _config_entry(
    org: Organization,
    row: AuditSnapshot,
    previous: AuditSnapshot | None,
    *,
    is_current: bool,
    cipher: Any,  # noqa: ANN401
) -> dict[str, Any]:
    now = _document(org, row, cipher)
    return {
        **store.base_entry(row),
        "headline": None,
        "change": None,
        "metrics": row.metrics,
        "changed_fields": (
            config.changed_labels(row.kind, now, _document(org, previous, cipher)) if previous is not None else None
        ),
        "is_current": is_current,
    }


def _previous_in(rows: list[AuditSnapshot], index: int, db: Session) -> AuditSnapshot | None:
    """The previous version of ``rows[index]``'s subject: from the page when it is there."""
    row = rows[index]
    for later in rows[index + 1:]:
        if later.subject_id == row.subject_id:
            return later
    return store.previous_of(db, row)


def history(
    db: Session,
    org: Organization,
    kind: str,
    limit: int,
    *,
    subject: str | None = None,
    cursor: str | None = None,
) -> dict[str, Any]:
    """One page of versions, newest first, each with what changed since the one before."""
    if kind == LOCAL_RANKS:
        return {"kind": kind, **_local_history(db, org.id, limit, cursor)}
    spec = spec_for(kind)
    subject_id: str | None = (subject or None) if spec.has_subjects else ""
    after = store.decode_cursor(cursor) if cursor else None
    fetched = store.page(db, org.id, kind, subject_id=subject_id, after=after, limit=limit + 1)
    rows = fetched[:limit]
    entries: list[dict[str, Any]] = []
    if kind == SnapshotKind.BRAND_PROFILE:
        from app.services import brand_profile

        current = brand_profile.get(org)
        entries = [_brand_entry(row, _previous_in(fetched, i, db), current) for i, row in enumerate(rows)]
    elif kind in SNAPSHOT_KINDS:
        entries = [_entry(kind, row, _previous_in(fetched, i, db)) for i, row in enumerate(rows)]
    elif kind == SnapshotKind.SITE_ANALYSIS:
        entries = [_site_entry(row, _previous_in(fetched, i, db)) for i, row in enumerate(rows)]
    else:
        cipher = _cipher(org)
        newest: dict[str, str] = {}
        for row in rows:
            if row.subject_id not in newest:
                latest = store.latest_of(db, org.id, kind, row.subject_id)
                newest[row.subject_id] = latest.id if latest is not None else ""
        entries = [
            _config_entry(
                org, row, _previous_in(fetched, i, db), is_current=newest.get(row.subject_id) == row.id, cipher=cipher
            )
            for i, row in enumerate(rows)
        ]
    first = store.first_at(db, org.id, kind)
    return {
        "kind": kind,
        "subject_id": subject_id,
        "restorable": spec.restorable,
        "entries": entries,
        "next_cursor": store.encode_cursor(rows[-1]) if len(fetched) > limit and rows else None,
        "started_at": first.isoformat() if first else None,
    }


# ── Local rankings adapter (no snapshots; numbered by check) ────────────────
def _local_history(db: Session, tenant_id: str, limit: int, cursor: str | None) -> dict[str, Any]:
    from app.models.local import LocalRankCheck
    from app.services.history import local_ranks

    before = local_ranks.moment_of(cursor) if cursor else None
    checks = local_ranks._checks(db, tenant_id, limit + 1, before=before)  # noqa: SLF001 - same package
    shown = checks[:limit]
    entries = [
        local_ranks._entry(moment, rows, checks[i + 1][1] if i + 1 < len(checks) else None)  # noqa: SLF001
        for i, (moment, rows) in enumerate(shown)
    ]
    if shown:
        # Check N is the Nth distinct check time; numbering needs one count.
        newest = int(
            db.scalar(
                select(func.count(func.distinct(LocalRankCheck.checked_at))).where(
                    LocalRankCheck.tenant_id == tenant_id, LocalRankCheck.checked_at <= shown[0][0]
                )
            )
            or 0
        )
        for i, entry in enumerate(entries):
            entry["version"] = max(newest - i, 1)
            entry["subject_id"] = ""
    return {
        "entries": entries,
        "trends": local_ranks.trends(shown),
        "started_at": None,
        "baseline": False,
        "restorable": False,
        "next_cursor": local_ranks.check_id(shown[-1][0]) if len(checks) > limit and shown else None,
    }


def _local_rows(db: Session, tenant_id: str, check: str) -> list[Any]:
    from app.models.local import LocalRankCheck
    from app.services.history import local_ranks

    moment = local_ranks.moment_of(check)
    rows = list(
        db.scalars(
            select(LocalRankCheck).where(
                LocalRankCheck.tenant_id == tenant_id,
                LocalRankCheck.checked_at >= moment,
                LocalRankCheck.checked_at < moment + timedelta(microseconds=1),
            )
        )
    )
    if not rows:
        raise NotFoundError("That check was not found")
    return rows


def _position(value: int | None) -> str:
    return "Not in the results" if value is None else f"Position {value}"


def local_changes(before_rows: list[Any], after_rows: list[Any]) -> dict[str, Any]:
    """Two local ranking checks, earlier then later, in the shared comparison shape."""
    from app.services.history import local_ranks

    diff = local_ranks.compare(after_rows, before_rows)
    better = [
        store.change_row(r["phrase"], before=_position(r["before"]), after=_position(r["after"]))
        for r in diff["moved_up"] + diff["entered_results"]
    ]
    worse = [
        store.change_row(r["phrase"], before=_position(r["before"]), after=_position(r["after"]))
        for r in diff["moved_down"] + diff["dropped_out"]
    ]
    positions = {r.phrase: r.position for r in after_rows}
    new = [store.change_row(p, before="Not tracked", after=_position(positions.get(p))) for p in diff["new_phrases"]]
    now, earlier = local_ranks.summarise(after_rows), local_ranks.summarise(before_rows)
    return store.comparison(
        noun=("search phrase", "search phrases"),
        new=new,
        fixed=better,
        worse=worse,
        unchanged=len(diff["unchanged"]),
        fixed_word="moved up",
        measures=[
            store.measure("In the top 3", earlier["in_pack"], now["in_pack"], better="higher"),
            store.measure("Found in the results", earlier["found_in"], now["found_in"], better="higher"),
            store.measure("Average position", earlier["average_position"], now["average_position"], better="lower"),
        ],
    )


# ── Comparing ───────────────────────────────────────────────────────────────
_SEVERITY_WORDS = {"high": "High priority", "medium": "Medium priority", "low": "Low priority"}


def technical_changes(
    before_metrics: dict[str, Any], before_items: dict[str, Any], after_metrics: dict[str, Any], after_items: dict[str, Any]
) -> dict[str, Any]:
    """Technical SEO issues: newly found, fixed, more severe; earlier then later."""
    rank = {s: i for i, s in enumerate(technical.SEVERITIES)}

    def state(items: dict[str, Any], key: str) -> list[Any]:
        return (list(items.get(key) or []) + [""] * 5)[:5]

    def row(key: str, items: dict[str, Any], *, before: str | None = None, after: str | None = None) -> dict[str, Any]:
        _status, _severity, kind, url, _target = state(items, key)
        return store.change_row(technical.label_for(str(kind)), where=url or None, before=before, after=after)

    now_open = {k for k, v in after_items.items() if v and v[0] == "open"}
    before_open = {k for k, v in before_items.items() if v and v[0] == "open"}
    gone = before_open - now_open
    fixed_keys = sorted(k for k in gone if state(after_items, k)[0] != "ignored")
    new_keys = sorted(now_open - before_open, key=lambda k: (rank.get(state(after_items, k)[1], 9), k))
    worse_keys, same = [], 0
    for key in sorted(now_open & before_open):
        a, b = state(after_items, key)[1], state(before_items, key)[1]
        if rank.get(a, 9) < rank.get(b, 9):
            worse_keys.append(key)
        else:
            same += 1
    sev = _SEVERITY_WORDS
    return store.comparison(
        noun=("issue", "issues"),
        new=[row(k, after_items, after=sev.get(state(after_items, k)[1])) for k in new_keys],
        fixed=[row(k, before_items, before=sev.get(state(before_items, k)[1]), after="Fixed") for k in fixed_keys],
        worse=[
            row(k, after_items, before=sev.get(state(before_items, k)[1]), after=sev.get(state(after_items, k)[1]))
            for k in worse_keys
        ],
        unchanged=same,
        measures=[
            store.measure("Open issues", before_metrics.get("open"), after_metrics.get("open"), better="lower"),
            store.measure(
                "High-priority issues",
                (before_metrics.get("by_severity") or {}).get("high"),
                (after_metrics.get("by_severity") or {}).get("high"),
                better="lower",
            ),
        ],
    )


def score_changes(
    before_metrics: dict[str, Any], before_items: dict[str, Any], after_metrics: dict[str, Any], after_items: dict[str, Any]
) -> dict[str, Any]:
    """SEO & AEO page gap scores (lower is better); earlier then later."""

    def gap(items: dict[str, Any], url: str) -> int:
        return int((list(items.get(url) or []) + [0])[0] or 0)

    def title(items: dict[str, Any], url: str) -> str:
        return str((list(items.get(url) or []) + [0, ""])[1] or url)

    def words(value: int) -> str:
        return f"Gap {value} of 100"

    improved, worsened, same = [], [], 0
    for url in sorted(set(after_items) & set(before_items)):
        a, b = gap(after_items, url), gap(before_items, url)
        if a == b:
            same += 1
        else:
            (improved if a < b else worsened).append(
                store.change_row(title(after_items, url), where=url, before=words(b), after=words(a))
            )
    added = [
        store.change_row(title(after_items, u), where=u, before="Not scored", after=words(gap(after_items, u)))
        for u in sorted(set(after_items) - set(before_items))
    ]
    return store.comparison(
        noun=("page", "pages"),
        new=added,
        fixed=improved,
        worse=worsened,
        unchanged=same,
        fixed_word="improved",
        measures=[
            store.measure("Average gap", before_metrics.get("average_gap"), after_metrics.get("average_gap"), better="lower"),
            store.measure("Pages scored", before_metrics.get("pages_scored"), after_metrics.get("pages_scored"), better="higher"),
        ],
    )


def _truncated(*rows: AuditSnapshot) -> bool:
    return any((row.item_count or 0) > len(row.items or {}) for row in rows)


def changes_between(
    org: Organization, kind: str, before: AuditSnapshot, after: AuditSnapshot, *, cipher: Any = None  # noqa: ANN401
) -> dict[str, Any]:
    """Two stored versions of one kind, earlier then later, in the shared comparison shape."""
    bm, bi, am, ai = before.metrics or {}, before.items or {}, after.metrics or {}, after.items or {}
    if kind == SnapshotKind.TECHNICAL_SEO:
        out = technical_changes(bm, bi, am, ai)
    elif kind == SnapshotKind.SEO_SCORES:
        out = score_changes(bm, bi, am, ai)
    elif kind in (SnapshotKind.BACKLINK_SCAN, SnapshotKind.COMPETITOR_SCAN):
        out = SNAPSHOT_KINDS[kind].compare(am, ai, bm, bi)
    elif kind == SnapshotKind.SITE_ANALYSIS:
        out = site_analysis.compare(bm, bi, am, ai)
    elif kind == SnapshotKind.BRAND_PROFILE or kind in CONFIG_KINDS:
        cipher = cipher or (_cipher(org) if kind in CONFIG_KINDS else None)
        out = config.compare(kind, _document(org, before, cipher), _document(org, after, cipher))
    else:
        raise NotFoundError("There is no history of that kind")
    if _truncated(before, after):
        out["truncated"] = True
    return out


def compare(db: Session, org: Organization, kind: str, a: str, b: str) -> dict[str, Any]:
    """Any two versions of one subject, from the earlier to the later."""
    if a == b:
        raise InvalidInputError("Choose two different versions to compare.")
    if kind == LOCAL_RANKS:
        from app.services.history import local_ranks

        rows_a, rows_b = _local_rows(db, org.id, a), _local_rows(db, org.id, b)
        earlier, later = (rows_a, rows_b) if rows_a[0].checked_at <= rows_b[0].checked_at else (rows_b, rows_a)

        def ref(rows: list[Any]) -> dict[str, Any]:
            moment = rows[0].checked_at
            return {"id": local_ranks.check_id(moment), "version": None, "created_at": moment.isoformat(),
                    "label": "Local Search Tracker run"}

        return {"kind": kind, "subject_id": "", "before": ref(earlier), "after": ref(later), **local_changes(earlier, later)}
    spec_for(kind)
    row_a, row_b = store.get(db, org.id, kind, a), store.get(db, org.id, kind, b)
    if row_a.subject_id != row_b.subject_id:
        raise InvalidInputError("Choose two versions of the same item to compare.")
    earlier, later = (row_a, row_b) if row_a.version <= row_b.version else (row_b, row_a)
    return {
        "kind": kind,
        "subject_id": row_a.subject_id,
        "before": store.version_ref(earlier),
        "after": store.version_ref(later),
        **changes_between(org, kind, earlier, later),
    }


# ── Reading: one version ────────────────────────────────────────────────────
def detail(db: Session, org: Organization, kind: str, entry_id: str) -> dict[str, Any]:
    if kind == LOCAL_RANKS:
        from app.services.history import local_ranks

        return {"kind": kind, **local_ranks.detail(db, org.id, entry_id)}
    spec = spec_for(kind)
    row = store.get(db, org.id, kind, entry_id)
    previous = store.previous_of(db, row)
    changes = (
        {"previous": store.version_ref(previous), **changes_between(org, kind, previous, row)}
        if previous is not None
        else None
    )
    if kind == SnapshotKind.BRAND_PROFILE:
        from app.services.history import brand

        return {"kind": kind, **brand.detail(db, org, row), "restorable": True, "changes": changes}
    if kind in SNAPSHOT_KINDS:
        builder = SNAPSHOT_KINDS[kind]
        return {
            "kind": kind,
            **_entry(kind, row, previous),
            "all_metrics": row.metrics,
            "restorable": False,
            "changes": changes,
            "comparison": (
                {
                    "previous_id": previous.id,
                    "previous_at": previous.created_at.isoformat() if previous.created_at else None,
                    "previous_label": store.source_label(previous),
                    "truncated": _truncated(previous, row),
                    **builder.compare(row.metrics or {}, row.items or {}, previous.metrics or {}, previous.items or {}),
                }
                if previous is not None
                else None
            ),
        }
    if kind == SnapshotKind.SITE_ANALYSIS:
        document = _document(org, row)
        return {
            "kind": kind,
            **_site_entry(row, previous),
            "website": document.get("website_url") or None,
            "restorable": False,
            "changes": changes,
        }
    cipher = _cipher(org)
    latest = store.latest_of(db, org.id, kind, row.subject_id)
    return {
        "kind": kind,
        **_config_entry(org, row, previous, is_current=latest is not None and latest.id == row.id, cipher=cipher),
        "fields": config.fields_view(kind, _document(org, row, cipher)),
        "restorable": spec.restorable,
        "changes": changes,
    }


# ── Restoring ───────────────────────────────────────────────────────────────
def restore(
    db: Session, org: Organization, kind: str, entry_id: str, *, user: Any, confirm: bool = False  # noqa: ANN401
) -> dict[str, Any]:
    """Make an old version current again, recorded as a new version. The caller commits.

    Analyses are records of what was found and cannot be restored (409).
    Restoring the workspace policy to "Publish automatically" needs
    ``confirm`` (422 otherwise).
    """
    from app.services import audit

    spec = spec_for(kind)
    if not spec.restorable:
        raise ConflictError("This is a record of an analysis, so it can't be restored. You can compare it with other versions.")
    row = store.get(db, org.id, kind, entry_id)
    name = store.kind_name(kind)
    if kind == SnapshotKind.BRAND_PROFILE:
        from app.services.history import brand

        brand.restore(db, org, row, user=user)
        new_row = store.latest_of(db, org.id, kind, "")
    else:
        cipher = _cipher(org)
        document = _document(org, row, cipher)
        latest = store.latest_of(db, org.id, kind, row.subject_id)
        if latest is not None and (latest.id == row.id or config.same(kind, _document(org, latest, cipher), document)):
            raise ConflictError("This version is already the current one.")
        if (
            kind == SnapshotKind.WORKSPACE_POLICY
            and not confirm
            and config.needs_confirmation(config.current_policy(db, org), document)
        ):
            raise InvalidInputError(
                config.CONFIRM_PUBLISH_AUTOMATICALLY,
                details={"needs_confirmation": True, "setting": config.PUBLISH_AUTOMATICALLY},
            )
        applier = RESTORERS.get(kind)
        if applier is None:
            raise ConflictError(f"Restoring the {name} isn't available yet.")
        applied = applier(db, org, row.subject_id, dict(document), user=user)
        db.flush()
        new_row = _write_version(
            db, org, kind, row.subject_id, applied if isinstance(applied, dict) else document,
            actor=user, agent_slug=None, model=None,
            summary=f"Restored version {row.version}", source=SnapshotSource.RESTORED, force=True,
        )
    audit.record_user_action(
        db,
        user=user,
        action=f"restored version {row.version} of the {name}",
        module=spec.module.value,
        context={
            "kind": kind,
            "subject_id": row.subject_id,
            "restored_version": row.version,
            "new_version": getattr(new_row, "version", None),
            "snapshot_id": row.id,
        },
    )
    # Imported here: brand_kits imports this module to register its restorers.
    from app.services import brand_kits

    # Plain-English notes a restorer left (e.g. photos that no longer exist).
    info = getattr(db, "info", None)
    notes = info.pop(brand_kits.RESTORE_NOTES_KEY, []) if isinstance(info, dict) else []
    return {
        "kind": kind,
        "subject_id": row.subject_id,
        "restored_version": row.version,
        "entry": store.base_entry(new_row) if new_row is not None else None,
        "notes": [str(n) for n in notes],
    }


# ── Retention settings ──────────────────────────────────────────────────────
def retention_view(org: Organization) -> dict[str, Any]:
    from app.core.config import settings

    kinds = []
    for kind in RETAINED_KINDS:
        policy = store.retention_for(org.settings, kind)
        kinds.append({
            "kind": kind,
            "name": store.kind_name(kind).capitalize(),
            "keep_versions": policy.keep_versions,
            "keep_days": policy.keep_days,
            "custom": policy.custom,
        })
    return {
        "min_versions_kept": settings.history_min_versions_kept,
        "defaults": {"keep_versions": settings.history_keep_versions_default, "keep_days": settings.history_keep_days_default},
        "limits": {"keep_versions": store.MAX_KEEP_VERSIONS, "keep_days": store.MAX_KEEP_DAYS},
        "kinds": kinds,
    }


def set_retention(
    db: Session, org: Organization, changes: dict[str, dict[str, int | None]], *, user: Any  # noqa: ANN401
) -> dict[str, Any]:
    """Store per-kind overrides; None for a value goes back to the default. The caller commits.

    Takes effect the next time a version of that kind is recorded.
    """
    from app.core.config import settings
    from app.services import audit

    minimum = settings.history_min_versions_kept
    stored = dict(((org.settings or {}).get(store.RETENTION_SETTING) or {}))
    for kind, values in changes.items():
        if kind not in RETAINED_KINDS:
            raise InvalidInputError("There is no history of that kind.")
        versions, days = values.get("keep_versions"), values.get("keep_days")
        if versions is not None and versions < minimum:
            raise InvalidInputError(f"Keep at least {minimum} versions.", details={"kind": kind})
        entry = {k: v for k, v in (("keep_versions", versions), ("keep_days", days)) if v is not None}
        if entry:
            stored[kind] = entry
        else:
            stored.pop(kind, None)
    org.settings = {**(org.settings or {}), store.RETENTION_SETTING: stored}
    db.flush()
    audit.record_user_action(
        db,
        user=user,
        action="changed how long version history is kept",
        module=Module.ADMIN.value,
        context={"kinds": sorted(changes)},
    )
    return retention_view(org)
