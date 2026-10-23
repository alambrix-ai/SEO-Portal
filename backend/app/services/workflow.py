"""Multi-step approvals, scoped API tokens, signed webhooks, ROI snapshots."""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core import crypto, egress, netguard
from app.db.base import utcnow
from app.models.approval import ApprovalItem
from app.models.workflow import (
    ApiToken,
    ApprovalStep,
    OutboundWebhook,
    RoiAttributionSnapshot,
    WebhookDeliveryLog,
)
from app.models.workspace import Organization
from app.services import audit
from app.services.encryption import Ctx, OrgCipher

ALLOWED_SCOPES = frozenset(
    {
        "approvals:read",
        "approvals:write",
        "metrics:read",
        "roi:read",
        "webhooks:manage",
    }
)
ALLOWED_WEBHOOK_EVENTS = frozenset(
    {
        "approval.submitted",
        "approval.decided",
        "roi.snapshot",
    }
)
#: Reject webhook deliveries whose signed timestamp is older/newer than this.
WEBHOOK_REPLAY_WINDOW_SECONDS = 300


def define_approval_chain(
    db: Session,
    org: Organization,
    item: ApprovalItem,
    *,
    steps: list[dict],
    actor_email: str,
) -> list[ApprovalStep]:
    """Attach ordered approval steps. Does not bypass the existing approval gate."""
    if item.tenant_id != org.id:
        raise ValueError("approval item tenant mismatch")
    if not steps:
        raise ValueError("at least one step required")
    if len(steps) > 20:
        raise ValueError("too many steps")
    existing = db.execute(
        select(ApprovalStep).where(
            ApprovalStep.tenant_id == org.id,
            ApprovalStep.approval_item_id == item.id,
        )
    ).scalars().all()
    if existing:
        raise ValueError("chain already defined")
    created: list[ApprovalStep] = []
    for i, spec in enumerate(steps):
        role = str(spec.get("required_role") or "").strip()[:32]
        email = str(spec.get("approver_email") or "").strip()
        sla = int(spec.get("sla_hours") or 24)
        if sla < 1 or sla > 720:
            raise ValueError("sla_hours out of range")
        row = ApprovalStep(
            tenant_id=org.id,
            approval_item_id=item.id,
            step_order=i + 1,
            required_role=role,
            approver_email_index=crypto.email_index(crypto.normalize_email(email)) if email else "",
            status="pending",
            sla_hours=sla,
        )
        db.add(row)
        created.append(row)
    db.flush()
    audit.record(
        db,
        tenant_id=org.id,
        actor=actor_email,
        action="approval.chain.define",
        module="admin",
        context={"approval_item_id": item.id, "steps": len(created)},
    )
    return created


def decide_step(
    db: Session,
    org: Organization,
    step: ApprovalStep,
    *,
    decision: str,
    actor_user_id: str,
    actor_email: str,
    actor_role: str,
    notes: str = "",
) -> ApprovalStep:
    decision = (decision or "").strip().lower()
    if decision not in {"approved", "rejected"}:
        raise ValueError("decision must be approved or rejected")
    if step.tenant_id != org.id or step.status != "pending":
        raise ValueError("step not actionable")
    # Enforce order: all prior steps must be approved.
    prior = db.execute(
        select(ApprovalStep).where(
            ApprovalStep.tenant_id == org.id,
            ApprovalStep.approval_item_id == step.approval_item_id,
            ApprovalStep.step_order < step.step_order,
        )
    ).scalars().all()
    if any(p.status != "approved" for p in prior):
        raise ValueError("prior steps incomplete")
    if step.required_role and step.required_role != actor_role:
        raise ValueError("role not authorised for this step")
    if step.approver_email_index:
        if crypto.email_index(crypto.normalize_email(actor_email)) != step.approver_email_index:
            raise ValueError("approver mismatch")
    step.status = decision
    step.decided_at = utcnow()
    step.decided_by_id = actor_user_id
    step.notes = (notes or "")[:500]
    db.flush()
    audit.record(
        db,
        tenant_id=org.id,
        actor=actor_email,
        action=f"approval.step.{decision}",
        module="admin",
        context={"step_id": step.id, "order": step.step_order},
    )
    return step


@dataclass(frozen=True, slots=True)
class MintedApiToken:
    row: ApiToken
    raw_token: str


