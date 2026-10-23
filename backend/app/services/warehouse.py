"""Warehouse export abstraction (BigQuery / Snowflake / S3) with a fake provider.

No real cloud credentials are required. Destinations are egress-allowlisted;
the fake provider records jobs in-process for tests and local/dev.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.exceptions import InvalidInputError
from app.db.base import utcnow
from app.models.warehouse import WarehouseExportJob
from app.models.workspace import Organization, User
from app.services import audit
from app.services.encryption import Ctx, OrgCipher

ALLOWED_PROVIDERS = frozenset({"fake", "bigquery", "snowflake", "s3"})
ALLOWED_DATASETS = frozenset({"metrics", "approvals", "audit_summary"})


@dataclass(frozen=True, slots=True)
class ExportResult:
    status: str
    row_count: int
    error_code: str = ""


class WarehouseProvider(ABC):
    name: str

    @abstractmethod
    def export_rows(
        self,
        *,
        destination: str,
        dataset: str,
        rows: list[dict],
        secret: str = "",
    ) -> ExportResult:
        raise NotImplementedError


class FakeWarehouseProvider(WarehouseProvider):
    """In-memory sink — no network, no credentials."""

    name = "fake"

    def __init__(self) -> None:
        self.exports: list[dict] = []

    def export_rows(
        self,
        *,
        destination: str,
        dataset: str,
        rows: list[dict],
        secret: str = "",
    ) -> ExportResult:
        _ = secret
        self.exports.append(
            {
                "destination": destination,
                "dataset": dataset,
                "row_count": len(rows),
            }
        )
        return ExportResult(status="succeeded", row_count=len(rows))


_FAKE = FakeWarehouseProvider()


def get_provider(name: str | None = None) -> WarehouseProvider:
    chosen = (name or "fake").strip().lower()
    if chosen == "fake":
        return _FAKE
    # Real providers are intentionally not wired without a business decision;
    # refuse rather than accept credentials.
    raise InvalidInputError(
        f"Warehouse provider {chosen!r} is not enabled. Use 'fake' until a "
        "certified destination is configured."
    )


def _destination_allowed(destination: str) -> bool:
    """Egress-style host check against IMAGE/SELF_HOSTED allow patterns.

    Fake destinations may be ``fake://…``. Real https destinations must use
    an allowlisted hostname from settings (never private/metadata).
    """
    text = (destination or "").strip()
    if not text:
        return False
    if text.startswith("fake://"):
        return True
    parts = urlsplit(text if "://" in text else f"https://{text}")
    if parts.scheme not in ("https",):
        return False
    host = (parts.hostname or "").lower()
    if not host:
        return False
    allowed = set(settings.image_fetch_allowed_hosts or ()) | set(
        settings.self_hosted_llm_allowed_hosts or ()
    )
    # Also accept explicit warehouse allow-list if present later.
    extra = getattr(settings, "warehouse_allowed_hosts", None) or ()
    allowed |= set(extra)
    if not allowed:
        # Fail closed when no hosts are configured for real destinations.
        return False
    return host in {h.lower() for h in allowed}


def _sample_rows(dataset: str, *, tenant_id: str) -> list[dict]:
    # Never include PII — aggregates / ids only.
    if dataset == "metrics":
        return [{"tenant_fp": tenant_id[:8], "metric": "actions_approved", "value": 0}]
    if dataset == "approvals":
        return [{"tenant_fp": tenant_id[:8], "pending": 0}]
    return [{"tenant_fp": tenant_id[:8], "events": 0}]


def enqueue_export(
    db: Session,
    org: Organization,
    cipher: OrgCipher,
    *,
    provider: str,
    dataset: str,
    destination: str,
    actor: User,
    secret: str = "",
) -> WarehouseExportJob:
    provider = (provider or "fake").strip().lower()
    dataset = (dataset or "metrics").strip().lower()
    destination = (destination or "").strip()[:512]
    if provider not in ALLOWED_PROVIDERS:
        raise InvalidInputError("Unknown warehouse provider.")
    if dataset not in ALLOWED_DATASETS:
        raise InvalidInputError("Unknown export dataset.")
    if not _destination_allowed(destination):
        raise InvalidInputError(
            "That destination is not on the warehouse egress allow-list."
        )
    row = WarehouseExportJob(
        tenant_id=org.id,
        provider=provider,
        dataset=dataset,
        destination=destination,
        status="queued",
        created_by_id=actor.id,
        secret_encrypted=cipher.encrypt(secret, context=Ctx.WAREHOUSE_SECRET) if secret else "",
    )
    db.add(row)
    db.flush()
    audit.record(
        db,
        tenant_id=org.id,
        actor=actor.email,
        action="warehouse.export.enqueue",
        module="admin",
        context={"provider": provider, "dataset": dataset},
    )
    return row


def run_export(
    db: Session, org: Organization, cipher: OrgCipher, job_id: str
) -> WarehouseExportJob:
    job = db.get(WarehouseExportJob, job_id)
    if job is None or job.tenant_id != org.id:
        raise LookupError("export job not found")
    if job.status not in ("queued", "failed"):
        return job
    job.status = "running"
    db.flush()
    try:
        provider = get_provider(job.provider)
        secret = (
            cipher.decrypt(job.secret_encrypted, context=Ctx.WAREHOUSE_SECRET)
            if job.secret_encrypted
            else ""
        )
        result = provider.export_rows(
            destination=job.destination,
            dataset=job.dataset,
            rows=_sample_rows(job.dataset, tenant_id=org.id),
            secret=secret,
        )
        job.status = result.status
        job.row_count = result.row_count
        job.error_code = result.error_code or ""
    except Exception:  # noqa: BLE001 - surface as job failure, no secret in logs
        job.status = "failed"
        job.error_code = "export_failed"
    job.finished_at = utcnow()
    db.flush()
    return job


def list_jobs(db: Session, *, tenant_id: str, limit: int = 50) -> list[WarehouseExportJob]:
    return list(
        db.execute(
            select(WarehouseExportJob)
            .where(WarehouseExportJob.tenant_id == tenant_id)
            .order_by(WarehouseExportJob.created_at.desc())
            .limit(limit)
        ).scalars()
    )
