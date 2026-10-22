"""Serve Creative Studio files: ``GET /files/{file_id}``.

Every file is looked up in the caller's workspace only (a tenant filter in
the query, with row-level security behind it), so another workspace's file
id gets the same "not found" as an id that never existed.

Access follows what the file is:

* logos, product photos and uploaded backgrounds belong to the brand kit and
  need Brand Kit view access;
* renders, previews and AI-made backgrounds belong to creatives and need
  Content view access.

A full-size render is the finished design, so it follows the same approval
gate as the download routes: it is served only while the design that owns it
(found through the job that made it, or through the design's own file ids) is
approved or exported, and only if it is the render that was approved. Any
other render gets the same "not found" as a missing file. Previews, logos and
the other kinds are not gated.

The response is the stored bytes as the stored MIME type (one of
``STORED_FILE_MIMES``; anything else is refused), with ``nosniff``, a CSP
that lets nothing run, a header-safe filename built by ``slug_for`` and a
private cache policy.
"""
from __future__ import annotations

import unicodedata

from fastapi import APIRouter
from fastapi.responses import Response
from sqlalchemy import Select, or_, select
from sqlalchemy.orm import Session

from app.api.deps import CurrentUserDep, DbSession
from app.core.exceptions import NotFoundError
from app.core.logging import get_logger
from app.core.rbac import Module
from app.models.content import AssetKind, AssetStatus, MarketingAsset
from app.models.creative import STORED_FILE_MIMES, CreativeJob, FileKind, FileSource, StoredFile
from app.services import brand_kits
from app.services.assets import slug_for
from app.services.storage import FILE_NOT_FOUND, storage_for

log = get_logger(__name__)

router = APIRouter(prefix="/files", tags=["files"])

#: Kinds that belong to the brand kit; every other known kind is creative output.
BRAND_KIT_KINDS = frozenset({FileKind.LOGO, FileKind.PRODUCT_PHOTO, FileKind.BACKGROUND})
CREATIVE_KINDS = frozenset({FileKind.RENDER, FileKind.PREVIEW})

_EXTENSIONS: dict[str, str] = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "application/pdf": ".pdf",
}
#: Files rarely change under an id, but they are a workspace's own: browsers
#: may keep them briefly, shared caches never.
CACHE_CONTROL = "private, max-age=300"
CONTENT_SECURITY_POLICY = "default-src 'none'; frame-ancestors 'none'; sandbox"

#: A design's full-size render is served only in these states.
RELEASED_STATUSES = frozenset({AssetStatus.APPROVED, AssetStatus.EXPORTED})
#: Detail keys that name a design's full-size render.
_RENDER_KEYS = ("approved_render_file_id", "render_file_id", "file_id")
#: Most designs looked at when finding a render's owner (a job has a few sizes).
MAX_OWNER_CANDIDATES = 50


def module_for(stored: StoredFile) -> Module | None:
    """The module whose view access a file needs, or ``None`` for an unknown kind."""
    if stored.kind == FileKind.BACKGROUND and stored.source == FileSource.GENERATED:
        return Module.CONTENT
    if stored.kind in BRAND_KIT_KINDS:
        return Module.BRAND_KIT
    if stored.kind in CREATIVE_KINDS:
        return Module.CONTENT
    return None


def download_name(stored: StoredFile) -> str:
    """A header-safe file name: an ASCII slug of the display name and the stored type's extension.

    Never the stored name as-is: it is user text, and a quote, line break or
    non-Latin-1 character in a header value would break or split the header.
    """
    stem = stored.display_name.rsplit(".", 1)[0] if stored.display_name else ""
    ascii_stem = unicodedata.normalize("NFKD", stem).encode("ascii", "ignore").decode("ascii")
    return f"{slug_for(ascii_stem or stored.kind)}{_EXTENSIONS[stored.mime]}"


