"""The approvals queue: storing agent proposals, and applying decisions.

When the runner queues an action it stores the action's ``payload`` encrypted
under the organisation's key. Approving it rebuilds the change from that
payload and applies it through the same ``APPLIERS`` table the agents' own
apply callables use — so an approved rewrite is byte-for-byte the rewrite the
agent proposed, days later, without re-running the agent or re-billing an LLM
call.
"""
from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from app.agents.base.contracts import AgentAction
from app.core.config import settings
from app.core.crypto import fingerprint
from app.core.exceptions import AppError, ConflictError, InvalidInputError, NotFoundError
from app.core.logging import get_logger, log_failure
from app.db.base import utcnow
from app.models.agent import AgentRecord
from app.models.approval import ApprovalItem, ApprovalStatus
from app.models.workspace import Organization, User
from app.services import audit, metrics
from app.services.encryption import Ctx, OrgCipher

log = get_logger(__name__)

# Registered appliers, keyed by ``target_kind``. Each agent module registers
# its own on import, which keeps the "what does approving this do?" logic next
# to the agent that proposed it.
Applier = Callable[[Session, Organization, ApprovalItem, dict], None]
APPLIERS: dict[str, Applier] = {}

#: Reserved payload key carrying the counters a change earns once applied.
#: Not shown to reviewers (the console hides keys starting with "_").
METRICS_KEY = "_metrics"
#: Reserved payload key: why a spending agent's change waits for a person
#: (an unverified connector, or over a workspace limit). Shown on the card.
WHY_KEY = "_why_waiting"
#: Reserved payload key on a practised item: what it would have done.
PRACTICE_KEY = "_practice"


def register_applier(target_kind: str) -> Callable[[Applier], Applier]:
    def decorator(fn: Applier) -> Applier:
        APPLIERS[target_kind] = fn
        return fn

    return decorator


# ── Relative time for the card's meta line ─────────────────────────────────
def humanize_age(delta: timedelta) -> str:
    seconds = int(delta.total_seconds())
    if seconds < 90:
        return "submitted just now"
    minutes = seconds // 60
    if minutes < 60:
        return f"submitted {minutes}m ago"
    hours = minutes // 60
    if hours < 24:
        return f"submitted {hours}h ago"
    days = hours // 24
    return f"submitted {days}d ago"


# ── Queueing ───────────────────────────────────────────────────────────────
def payload_digest(payload: dict) -> str:
    """A stable label for a proposal's content.

    Canonical JSON — sorted keys, no incidental whitespace — so two runs that
    propose the same thing produce the same digest regardless of dict ordering.
    The ciphertext cannot be used for this: AES-GCM draws a fresh nonce per
    write, so identical payloads never encrypt identically.
    """
    canonical = json.dumps(payload or {}, sort_keys=True, separators=(",", ":"), default=str)
    return fingerprint(canonical, length=32)


def find_open_proposal(
    db: Session, *, tenant_id: str, agent_slug: str, target_kind: str, target_id: str | None
) -> ApprovalItem | None:
    """The agent's existing pending proposal for this target, if any."""
    stmt = select(ApprovalItem).where(
        ApprovalItem.tenant_id == tenant_id,
        ApprovalItem.status == ApprovalStatus.PENDING.value,
        ApprovalItem.agent_slug == agent_slug,
        ApprovalItem.target_kind == target_kind,
    )
    # A target_id of None is a real case — a budget plan or an audience model
    # is about the workspace, not a row — and `== None` does not match in SQL.
    stmt = stmt.where(
        ApprovalItem.target_id.is_(None) if target_id is None
        else ApprovalItem.target_id == target_id
    )
    return db.execute(stmt.order_by(ApprovalItem.submitted_at.desc())).scalars().first()


