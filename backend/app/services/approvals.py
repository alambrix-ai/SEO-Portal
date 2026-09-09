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
from collections.abc import Callable
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.agents.base.contracts import AgentAction
from app.core.crypto import fingerprint
from app.core.exceptions import ConflictError, NotFoundError
from app.core.logging import get_logger
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
) -> tuple[ApprovalItem, bool]:
    """Store one proposed action for human review.

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
    digest = payload_digest(action.payload)
    existing = find_open_proposal(
        db,
        tenant_id=org.id,
        agent_slug=record.slug,
        target_kind=action.target_kind,
        target_id=action.target_id,
    )

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
        existing.payload_encrypted = cipher.encrypt_json(
            action.payload, context=Ctx.APPROVAL_PAYLOAD
        )
        existing.payload_digest = digest
        existing.submitted_at = now
        existing.meta = humanize_age(timedelta(0))
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
        payload_encrypted=cipher.encrypt_json(action.payload, context=Ctx.APPROVAL_PAYLOAD),
        payload_digest=digest,
        meta=humanize_age(timedelta(0)),
        status=ApprovalStatus.PENDING.value,
        submitted_at=now,
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
def list_pending(db: Session, *, tenant_id: str, limit: int = 100) -> list[ApprovalItem]:
    items = list(
        db.execute(
            select(ApprovalItem)
            .where(
                ApprovalItem.tenant_id == tenant_id,
                ApprovalItem.status == ApprovalStatus.PENDING.value,
            )
            .order_by(ApprovalItem.submitted_at.desc())
            .limit(limit)
        ).scalars()
    )
    now = utcnow()
    for item in items:
        # Recomputed on read so "2h ago" is true when it is displayed, not
        # when it was written.
        item.meta = humanize_age(now - item.submitted_at)
    return items


def pending_count(db: Session, *, tenant_id: str) -> int:
    return db.execute(
        select(func.count())
        .select_from(ApprovalItem)
        .where(
            ApprovalItem.tenant_id == tenant_id,
            ApprovalItem.status == ApprovalStatus.PENDING.value,
        )
    ).scalar_one()


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
    if item.status != ApprovalStatus.PENDING.value:
        raise ConflictError("That item has already been decided")

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
        item.apply_error = f"{type(exc).__name__}: {exc}"
        db.flush()
        raise

    item.status = ApprovalStatus.APPROVED.value
    item.decided_at = utcnow()
    item.decided_by = actor.name
    item.decided_by_id = actor.id
    item.decision_note = note
    item.apply_error = ""
    db.flush()

    metrics.accumulate(db, tenant_id=org.id, values={"actions_approved": 1})
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
    item = get_item(db, tenant_id=org.id, item_id=item_id)
    if item.status != ApprovalStatus.PENDING.value:
        raise ConflictError("That item has already been decided")

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