def mint_api_token(
    db: Session,
    org: Organization,
    cipher: OrgCipher,
    *,
    label: str,
    scopes: list[str],
    created_by_id: str | None,
    actor_email: str,
    ttl_days: int | None = 90,
) -> MintedApiToken:
    label = (label or "").strip()[:120] or "API token"
    clean_scopes = sorted({s.strip() for s in scopes if s and s.strip()})
    if not clean_scopes or not set(clean_scopes) <= ALLOWED_SCOPES:
        raise ValueError("invalid scopes")
    raw = f"srk_{crypto.new_url_token(32)}"
    expires = None
    if ttl_days is not None:
        ttl_days = int(ttl_days)
        if ttl_days < 1 or ttl_days > 730:
            raise ValueError("ttl_days out of range")
        expires = utcnow() + timedelta(days=ttl_days)
    row = ApiToken(
        tenant_id=org.id,
        label=label,
        token_hash=crypto.hash_token(raw),
        token_prefix=raw[:12],
        scopes=clean_scopes,
        meta_encrypted=cipher.encrypt_json({"label": label}, context=Ctx.API_TOKEN_META),
        expires_at=expires,
        created_by_id=created_by_id,
    )
    db.add(row)
    db.flush()
    audit.record(
        db,
        tenant_id=org.id,
        actor=actor_email,
        action="api_token.mint",
        module="admin",
        context={"prefix": row.token_prefix, "scopes": clean_scopes},
    )
    return MintedApiToken(row=row, raw_token=raw)


def revoke_api_token(
    db: Session, org: Organization, token_id: str, *, actor_email: str
) -> ApiToken:
    row = db.get(ApiToken, token_id)
    if row is None or row.tenant_id != org.id:
        raise LookupError("API token not found")
    row.revoked_at = utcnow()
    db.flush()
    audit.record(
        db,
        tenant_id=org.id,
        actor=actor_email,
        action="api_token.revoke",
        module="admin",
        context={"token_id": row.id},
    )
    return row


def authenticate_api_token(
    db: Session, raw_token: str, *, required_scope: str
) -> tuple[Organization, ApiToken] | None:
    if not raw_token or not raw_token.startswith("srk_"):
        return None
    digest = crypto.hash_token(raw_token)
    row = db.execute(select(ApiToken).where(ApiToken.token_hash == digest)).scalar_one_or_none()
    if row is None or row.revoked_at is not None:
        return None
    if row.expires_at is not None and row.expires_at < utcnow():
        return None
    if required_scope not in (row.scopes or []):
        return None
    org = db.get(Organization, row.tenant_id)
    if org is None or not org.is_active:
        return None
    row.last_used_at = utcnow()
    db.flush()
    return org, row


def register_webhook(
    db: Session,
    org: Organization,
    cipher: OrgCipher,
    *,
    name: str,
    url: str,
    events: list[str],
    secret: str,
    created_by_id: str | None,
    actor_email: str,
) -> OutboundWebhook:
    name = (name or "").strip()[:120]
    if not name:
        raise ValueError("name required")
    checked = netguard.check_public_url(url)
    clean_events = sorted({e.strip() for e in events if e and e.strip()})
    if not clean_events or not set(clean_events) <= ALLOWED_WEBHOOK_EVENTS:
        raise ValueError("invalid events")
    secret = (secret or "").strip()
    if len(secret) < 16:
        raise ValueError("secret too short")
    row = OutboundWebhook(
        tenant_id=org.id,
        name=name,
        url=checked.strip()[:1000],
        secret_encrypted=cipher.encrypt(secret, context=Ctx.WEBHOOK_SECRET),
        events=clean_events,
        enabled=True,
        created_by_id=created_by_id,
    )
    db.add(row)
    db.flush()
    audit.record(
        db,
        tenant_id=org.id,
        actor=actor_email,
        action="webhook.register",
        module="admin",
        context={"webhook_id": row.id, "events": clean_events},
    )
    return row


def sign_webhook_payload(
    secret: str, *, body: bytes, timestamp: int, nonce: str
) -> str:
    """HMAC-SHA256 over ``timestamp.nonce.body`` — consumers must check replay window."""
    mac = hmac.new(
        secret.encode(),
        f"{timestamp}.{nonce}.".encode() + body,
        hashlib.sha256,
    ).hexdigest()
    return mac