def queue_action(
    db: Session,
    *,
    org: Organization,
    record: AgentRecord,
    action: AgentAction,
    cipher: OrgCipher,
    why: list[str] | None = None,
) -> tuple[ApprovalItem, bool]:
    """Store one proposed action for human review.

    ``why`` carries the safety holds that stopped it being applied
    automatically ("Moves 35% of budget; your limit is 20% per change."),
    stored with the payload so the reviewer sees them on the card.

    Returns ``(item, created)``. One agent gets **one** pending item per
    target, because that is what the queue is for: it holds the decision a
    person still has to make, not a log of every time an agent thought about
    it. An agent on an hourly schedule re-derives the same recommendation
    every hour, and without this each pass would add another card — the queue
    fills with dozens of copies of one decision, the badge count becomes
    meaningless, and approving one leaves the rest behind to be applied again.

    So:

    * same proposal as the one already pending — nothing happens, and
      crucially ``submitted_at`` is left alone, so "submitted 3h ago" keeps
      telling the truth about when the recommendation first appeared;
    * changed proposal — the pending item is updated in place. A reviewer
      should be deciding on the agent's current recommendation, not on one
      from yesterday that has already been superseded by better data.
    """
    now = utcnow()
    # The counters this change earns ride with it, and are recorded only if
    # it is approved and applied (see approve()).
    stored = {**action.payload, METRICS_KEY: action.metrics} if action.metrics else action.payload
    if why:
        stored = {**stored, WHY_KEY: list(why)}
    digest = payload_digest(stored)
    existing = find_open_proposal(
        db,
        tenant_id=org.id,
        agent_slug=record.slug,
        target_kind=action.target_kind,
        target_id=action.target_id,
    )

    from app.services import approval_scopes

    brand = (action.scope_brand or "").strip()[:80]
    region = (action.scope_region or "").strip()[:80]
    channel = (action.scope_channel or "").strip()[:80]
    if not (brand or region or channel):
        brand, region, channel = approval_scopes.scopes_from_action_payload(action.payload)

    if existing is not None:
        if existing.payload_digest == digest:
            log.debug(
                "%s already has this %s pending; not queueing a duplicate",
                record.slug,
                existing.type,
            )
            return existing, False

        existing.type = action.approval_type or action.kind
        existing.title = action.title
        existing.agent_name = record.name
        existing.payload_encrypted = cipher.encrypt_json(stored, context=Ctx.APPROVAL_PAYLOAD)
        existing.payload_digest = digest
        existing.submitted_at = now
        existing.meta = humanize_age(timedelta(0))
        existing.scope_brand = brand
        existing.scope_region = region
        existing.scope_channel = channel
        db.flush()
        log.info("Updated the pending %s from %s with a new proposal", existing.type, record.slug)
        return existing, False

    item = ApprovalItem(
        tenant_id=org.id,
        type=action.approval_type or action.kind,
        title=action.title,
        agent_slug=record.slug,
        agent_name=record.name,
        target_kind=action.target_kind,
        target_id=action.target_id,
        payload_encrypted=cipher.encrypt_json(stored, context=Ctx.APPROVAL_PAYLOAD),
        payload_digest=digest,
        meta=humanize_age(timedelta(0)),
        status=ApprovalStatus.PENDING.value,
        submitted_at=now,
        scope_brand=brand,
        scope_region=region,
        scope_channel=channel,
    )
    db.add(item)
    db.flush()
    log.info("Queued %s from %s for review", item.type, record.slug)
    return item, True


def queue_manual(
    db: Session,
    *,
    org: Organization,
    type_: str,
    title: str,
    agent_slug: str,
    agent_name: str,
    target_kind: str,
    target_id: str | None,
    payload: dict,
) -> ApprovalItem:
    """Queue an item a *person* initiated (e.g. "Approve rewrite" in the UI).

    The same queue serves both, so an operator asking for a change and an agent
    proposing one are reviewed in one place.
    """
    cipher = OrgCipher.for_org(org)
    now = utcnow()
    item = ApprovalItem(
        tenant_id=org.id,
        type=type_,
        title=title,
        agent_slug=agent_slug,
        agent_name=agent_name,
        target_kind=target_kind,
        target_id=target_id,
        payload_encrypted=cipher.encrypt_json(payload, context=Ctx.APPROVAL_PAYLOAD),
        meta=humanize_age(timedelta(0)),
        status=ApprovalStatus.PENDING.value,
        submitted_at=now,
    )
    db.add(item)
    db.flush()
    return item


