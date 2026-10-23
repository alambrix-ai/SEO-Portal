"""Retention, subject requests, signed audit exports, AI governance."""
from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core import crypto, netguard
from app.core import egress
from app.db.base import utcnow
from app.models.compliance import (
    ALLOWED_DATA_CLASSES,
    AiGovernancePolicy,
    AuditExportJob,
    RetentionPolicy,
    SubjectRequest,
)
from app.models.workspace import AuditLogEntry, Organization
from app.services import audit
from app.services.encryption import Ctx, OrgCipher


def upsert_retention(
    db: Session,
    org: Organization,
    *,
    data_class: str,
    ttl_days: int,
    enabled: bool = True,
    actor_email: str,
) -> RetentionPolicy:
    data_class = (data_class or "").strip()
    if data_class not in ALLOWED_DATA_CLASSES:
        raise ValueError(f"unsupported data_class: {data_class}")
    ttl_days = int(ttl_days)
    if ttl_days < 1 or ttl_days > 3650:
        raise ValueError("ttl_days out of range")
    row = db.execute(
        select(RetentionPolicy).where(
            RetentionPolicy.tenant_id == org.id,
            RetentionPolicy.data_class == data_class,
        )
    ).scalar_one_or_none()
    if row is None:
        row = RetentionPolicy(tenant_id=org.id, data_class=data_class)
        db.add(row)
    row.ttl_days = ttl_days
    row.enabled = bool(enabled)
    db.flush()
    audit.record(
        db,
        tenant_id=org.id,
        actor=actor_email,
        action="retention.upsert",
        module="admin",
        context={"data_class": data_class, "ttl_days": ttl_days},
    )
    return row


def open_subject_request(
    db: Session,
    org: Organization,
    cipher: OrgCipher,
    *,
    kind: str,
    email: str,
    requested_by_id: str | None,
    actor_email: str,
) -> SubjectRequest:
    kind = (kind or "").strip().lower()
    if kind not in {"export", "erasure"}:
        raise ValueError("kind must be export or erasure")
    index = crypto.email_index(crypto.normalize_email(email))
    row = SubjectRequest(
        tenant_id=org.id,
        kind=kind,
        subject_email_index=index,
        status="received",
        requested_at=utcnow(),
        requested_by_id=requested_by_id,
        evidence={"region": org.data_region},
    )
    db.add(row)
    db.flush()
    audit.record(
        db,
        tenant_id=org.id,
        actor=actor_email,
        action=f"subject_request.{kind}.open",
        module="admin",
        context={"request_id": row.id},
    )
    return row


def complete_subject_export(
    db: Session,
    org: Organization,
    cipher: OrgCipher,
    request: SubjectRequest,
    *,
    payload: dict,
    actor_email: str,
) -> SubjectRequest:
    if request.tenant_id != org.id or request.kind != "export":
        raise ValueError("invalid export request")
    # Payload must not include secrets; store encrypted.
    request.result_encrypted = cipher.encrypt_json(payload, context=Ctx.SUBJECT_EXPORT)
    request.status = "completed"
    request.completed_at = utcnow()
    db.flush()
    audit.record(
        db,
        tenant_id=org.id,
        actor=actor_email,
        action="subject_request.export.complete",
        module="admin",
        context={"request_id": request.id},
    )
    return request


def complete_subject_erasure(
    db: Session,
    org: Organization,
    request: SubjectRequest,
    *,
    actor_email: str,
    deactivated_user_ids: list[str],
) -> SubjectRequest:
    """Mark erasure complete after caller deactivates users (audit preserved)."""
    if request.tenant_id != org.id or request.kind != "erasure":
        raise ValueError("invalid erasure request")
    request.status = "completed"
    request.completed_at = utcnow()
    request.evidence = {
        **(request.evidence or {}),
        "deactivated_count": len(deactivated_user_ids),
        # Fingerprints only — never raw emails or user PII.
        "user_fps": [crypto.fingerprint(uid) for uid in deactivated_user_ids[:50]],
    }
    db.flush()
    audit.record(
        db,
        tenant_id=org.id,
        actor=actor_email,
        action="subject_request.erasure.complete",
        module="admin",
        context={"request_id": request.id, "deactivated_count": len(deactivated_user_ids)},
    )
    return request