def deliver_webhook(
    db: Session,
    org: Organization,
    cipher: OrgCipher,
    hook: OutboundWebhook,
    *,
    event: str,
    payload: dict,
    now: datetime | None = None,
) -> WebhookDeliveryLog:
    if hook.tenant_id != org.id or not hook.enabled:
        raise ValueError("webhook inactive")
    if event not in (hook.events or []):
        raise ValueError("event not subscribed")
    secret = cipher.decrypt(hook.secret_encrypted, context=Ctx.WEBHOOK_SECRET)
    ts_dt = now or utcnow()
    timestamp = int(ts_dt.timestamp())
    nonce = secrets.token_hex(16)
    # Replay protection: refuse reuse of nonce within tenant.
    clash = db.execute(
        select(WebhookDeliveryLog).where(
            WebhookDeliveryLog.tenant_id == org.id,
            WebhookDeliveryLog.nonce == nonce,
        )
    ).scalar_one_or_none()
    if clash is not None:
        raise ValueError("nonce collision")
    body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    signature = sign_webhook_payload(secret, body=body, timestamp=timestamp, nonce=nonce)
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "Synaprank/1.0 (+webhooks)",
        "X-Synaprank-Timestamp": str(timestamp),
        "X-Synaprank-Nonce": nonce,
        "X-Synaprank-Signature": signature,
        "X-Synaprank-Event": event,
    }
    # Soft clock skew check for callers verifying our signature.
    if abs(int(time.time()) - timestamp) > WEBHOOK_REPLAY_WINDOW_SECONDS:
        raise ValueError("timestamp outside replay window")
    status_code = 0
    success = False
    checked = netguard.check_public_url(hook.url)
    try:
        with egress.pinned_client(timeout=5.0, headers={"User-Agent": headers["User-Agent"]}) as client:
            resp = egress.fetch(
                client,
                "POST",
                checked.strip(),
                content=body,
                headers=headers,
                require_https=True,
                max_bytes=65_536,
                truncate=True,
            )
            status_code = int(getattr(resp, "status_code", 0) or 0)
            success = 200 <= status_code < 300
    except Exception:
        success = False
    log_row = WebhookDeliveryLog(
        tenant_id=org.id,
        webhook_id=hook.id,
        event=event,
        nonce=nonce,
        signed_at=ts_dt,
        status_code=status_code,
        success=success,
    )
    db.add(log_row)
    db.flush()
    audit.record(
        db,
        tenant_id=org.id,
        actor="system",
        action="webhook.deliver",
        module="admin",
        context={"webhook_id": hook.id, "event": event, "success": success},
    )
    return log_row


def record_roi_snapshot(
    db: Session,
    org: Organization,
    *,
    period_start: datetime,
    period_end: datetime,
    channels: dict,
    created_by_id: str | None,
    actor_email: str,
    notes: str = "",
) -> RoiAttributionSnapshot:
    if period_end <= period_start:
        raise ValueError("invalid period")
    clean: dict[str, dict[str, str]] = {}
    total_spend = Decimal("0")
    total_revenue = Decimal("0")
    for channel, metrics in (channels or {}).items():
        ch = str(channel).strip()[:64]
        if not ch or not isinstance(metrics, dict):
            continue
        try:
            spend = Decimal(str(metrics.get("spend", "0")))
            revenue = Decimal(str(metrics.get("revenue", "0")))
            conversions = Decimal(str(metrics.get("conversions", "0")))
        except (InvalidOperation, TypeError) as exc:
            raise ValueError(f"invalid metrics for {ch}") from exc
        if spend < 0 or revenue < 0 or conversions < 0:
            raise ValueError("metrics must be non-negative")
        clean[ch] = {
            "spend": format(spend, "f"),
            "revenue": format(revenue, "f"),
            "conversions": format(conversions, "f"),
        }
        total_spend += spend
        total_revenue += revenue
    blended = "0"
    if total_spend > 0:
        blended = format(total_revenue / total_spend, ".4f")
    row = RoiAttributionSnapshot(
        tenant_id=org.id,
        period_start=period_start,
        period_end=period_end,
        channels=clean,
        blended_roas=blended,
        notes=(notes or "")[:500],
        created_by_id=created_by_id,
    )
    db.add(row)
    db.flush()
    audit.record(
        db,
        tenant_id=org.id,
        actor=actor_email,
        action="roi.snapshot.record",
        module="reports",
        context={"snapshot_id": row.id, "blended_roas": blended},
    )
    return row