# ── Reads ──────────────────────────────────────────────────────────────────
def list_pending(
    db: Session,
    *,
    tenant_id: str,
    limit: int = 100,
    viewer: User | None = None,
) -> list[ApprovalItem]:
    items = list(
        db.execute(
            select(ApprovalItem)
            .where(
                ApprovalItem.tenant_id == tenant_id,
                ApprovalItem.status == ApprovalStatus.PENDING.value,
            )
            .order_by(ApprovalItem.submitted_at.desc())
            .limit(limit * 4 if viewer is not None else limit)
        ).scalars()
    )
    if viewer is not None:
        from app.services import approval_scopes

        items = approval_scopes.filter_items(db, viewer, items)[:limit]
    now = utcnow()
    for item in items:
        # Recomputed on read so "2h ago" is true when it is displayed, not
        # when it was written. SQLite may strip tzinfo on round-trip.
        submitted = item.submitted_at
        if submitted.tzinfo is None:
            from datetime import timezone

            submitted = submitted.replace(tzinfo=timezone.utc)
        item.meta = humanize_age(now - submitted)
    return items


def list_practised(
    db: Session, *, tenant_id: str, limit: int = 100, agent_slug: str = "", days: int = 30
) -> list[ApprovalItem]:
    """What spending agents in practice mode would have done, newest first."""
    stmt = select(ApprovalItem).where(
        ApprovalItem.tenant_id == tenant_id,
        ApprovalItem.status == ApprovalStatus.PRACTISED.value,
        ApprovalItem.submitted_at >= utcnow() - timedelta(days=days),
    )
    if agent_slug:
        stmt = stmt.where(ApprovalItem.agent_slug == agent_slug)
    items = list(db.execute(stmt.order_by(ApprovalItem.submitted_at.desc()).limit(limit)).scalars())
    now = utcnow()
    for item in items:
        item.meta = humanize_age(now - item.submitted_at).replace("submitted", "practised")
    return items


def why_waiting(payload: dict | None) -> list[str]:
    """The safety holds stored with a queued change, for the reviewer."""
    raw = (payload or {}).get(WHY_KEY) if isinstance(payload, dict) else None
    return [str(line) for line in raw] if isinstance(raw, list) else []


def pending_count(db: Session, *, tenant_id: str, viewer: User | None = None) -> int:
    if viewer is None:
        return db.execute(
            select(func.count())
            .select_from(ApprovalItem)
            .where(
                ApprovalItem.tenant_id == tenant_id,
                ApprovalItem.status == ApprovalStatus.PENDING.value,
            )
        ).scalar_one()
    return len(list_pending(db, tenant_id=tenant_id, limit=500, viewer=viewer))


def get_item(db: Session, *, tenant_id: str, item_id: str) -> ApprovalItem:
    item = db.get(ApprovalItem, item_id)
    if item is None or item.tenant_id != tenant_id:
        raise NotFoundError("That item is no longer in the queue")
    return item


def read_payload(org: Organization, item: ApprovalItem) -> dict:
    cipher = OrgCipher.for_org(org)
    return cipher.decrypt_json(item.payload_encrypted, context=Ctx.APPROVAL_PAYLOAD, default={})


# ── Decisions ──────────────────────────────────────────────────────────────
def approve(
    db: Session,
    *,
    org: Organization,
    actor: User,
    item_id: str,
    note: str = "",
    ip_address: str | None = None,
) -> ApprovalItem:
    """Apply a queued change and mark it approved.

    If applying fails, the item stays pending with the error recorded — a
    failed publish must not silently disappear from someone's queue.
    """
    item = get_item(db, tenant_id=org.id, item_id=item_id)
    if item.status == ApprovalStatus.PRACTISED.value:
        raise ConflictError(
            "This was practised, not proposed: practice mode never sends changes. "
            "Switch practice mode off and the agent will propose it for real."
        )
    if item.status != ApprovalStatus.PENDING.value:
        raise ConflictError("That item has already been decided")

    from app.services import agent_safety, approval_scopes

    approval_scopes.assert_can_act(db, actor, item)

    halted = agent_safety.halt_reason(db, org)
    if halted:
        # A stop (the platform's or this workspace's) is for "something is
        # wrong with what we send"; an approval sends through the same
        # connectors, so it waits too.
        if halted == agent_safety.PLATFORM_STOP_REASON:
            raise ConflictError(
                "Emergency stop is on for the whole platform, so approved changes "
                "cannot be sent right now. It stays in your queue."
            )
        raise ConflictError(
            "All agents in this workspace are stopped, so approved changes cannot be "
            "sent right now. It stays in your queue. Press Resume on the banner first."
        )

    payload = read_payload(org, item)
    applier = APPLIERS.get(item.target_kind)

    if applier is None:
        log.error("No applier registered for target_kind %r", item.target_kind)
        item.apply_error = f"No handler for {item.target_kind!r}"
        db.flush()
        raise ConflictError(
            "This change cannot be applied automatically — its handler is missing"
        )

    try:
        applier(db, org, item, payload)
    except Exception as exc:  # noqa: BLE001 - reported, not swallowed
        log.exception("Applying approval %s failed", item.id)
        from app.core.user_messages import public_error_message

        public = public_error_message(
            exc, fallback="This change could not be applied. Try again later."
        )
        item.apply_error = public
        db.flush()
        raise ConflictError(public) from exc

    item.status = ApprovalStatus.APPROVED.value
    item.decided_at = utcnow()
    item.decided_by = actor.name
    item.decided_by_id = actor.id
    item.decision_note = note
    item.apply_error = ""
    db.flush()

    earned = payload.get(METRICS_KEY) if isinstance(payload, dict) else None
    metrics.accumulate(
        db,
        tenant_id=org.id,
        values={"actions_approved": 1, **(earned if isinstance(earned, dict) else {})},
    )
    audit.record_user_action(
        db,
        user=actor,
        action=f"approved {item.type.lower()}: {item.title}",
        module="approvals",
        ip_address=ip_address,
        context={"approval_id": item.id, "agent": item.agent_slug},
    )
    return item