def released_render_ids(asset: MarketingAsset) -> set[str]:
    """The render ids of ``asset`` that may be served once it is approved.

    When the approval recorded which render was approved, only that one;
    otherwise the design's current full-size file.
    """
    detail = asset.detail or {}
    approved = detail.get("approved_render_file_id")
    if approved:
        return {str(approved)}
    return {str(v) for v in (detail.get("render_file_id"), detail.get("file_id")) if v}


def _creative_assets(tenant_id: str) -> Select[tuple[MarketingAsset]]:
    return select(MarketingAsset).where(
        MarketingAsset.tenant_id == tenant_id, MarketingAsset.kind == AssetKind.CREATIVE
    )


def _owners_via_job(db: Session, tenant_id: str, stored: StoredFile) -> list[MarketingAsset]:
    """This workspace's designs listed in the outputs of the job that made ``stored``."""
    job_id = brand_kits.parse_id(stored.job_id)
    if job_id is None:
        return []
    job = db.execute(
        select(CreativeJob).where(CreativeJob.id == job_id, CreativeJob.tenant_id == tenant_id)
    ).scalars().first()
    if job is None:
        return []
    outputs = [o for o in (job.outputs or []) if isinstance(o, dict)]
    asset_ids = sorted({i for i in (brand_kits.parse_id(o.get("asset_id")) for o in outputs) if i})
    if not asset_ids:
        return []
    stmt = _creative_assets(tenant_id).where(MarketingAsset.id.in_(asset_ids[:MAX_OWNER_CANDIDATES]))
    return list(db.execute(stmt).scalars())


def _owners_via_detail(db: Session, tenant_id: str, file_id: str) -> list[MarketingAsset]:
    """This workspace's designs whose own file ids name ``file_id``."""
    refs = [MarketingAsset.detail[key].astext == file_id for key in _RENDER_KEYS]
    stmt = _creative_assets(tenant_id).where(or_(*refs)).limit(MAX_OWNER_CANDIDATES)
    return list(db.execute(stmt).scalars())


def render_is_released(db: Session, tenant_id: str, stored: StoredFile) -> bool:
    """Whether a full-size render may be served: its design is approved (or
    exported) and it is the render that design releases."""
    def released(owners: list[MarketingAsset]) -> bool:
        return any(a.status in RELEASED_STATUSES and stored.id in released_render_ids(a) for a in owners)

    # The job's own outputs first (a key lookup); then the designs' file ids,
    # which also covers a design redrawn after its job.
    return released(_owners_via_job(db, tenant_id, stored)) or released(
        _owners_via_detail(db, tenant_id, stored.id)
    )


@router.get("/{file_id}")
def read_file(file_id: str, current: CurrentUserDep, db: DbSession) -> Response:
    stored = brand_kits.get_file(db, current.tenant_id, file_id)
    module = module_for(stored) if stored is not None else None
    if (
        stored is None
        or module is None
        or stored.mime not in STORED_FILE_MIMES
        or stored.mime not in _EXTENSIONS
    ):
        raise NotFoundError(FILE_NOT_FOUND)
    current.require_view(module)
    if stored.kind == FileKind.RENDER and not render_is_released(db, current.tenant_id, stored):
        # An unapproved design's full-size file is not available yet; the
        # same answer as a missing file, so nothing is revealed.
        log.info("Render not served before approval: tenant=%s file=%s", current.tenant_id, stored.id)
        raise NotFoundError(FILE_NOT_FOUND)
    data = storage_for(stored).get(db, tenant_id=current.tenant_id, file_id=stored.id)
    disposition = "attachment" if stored.mime == "application/pdf" else "inline"
    headers = {
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": CONTENT_SECURITY_POLICY,
        "Content-Disposition": f'{disposition}; filename="{download_name(stored)}"',
        "Cache-Control": CACHE_CONTROL,
    }
    return Response(content=data, media_type=stored.mime, headers=headers)