def build_signed_audit_export(
    db: Session,
    org: Organization,
    cipher: OrgCipher,
    *,
    window_start: datetime,
    window_end: datetime,
    format: str = "json",
    siem_url: str = "",
    created_by_id: str | None,
    actor_email: str,
    signing_key: bytes | None = None,
) -> AuditExportJob:
    format = (format or "json").lower()
    if format not in {"json", "csv"}:
        raise ValueError("format must be json or csv")
    if siem_url:
        netguard.check_public_url(siem_url)
    rows = list(
        db.execute(
            select(AuditLogEntry)
            .where(
                AuditLogEntry.tenant_id == org.id,
                AuditLogEntry.at >= window_start,
                AuditLogEntry.at <= window_end,
            )
            .order_by(AuditLogEntry.at.asc())
            .limit(50_000)
        ).scalars()
    )
    body_obj = [
        {
            "at": r.at.isoformat() if r.at else "",
            "actor": r.actor,
            "action": r.action,
            "module": r.module,
            "context": r.context or {},
        }
        for r in rows
    ]
    raw = json.dumps(body_obj, separators=(",", ":"), sort_keys=True).encode("utf-8")
    key = signing_key or crypto.hash_token(org.id).encode("utf-8")
    signature = hmac.new(key, raw, hashlib.sha256).hexdigest()
    job = AuditExportJob(
        tenant_id=org.id,
        format=format,
        window_start=window_start,
        window_end=window_end,
        status="ready",
        signature=signature,
        row_count=len(rows),
        body_encrypted=cipher.encrypt(raw.decode("utf-8"), context=Ctx.AUDIT_EXPORT),
        siem_url=siem_url[:1000],
        created_by_id=created_by_id,
    )
    db.add(job)
    db.flush()
    if siem_url:
        _deliver_siem(siem_url, raw, signature)
        job.status = "shipped"
        db.flush()
    audit.record(
        db,
        tenant_id=org.id,
        actor=actor_email,
        action="audit.export",
        module="admin",
        context={"job_id": job.id, "rows": job.row_count, "shipped": bool(siem_url)},
    )
    return job


def upsert_ai_governance(
    db: Session,
    org: Organization,
    *,
    model_allowlist: list[str],
    agent_cost_caps: dict[str, int],
    log_prompts: bool = False,
    log_actions: bool = True,
    actor_email: str,
) -> AiGovernancePolicy:
    allow = [str(s)[:64] for s in (model_allowlist or [])[:50]]
    caps = {
        str(k)[:64]: max(0, min(int(v), 10_000_000))
        for k, v in (agent_cost_caps or {}).items()
    }
    row = db.execute(
        select(AiGovernancePolicy).where(AiGovernancePolicy.tenant_id == org.id)
    ).scalar_one_or_none()
    if row is None:
        row = AiGovernancePolicy(tenant_id=org.id)
        db.add(row)
    row.model_allowlist = allow
    row.agent_cost_caps = caps
    row.log_prompts = bool(log_prompts)
    row.log_actions = bool(log_actions)
    db.flush()
    audit.record(
        db,
        tenant_id=org.id,
        actor=actor_email,
        action="ai_governance.upsert",
        module="admin",
        context={"models": len(allow), "caps": len(caps)},
    )
    return row


def model_allowed(db: Session, tenant_id: str, model_slug: str) -> bool:
    row = db.execute(
        select(AiGovernancePolicy).where(AiGovernancePolicy.tenant_id == tenant_id)
    ).scalar_one_or_none()
    if row is None or not row.model_allowlist:
        return True
    return model_slug in row.model_allowlist


def _deliver_siem(url: str, body: bytes, signature: str) -> None:
    netguard.check_public_url(url)
    with egress.pinned_client(timeout=20.0) as client:
        egress.fetch(
            client,
            "POST",
            url,
            content=body,
            headers={
                "Content-Type": "application/json",
                "X-Synaprank-Signature": signature,
            },
            max_bytes=64_000,
        )


# Document residency limitation for operators.
RESIDENCY_LIMITATIONS = (
    "data_region on the organization pins intended residency. Cross-region "
    "failover and customer-managed KMS wrapping are configured per deployment; "
    "this control does not by itself move physical database shards."
)