def reject(
    db: Session,
    *,
    org: Organization,
    actor: User,
    item_id: str,
    note: str = "",
    ip_address: str | None = None,
) -> ApprovalItem:
    """Decline a queued change and revert the target to a settled state."""
    from app.services import approval_scopes

    item = get_item(db, tenant_id=org.id, item_id=item_id)
    if item.status != ApprovalStatus.PENDING.value:
        raise ConflictError("That item has already been decided")
    approval_scopes.assert_can_act(db, actor, item)

    rejecter = REJECTERS.get(item.target_kind)
    if rejecter is not None:
        try:
            rejecter(db, org, item, read_payload(org, item))
        except Exception:  # noqa: BLE001 - a rejection must still complete
            log.exception("Reverting rejected approval %s failed", item.id)

    item.status = ApprovalStatus.REJECTED.value
    item.decided_at = utcnow()
    item.decided_by = actor.name
    item.decided_by_id = actor.id
    item.decision_note = note
    db.flush()

    metrics.accumulate(db, tenant_id=org.id, values={"actions_rejected": 1})
    audit.record_user_action(
        db,
        user=actor,
        action=f"rejected {item.type.lower()}: {item.title}",
        module="approvals",
        ip_address=ip_address,
        context={"approval_id": item.id, "agent": item.agent_slug},
    )
    return item


# Optional revert handlers, registered the same way as appliers.
REJECTERS: dict[str, Applier] = {}


def register_rejecter(target_kind: str) -> Callable[[Applier], Applier]:
    def decorator(fn: Applier) -> Applier:
        REJECTERS[target_kind] = fn
        return fn

    return decorator


def expire_stale(db: Session, *, tenant_id: str, older_than_days: int = 30) -> int:
    """Close out items nobody has decided. Keeps the queue meaningful."""
    cutoff = utcnow() - timedelta(days=older_than_days)
    stale = db.execute(
        select(ApprovalItem).where(
            ApprovalItem.tenant_id == tenant_id,
            ApprovalItem.status == ApprovalStatus.PENDING.value,
            ApprovalItem.submitted_at < cutoff,
        )
    ).scalars()
    count = 0
    for item in stale:
        item.status = ApprovalStatus.EXPIRED.value
        item.decided_at = utcnow()
        item.decided_by = "System"
        item.decision_note = f"Expired after {older_than_days} days without a decision"
        count += 1
    if count:
        db.flush()
    return count


# ── Asking for changes and editing text ────────────────────────────────────
# A reviewer who does not want to approve or reject outright can ask for a
# revised version ("make the headline shorter") or correct the text directly.
# What either means depends on the target, so, like appliers, the handlers
# are registered per ``target_kind`` by the module that owns the target. The
# content library registers one dispatcher for its assets and routes by asset
# kind (see ``app.services.assets``); the creative agent plugs the actual
# regeneration in there.

#: Reserved payload key: how many times this piece of work has been revised.
REVISION_KEY = "_revision"
#: Reserved payload key: the approval a revised item replaces.
REVISES_KEY = "_revises"

