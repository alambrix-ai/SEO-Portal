"""Delete what the platform no longer needs to keep.

Two kinds of housekeeping, both run once a day:

* :func:`prune_expired` — sign-in codes, refresh tokens and website-analysis
  requests past their retention period (``retention_*_days``). These tables
  are not tenant-scoped (they are used before a session exists), so this runs
  once, unpinned. A code or token is pruned only once it has been *expired*
  for the whole period, so nothing that could still be redeemed is touched.
* :func:`prune_orphan_uploads` — uploaded logos, product photos and
  backgrounds that nothing uses any more, after ``ORPHAN_UPLOAD_DAYS``. Run
  per workspace, under that workspace's pin, with an explicit tenant filter.
  Orphans otherwise count toward the storage cap for ever. Renders, previews
  and anything a job produced are never candidates.

Deletes are batched so one run cannot hold a table for long. Only counts are
logged and audited: never ids, names or file contents.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import String, Text, cast, delete, exists, func, or_, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.logging import get_logger, log_failure
from app.db.base import utcnow
from app.models.content import MarketingAsset
from app.models.creative import (
    BrandKit,
    CreativeJob,
    FileKind,
    FileSource,
    Product,
    StoredFile,
)
from app.models.demo import DemoAnalysis
from app.models.identity import LoginCode
from app.models.workspace import RefreshToken

log = get_logger(__name__)

#: An upload nothing references is kept this long, so a file uploaded while a
#: kit or product is still being filled in is never pulled out from under it.
ORPHAN_UPLOAD_DAYS = 7
#: The only kinds of file this module may delete.
PRUNABLE_UPLOAD_KINDS: tuple[str, ...] = (
    FileKind.LOGO,
    FileKind.PRODUCT_PHOTO,
    FileKind.BACKGROUND,
)
#: Rows deleted per statement.
DELETE_BATCH = 1_000
#: Statements per table per run; whatever is left goes tomorrow.
MAX_BATCHES = 20
#: Orphaned files removed per workspace per run.
ORPHAN_BATCH = 200


@dataclass(frozen=True, slots=True)
class OrphanPrune:
    files: int = 0
    bytes: int = 0
    failed: int = 0


# ── Expired credentials and demo requests ─────────────────────────────────
def _delete_in_batches(db: Session, model: Any, condition: Any) -> int:  # noqa: ANN401
    """Delete rows of ``model`` matching ``condition``, a batch at a time."""
    removed = 0
    for _ in range(MAX_BATCHES):
        batch = select(model.id).where(condition).limit(DELETE_BATCH).scalar_subquery()
        result = db.execute(
            delete(model).where(model.id.in_(batch)).execution_options(synchronize_session=False)
        )
        count = int(result.rowcount or 0)
        removed += count
        if count < DELETE_BATCH:
            break
    return removed


def expired_conditions(now: datetime) -> dict[str, tuple[Any, Any]]:
    """What :func:`prune_expired` deletes, per table: ``(model, condition)``."""
    return {
        "login_codes": (
            LoginCode,
            LoginCode.expires_at < now - timedelta(days=settings.retention_login_codes_days),
        ),
        "refresh_tokens": (
            RefreshToken,
            RefreshToken.expires_at < now - timedelta(days=settings.retention_refresh_tokens_days),
        ),
        "demo_analyses": (
            DemoAnalysis,
            DemoAnalysis.created_at < now - timedelta(days=settings.retention_demo_analyses_days),
        ),
    }


def prune_expired(db: Session, *, now: datetime | None = None) -> dict[str, int]:
    """Delete expired sign-in codes, refresh tokens and old demo requests.

    The caller owns the transaction. Returns the count per table.
    """
    moment = now or utcnow()
    return {
        table: _delete_in_batches(db, model, condition)
        for table, (model, condition) in expired_conditions(moment).items()
    }


# ── Orphaned uploads ──────────────────────────────────────────────────────
def orphan_uploads_query(tenant_id: str, *, now: datetime, limit: int = ORPHAN_BATCH) -> Any:  # noqa: ANN401
    """Old uploads in one workspace that no kit, product, job or asset uses.

    Every reference check is filtered to the same workspace. JSON columns are
    matched on the file id's text, which can only over-match (keep a file),
    never under-match. The rows are locked so a kit cannot link a file between
    this check and its deletion (the kit's foreign key waits on the lock).
    """
    file_id = cast(StoredFile.id, String)
    used_by_kit = exists().where(
        BrandKit.tenant_id == tenant_id,
        or_(BrandKit.logo_file_id == StoredFile.id, BrandKit.logo_dark_file_id == StoredFile.id),
    )
    used_by_product = exists().where(
        Product.tenant_id == tenant_id,
        Product.image_file_ids.op("@>", is_comparison=True)(func.jsonb_build_array(file_id)),
    )
    used_by_job = exists().where(
        CreativeJob.tenant_id == tenant_id,
        cast(CreativeJob.outputs, Text).contains(file_id),
    )
    used_by_asset = exists().where(
        MarketingAsset.tenant_id == tenant_id,
        or_(
            cast(MarketingAsset.detail, Text).contains(file_id),
            cast(MarketingAsset.image_ids, Text).contains(file_id),
        ),
    )
    return (
        select(StoredFile)
        .where(
            StoredFile.tenant_id == tenant_id,
            StoredFile.kind.in_(PRUNABLE_UPLOAD_KINDS),
            StoredFile.source == FileSource.UPLOAD,
            # Anything a job made is the job's, whatever its kind.
            StoredFile.job_id == "",
            StoredFile.created_at < now - timedelta(days=ORPHAN_UPLOAD_DAYS),
            ~used_by_kit,
            ~used_by_product,
            ~used_by_job,
            ~used_by_asset,
        )
        .order_by(StoredFile.created_at)
        .limit(limit)
        .with_for_update(of=StoredFile, skip_locked=True)
    )


def prune_orphan_uploads(
    db: Session, *, tenant_id: str, now: datetime | None = None
) -> OrphanPrune:
    """Delete one workspace's orphaned uploads (row and bytes).

    The caller owns the transaction and must have pinned ``tenant_id``. A
    file whose storage backend refuses is left in place and counted as failed.
    """
    from app.services import audit
    from app.services.storage import storage_for

    moment = now or utcnow()
    candidates = list(db.execute(orphan_uploads_query(tenant_id, now=moment)).scalars())
    files = size = failed = 0
    for stored in candidates:
        # Re-checked here so a wrong query can never reach another workspace
        # or a kind this module must not delete.
        if (
            stored.tenant_id != tenant_id
            or stored.kind not in PRUNABLE_UPLOAD_KINDS
            or stored.source != FileSource.UPLOAD
        ):
            failed += 1
            continue
        stored_bytes = int(stored.size_bytes or 0)
        try:
            storage_for(stored).delete(db, tenant_id=tenant_id, file_id=stored.id)
        except Exception as exc:  # noqa: BLE001 - one file must not stop the rest
            log_failure(log, "Could not remove an unused upload", exc, tenant=tenant_id)
            failed += 1
            continue
        files += 1
        size += stored_bytes

    if files:
        audit.record_system_action(
            db,
            tenant_id=tenant_id,
            action=f"removed {files} unused uploaded file(s) older than {ORPHAN_UPLOAD_DAYS} days",
            module="brand_kit",
            context={"files": files, "bytes": size},
        )
    return OrphanPrune(files=files, bytes=size, failed=failed)