#: Longest feedback a reviewer may send with "Ask for changes".
MAX_FEEDBACK_CHARS = 2000
#: Bounds on one "Edit text" request.
MAX_EDIT_FIELDS = 8
MAX_EDIT_FIELD_CHARS = 2000
EDIT_FIELD_NAME = re.compile(r"[a-z][a-z_]{0,39}")

_NO_REVISE = "Changes can't be requested for this kind of item. Approve or reject it instead."
_NO_EDIT = "The text of this kind of item can't be edited here. Approve or reject it instead."


@dataclass(frozen=True, slots=True)
class RevisionRequest:
    """What a reviser receives for one "Ask for changes".

    The feedback is untrusted reviewer text. It is handed over only in its
    encrypted form (``Ctx.CREATIVE_FEEDBACK``), so it is stored the way it
    arrives; whoever passes it to a model must decrypt it with
    :func:`read_feedback` and wrap it with ``llm.untrusted`` first.
    """

    approval_id: str
    feedback_encrypted: str
    feedback_chars: int
    #: The revision this request will produce (1 for the first).
    revision: int
    max_revisions: int
    actor_id: str


@dataclass(slots=True)
class RevisionOutcome:
    """What a reviser or editor reports back."""

    #: The pending item that now holds the work, when one exists already. A
    #: reviser that regenerates in the background leaves it ``None`` and
    #: queues the revised item later with :func:`queue_revision`.
    approval_id: str | None = None
    #: Plain-English toast; a default is used when empty.
    message: str = ""


@dataclass(frozen=True, slots=True)
class RevisionResult:
    """The outcome of a request, as the API returns it."""

    approval_id: str | None
    revision: int
    max_revisions: int
    message: str


@dataclass(frozen=True, slots=True)
class RevisionSupport:
    """What a reviewer may do with one pending item besides approve/reject."""

    revise: bool = False
    edit: bool = False
    #: The stored rendered preview, when the target has one.
    preview_file_id: str | None = None


Reviser = Callable[[Session, Organization, ApprovalItem, dict, RevisionRequest], RevisionOutcome]
Editor = Callable[[Session, Organization, ApprovalItem, dict, dict[str, str]], RevisionOutcome]
#: Batch check for a list of cards: ``(item, decrypted payload)`` pairs in,
#: support per item id out. Optional; without one, support is simply whether
#: a reviser or editor is registered for the target kind.
SupportProbe = Callable[
    [Session, Organization, list[tuple[ApprovalItem, dict]]], dict[str, RevisionSupport]
]

REVISERS: dict[str, Reviser] = {}
EDITORS: dict[str, Editor] = {}
SUPPORT_PROBES: dict[str, SupportProbe] = {}


def register_reviser(target_kind: str) -> Callable[[Reviser], Reviser]:
    def decorator(fn: Reviser) -> Reviser:
        REVISERS[target_kind] = fn
        return fn

    return decorator


def register_editor(target_kind: str) -> Callable[[Editor], Editor]:
    def decorator(fn: Editor) -> Editor:
        EDITORS[target_kind] = fn
        return fn

    return decorator


def register_support_probe(target_kind: str) -> Callable[[SupportProbe], SupportProbe]:
    def decorator(fn: SupportProbe) -> SupportProbe:
        SUPPORT_PROBES[target_kind] = fn
        return fn

    return decorator


def max_revisions() -> int:
    return settings.creative_max_revisions


def revision_of(payload: dict | None) -> int:
    """How many times the work in this payload has been revised (0 = original)."""
    raw = payload.get(REVISION_KEY) if isinstance(payload, dict) else None
    if isinstance(raw, int) and not isinstance(raw, bool) and raw >= 0:
        return raw
    return 0


def read_feedback(org: Organization, feedback_encrypted: str) -> str:
    """Decrypt reviewer feedback. Untrusted text: wrap it before any model sees it."""
    return OrgCipher.for_org(org).decrypt(feedback_encrypted, context=Ctx.CREATIVE_FEEDBACK)


def _lock_pending(db: Session, *, tenant_id: str, item_id: str) -> ApprovalItem:
    """The item, row-locked, and still pending.

    The lock stops two reviewers asking for changes to the same item at once
    and so spending two revisions on one decision.
    """
    item = db.execute(
        select(ApprovalItem)
        .where(ApprovalItem.id == item_id, ApprovalItem.tenant_id == tenant_id)
        .with_for_update()
    ).scalars().first()
    if item is None:
        raise NotFoundError("That item is no longer in the queue")
    if item.status != ApprovalStatus.PENDING.value:
        raise ConflictError("That item has already been decided")
    return item


def _call_hook(
    hook: Callable[..., RevisionOutcome], item: ApprovalItem, event: str, *args: object
) -> RevisionOutcome:
    """Run a reviser or editor. Authored errors pass through as they are;
    anything else is logged without its text and refused in plain English."""
    try:
        outcome = hook(*args)
    except AppError:
        raise
    except Exception as exc:
        log_failure(log, event, exc, approval_id=item.id, target_kind=item.target_kind)
        raise ConflictError("That didn't work this time. Try again in a minute.") from exc
    return outcome if isinstance(outcome, RevisionOutcome) else RevisionOutcome()


def request_changes(
    db: Session,
    *,
    org: Organization,
    actor: User,
    item_id: str,
    feedback: str,
    ip_address: str | None = None,
) -> RevisionResult:
    """Close a pending item as superseded and ask for a revised version.

    Bounded by ``creative_max_revisions`` per piece of work, counted across
    the chain of revisions (``REVISION_KEY``).
    """
    from app.services import approval_scopes

    text = (feedback or "").strip()
    if not text:
        raise InvalidInputError("Say what you would like changed.")
    if len(text) > MAX_FEEDBACK_CHARS:
        raise InvalidInputError(f"Keep your feedback under {MAX_FEEDBACK_CHARS} characters.")

    item = _lock_pending(db, tenant_id=org.id, item_id=item_id)
    approval_scopes.assert_can_act(db, actor, item)
    reviser = REVISERS.get(item.target_kind)
    if reviser is None:
        raise ConflictError(_NO_REVISE)

    payload = read_payload(org, item)
    used = revision_of(payload)
    limit = max_revisions()
    if used >= limit:
        raise ConflictError(
            f"This has already been revised {used} times, the most allowed. "
            "Approve it, reject it, or edit the text yourself.",
            details={"revision": used, "max_revisions": limit},
        )

    request = RevisionRequest(
        approval_id=item.id,
        feedback_encrypted=OrgCipher.for_org(org).encrypt(text, context=Ctx.CREATIVE_FEEDBACK),
        feedback_chars=len(text),
        revision=used + 1,
        max_revisions=limit,
        actor_id=actor.id,
    )
    outcome = _call_hook(
        reviser, item, "approval.request_changes_failed", db, org, item, payload, request
    )

    item.status = ApprovalStatus.SUPERSEDED.value
    item.decided_at = utcnow()
    item.decided_by = actor.name
    item.decided_by_id = actor.id
    # The feedback itself is not copied here: this column is not encrypted.
    item.decision_note = "Changes requested"
    db.flush()

    audit.record_user_action(
        db,
        user=actor,
        action=f"asked for changes to {item.type.lower()}: {item.title}",
        module="approvals",
        ip_address=ip_address,
        context={
            "approval_id": item.id,
            "agent": item.agent_slug,
            "revision": used + 1,
            "feedback_chars": len(text),
        },
    )
    return RevisionResult(
        approval_id=outcome.approval_id,
        revision=used + 1,
        max_revisions=limit,
        message=outcome.message or "Changes requested. The revised version will appear in your queue.",
    )


def _check_fields(fields: dict) -> dict[str, str]:
    if not isinstance(fields, dict) or not fields:
        raise InvalidInputError("Change at least one piece of text.")
    if len(fields) > MAX_EDIT_FIELDS:
        raise InvalidInputError("Too many fields in one edit.")
    clean: dict[str, str] = {}
    for name, value in fields.items():
        if not isinstance(name, str) or not EDIT_FIELD_NAME.fullmatch(name):
            raise InvalidInputError("That text can't be edited here.")
        if not isinstance(value, str) or len(value) > MAX_EDIT_FIELD_CHARS:
            raise InvalidInputError("That text is too long.")
        clean[name] = value
    return clean


def edit_text(
    db: Session,
    *,
    org: Organization,
    actor: User,
    item_id: str,
    fields: dict[str, str],
    ip_address: str | None = None,
) -> RevisionResult:
    """Correct the text of a pending item in place. No model is called.

    The editor validates the new text the same way generated text is
    validated; the item stays pending for a decision.
    """
    clean = _check_fields(fields)
    item = _lock_pending(db, tenant_id=org.id, item_id=item_id)
    editor = EDITORS.get(item.target_kind)
    if editor is None:
        raise ConflictError(_NO_EDIT)

    payload = read_payload(org, item)
    outcome = _call_hook(editor, item, "approval.edit_text_failed", db, org, item, payload, clean)
    db.flush()

    audit.record_user_action(
        db,
        user=actor,
        action=f"edited the text of {item.type.lower()}: {item.title}",
        module="approvals",
        ip_address=ip_address,
        context={"approval_id": item.id, "agent": item.agent_slug, "fields": sorted(clean)},
    )
    return RevisionResult(
        approval_id=outcome.approval_id or item.id,
        revision=revision_of(payload),
        max_revisions=max_revisions(),
        message=outcome.message or "Text updated",
    )


def queue_revision(
    db: Session,
    *,
    org: Organization,
    previous: ApprovalItem,
    title: str,
    payload: dict,
) -> ApprovalItem:
    """Queue the revised version of ``previous`` for review.

    Revisers call this (directly or from a background job) so the revision
    count follows the work: the new item carries ``previous``'s count plus
    one, and a link back to it.
    """
    if previous.tenant_id != org.id:
        raise NotFoundError("That item is no longer in the queue")
    stored = {
        **payload,
        REVISION_KEY: revision_of(read_payload(org, previous)) + 1,
        REVISES_KEY: previous.id,
    }
    return queue_manual(
        db,
        org=org,
        type_=previous.type,
        title=title,
        agent_slug=previous.agent_slug,
        agent_name=previous.agent_name,
        target_kind=previous.target_kind,
        target_id=previous.target_id,
        payload=stored,
    )


def revision_support(
    db: Session, *, org: Organization, entries: list[tuple[ApprovalItem, dict]]
) -> dict[str, RevisionSupport]:
    """Per pending item: may it be revised or edited, and its preview.

    Role checks are the caller's; this says what the item itself allows.
    """
    pending = [(i, p) for i, p in entries if i.status == ApprovalStatus.PENDING.value]
    by_kind: dict[str, list[tuple[ApprovalItem, dict]]] = {}
    for item, payload in pending:
        by_kind.setdefault(item.target_kind, []).append((item, payload))

    found: dict[str, RevisionSupport] = {}
    for kind, group in by_kind.items():
        probe = SUPPORT_PROBES.get(kind)
        if probe is None:
            plain = RevisionSupport(revise=kind in REVISERS, edit=kind in EDITORS)
            found.update({item.id: plain for item, _ in group})
            continue
        try:
            found.update(probe(db, org, group))
        except Exception as exc:  # noqa: BLE001 - logged; the cards still load without the extras
            log_failure(log, "approval.support_probe_failed", exc, target_kind=kind, count=len(group))

    limit = max_revisions()
    out: dict[str, RevisionSupport] = {}
    for item, payload in pending:
        support = found.get(item.id)
        if support is None:
            continue
        out[item.id] = RevisionSupport(
            revise=support.revise and revision_of(payload) < limit,
            edit=support.edit,
            preview_file_id=support.preview_file_id,
        )
    return out


# ── History: what left the queue, and why ──────────────────────────────────
#: The states an item leaves the queue with. Practised items are not listed:
#: nothing was ever proposed, and they have their own view.
DECIDED_STATUSES: tuple[str, ...] = (
    ApprovalStatus.APPROVED.value,
    ApprovalStatus.REJECTED.value,
    ApprovalStatus.SUPERSEDED.value,
    ApprovalStatus.EXPIRED.value,
)
#: The most history items one page may hold.
HISTORY_MAX_PAGE = 100
#: How many later items are read to find the revisions that replaced a page
#: of superseded items. Bounds the decryption work one request can cause.
REVISION_SCAN_LIMIT = 300

_BAD_HISTORY_CURSOR = "This page of the history is no longer available. Start again from the top."


@dataclass(frozen=True, slots=True)
class HistoryEntry:
    """One decided item and its place in a chain of revisions."""

    item: ApprovalItem
    #: How many times the work had been revised when this version was made.
    revision: int = 0
    #: The earlier version this one replaced, if it is a revision.
    revises_id: str | None = None
    #: The revised version that replaced this one (superseded items only),
    #: once it has been made.
    replaced_by_id: str | None = None
    replaced_by_status: str | None = None


@dataclass(frozen=True, slots=True)
class HistoryPage:
    entries: list[HistoryEntry]
    next_cursor: str | None


def _revises_of(payload: dict | None) -> str | None:
    raw = payload.get(REVISES_KEY) if isinstance(payload, dict) else None
    return raw if isinstance(raw, str) and raw else None


def _replacements(
    db: Session, *, org: Organization, superseded: list[ApprovalItem]
) -> dict[str, ApprovalItem]:
    """The revised item that replaced each superseded one, by the old item's id.

    A revision records the item it replaces inside its encrypted payload, so
    later items for the same targets are read (tenant-filtered, bounded by
    ``REVISION_SCAN_LIMIT``) and matched on that link.
    """
    if not superseded:
        return {}
    wanted = {item.id for item in superseded}
    kinds = sorted({item.target_kind for item in superseded})
    target_ids = sorted({item.target_id for item in superseded if item.target_id is not None})
    target_filters = []
    if target_ids:
        target_filters.append(ApprovalItem.target_id.in_(target_ids))
    if any(item.target_id is None for item in superseded):
        target_filters.append(ApprovalItem.target_id.is_(None))
    earliest = min(item.submitted_at for item in superseded)
    candidates = db.execute(
        select(ApprovalItem)
        .where(
            ApprovalItem.tenant_id == org.id,
            ApprovalItem.status != ApprovalStatus.PRACTISED.value,
            ApprovalItem.target_kind.in_(kinds),
            or_(*target_filters),
            ApprovalItem.submitted_at >= earliest,
        )
        .order_by(ApprovalItem.submitted_at.asc(), ApprovalItem.id.asc())
        .limit(REVISION_SCAN_LIMIT)
    ).scalars()
    found: dict[str, ApprovalItem] = {}
    for candidate in candidates:
        if candidate.id in wanted or candidate.tenant_id != org.id:
            continue
        previous = _revises_of(read_payload(org, candidate))
        if previous in wanted and previous not in found:
            found[previous] = candidate
    return found


def list_history(
    db: Session,
    *,
    org: Organization,
    status: str | None = None,
    type_: str | None = None,
    cursor: str | None = None,
    limit: int = 50,
) -> HistoryPage:
    """Decided items, newest decision first, keyset-paginated on (decided_at, id).

    Tenant-filtered. ``status`` narrows to one decided state and ``type_`` to
    one approval type; ``next_cursor`` is set only when more items remain.
    """
    if status is not None and status not in DECIDED_STATUSES:
        raise InvalidInputError("Choose approved, rejected, replaced or expired.")
    page_size = max(1, min(limit, HISTORY_MAX_PAGE))
    stmt = select(ApprovalItem).where(
        ApprovalItem.tenant_id == org.id,
        ApprovalItem.status.in_([status] if status else list(DECIDED_STATUSES)),
        ApprovalItem.decided_at.is_not(None),
    )
    if type_:
        stmt = stmt.where(ApprovalItem.type == type_)
    if cursor:
        try:
            after_at, after_id = audit.decode_cursor(cursor)
        except InvalidInputError:
            raise InvalidInputError(_BAD_HISTORY_CURSOR) from None
        stmt = stmt.where(
            or_(
                ApprovalItem.decided_at < after_at,
                and_(ApprovalItem.decided_at == after_at, ApprovalItem.id < after_id),
            )
        )
    stmt = stmt.order_by(ApprovalItem.decided_at.desc(), ApprovalItem.id.desc()).limit(page_size + 1)
    fetched = list(db.execute(stmt).scalars())
    # Belt and braces: the query is tenant-filtered and so is RLS.
    items = [item for item in fetched[:page_size] if item.tenant_id == org.id]
    more = len(fetched) > page_size

    payloads = {item.id: read_payload(org, item) for item in items}
    replaced = _replacements(
        db,
        org=org,
        superseded=[i for i in items if i.status == ApprovalStatus.SUPERSEDED.value],
    )
    entries = [
        HistoryEntry(
            item=item,
            revision=revision_of(payloads[item.id]),
            revises_id=_revises_of(payloads[item.id]),
            replaced_by_id=replaced[item.id].id if item.id in replaced else None,
            replaced_by_status=replaced[item.id].status if item.id in replaced else None,
        )
        for item in items
    ]
    last = items[-1] if items else None
    next_cursor = (
        audit.encode_cursor(last.decided_at, str(last.id))
        if more and last is not None and last.decided_at is not None
        else None
    )
    return HistoryPage(entries=entries, next_cursor=next_cursor)
