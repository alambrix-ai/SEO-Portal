"""Creative Studio: start design jobs, follow them, download approved work.

* ``GET  /creative/options``                         sizes, AI model picker, this month's allowance, input limits
* ``POST /creative/jobs``                            start a design job (202); a worker does the slow part
* ``GET  /creative/jobs``                            this workspace's recent jobs
* ``GET  /creative/jobs/{id}``                       one job, with its outputs and their review status
* ``GET  /creative/jobs/{id}/download?format=zip``   every approved design of a job, in one ZIP
* ``GET  /creative/assets/{id}/download?format=…``   one approved design as PNG, PDF or ZIP

Reading needs Content view access; starting a job needs Content write
access. The role check is a dependency, so it runs before the body is
validated. Every lookup is filtered by the caller's workspace: another
workspace's job, design or file is "not found". Only approved designs can be
downloaded (409 otherwise), with a header-safe file name, ``nosniff`` and a
private cache policy. The job rules live in
:mod:`app.services.creative.pipeline`.
"""
from __future__ import annotations

import io
import unicodedata
import zipfile
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Path, Query
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.sql import Select

from app.api.deps import ClientIp, CurrentUser, DbSession, require_module
from app.core.exceptions import ConflictError, InvalidInputError, NotFoundError
from app.core.logging import get_logger
from app.core.rbac import Module
from app.models.content import AssetKind, AssetStatus, MarketingAsset
from app.models.creative import FileKind, StoredFile
from app.schemas.common import ApiModel
from app.services import audit, brand_kits
from app.services.assets import slug_for
from app.services.creative import model_choice, pipeline, quotas
from app.services.creative.formats import FORMATS, FormatSpec
from app.services.storage import storage_for

log = get_logger(__name__)

router = APIRouter(prefix="/creative", tags=["creative"])

ContentViewer = Annotated[CurrentUser, Depends(require_module(Module.CONTENT))]
ContentWriter = Annotated[CurrentUser, Depends(require_module(Module.CONTENT, write=True))]

AUDIT_MODULE = Module.CONTENT.value
_ID_PATTERN = r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"

#: A design may be downloaded once a reviewer approved it (and after that).
DOWNLOADABLE_STATUSES = frozenset({AssetStatus.APPROVED, AssetStatus.EXPORTED})
#: The only stored kinds a design's download can contain.
_OUTPUT_KINDS = (FileKind.RENDER, FileKind.PREVIEW)
_RASTER_MIMES = frozenset({"image/png", "image/jpeg"})
_EXTENSIONS: dict[str, str] = {"image/png": ".png", "image/jpeg": ".jpg", "application/pdf": ".pdf"}
#: Most bytes (before compression) one ZIP may hold. Renders are small, a
#: print PDF a few MB; this bounds the memory one download can take.
MAX_ZIP_BYTES = 100 * 1024 * 1024

#: Downloads are a workspace's own work: never kept by a shared cache.
CACHE_CONTROL = "private, no-store"
CONTENT_SECURITY_POLICY = "default-src 'none'; frame-ancestors 'none'; sandbox"

NOT_APPROVED_YET = "This design can be downloaded once it has been approved."
NOTHING_APPROVED_YET = "None of this request's designs have been approved yet. Approve one, then download."
DESIGN_NOT_FOUND = "That design could not be found"
FILE_MISSING = "The file for this design is no longer available. Ask for changes to draw it again."
NO_PDF = "A PDF is available for print designs. Download the PNG instead."
TOO_BIG = "These designs are too big to download together. Download them one at a time."


# ── Schemas ────────────────────────────────────────────────────────────────
class CopyLimitsOut(ApiModel):
    headline_chars: int
    body_chars: int
    cta_chars: int
    headline_words: int | None = None


class FormatOut(ApiModel):
    id: str
    label: str
    kind: str
    width_px: int
    height_px: int
    network: str
    panels: int
    limits: CopyLimitsOut


class ModelsOut(model_choice.ModelPicker):
    #: Preselected choices; "" when nothing can be chosen.
    recommended_text: str = ""
    recommended_image: str = model_choice.NO_IMAGE


class QuotaOut(ApiModel):
    #: Monthly caps; 0 means no cap.
    limits: dict[str, int]
    used: dict[str, int]
    #: The day (UTC) the allowance starts again.
    resets_on: str


class OptionsOut(ApiModel):
    formats: list[FormatOut]
    models: ModelsOut
    quota: QuotaOut
    limits: dict[str, int]


def format_out(spec: FormatSpec) -> FormatOut:
    return FormatOut(
        id=spec.id,
        label=spec.label,
        kind=spec.kind,
        width_px=spec.width_px,
        height_px=spec.height_px,
        network=spec.network,
        panels=spec.panels,
        limits=CopyLimitsOut(
            headline_chars=spec.limits.headline_chars,
            body_chars=spec.limits.body_chars,
            cta_chars=spec.limits.cta_chars,
            headline_words=spec.limits.headline_words,
        ),
    )


def models_out(picker: model_choice.ModelPicker) -> ModelsOut:
    """The picker plus a preselection: the first usable text model, and no AI
    image (it is always allowed and costs nothing; the user can pick one)."""
    text = next((o.key for o in picker.text if o.available), "")
    return ModelsOut(**picker.model_dump(), recommended_text=text, recommended_image=model_choice.NO_IMAGE)


def quota_out(db: Session, current: CurrentUser) -> QuotaOut:
    org = current.organization
    limits = quotas.limits_from(getattr(org, "settings", None))
    used = quotas.usage(db, current.tenant_id)
    _, end = quotas.month_window()
    return QuotaOut(limits=limits.as_dict(), used=used.as_dict(), resets_on=end.date().isoformat())


# ── Helpers: tenant-filtered loading ───────────────────────────────────────
def asset_query(tenant_id: str, asset_id: str) -> Select:
    """One creative design of this workspace."""
    return select(MarketingAsset).where(
        MarketingAsset.id == asset_id,
        MarketingAsset.tenant_id == tenant_id,
        MarketingAsset.kind == AssetKind.CREATIVE,
    )


def assets_query(tenant_id: str, asset_ids: list[str]) -> Select:
    """Several creative designs of this workspace."""
    return select(MarketingAsset).where(
        MarketingAsset.id.in_(asset_ids),
        MarketingAsset.tenant_id == tenant_id,
        MarketingAsset.kind == AssetKind.CREATIVE,
    )


def load_design(db: Session, tenant_id: str, asset_id: str) -> MarketingAsset:
    canonical = brand_kits.parse_id(asset_id)
    asset = db.execute(asset_query(tenant_id, canonical)).scalars().first() if canonical else None
    if asset is None:
        raise NotFoundError(DESIGN_NOT_FOUND)
    return asset


def load_approved_designs(db: Session, tenant_id: str, asset_ids: list[str]) -> dict[str, MarketingAsset]:
    wanted = sorted({i for i in (brand_kits.parse_id(a) for a in asset_ids) if i})
    if not wanted:
        return {}
    rows = db.execute(assets_query(tenant_id, wanted)).scalars()
    return {row.id: row for row in rows if row.status in DOWNLOADABLE_STATUSES}


def design_files(db: Session, tenant_id: str, asset: MarketingAsset) -> tuple[StoredFile, StoredFile | None]:
    """``(full-size file, preview or None)``, both this workspace's output files.

    When the approval recorded which render was approved
    (``approved_render_file_id``), that render is served, with the preview
    approved alongside it; a later redraw never changes what is downloaded.
    Designs approved before that was recorded serve their current render.
    """
    detail = asset.detail or {}
    approved_id = detail.get("approved_render_file_id")
    if approved_id:
        main_id = approved_id
        preview_id = detail.get("approved_preview_file_id")
        if not preview_id and detail.get("render_file_id") == approved_id:
            # Not redrawn since approval: the current preview is the approved one.
            preview_id = detail.get("preview_file_id")
    else:
        main_id = detail.get("render_file_id") or detail.get("file_id")
        preview_id = detail.get("preview_file_id")
    main = brand_kits.get_file(db, tenant_id, str(main_id or ""), _OUTPUT_KINDS)
    if main is None or main.mime not in _EXTENSIONS:
        raise ConflictError(FILE_MISSING)
    preview = None
    if preview_id and preview_id != main.id:
        preview = brand_kits.get_file(db, tenant_id, str(preview_id), _OUTPUT_KINDS)
        if preview is not None and preview.mime not in _RASTER_MIMES:
            preview = None
    return main, preview


def file_bytes(db: Session, tenant_id: str, row: StoredFile) -> bytes:
    return storage_for(row).get(db, tenant_id=tenant_id, file_id=row.id)


def _ascii_slug(text: str, fallback: str) -> str:
    """A header- and archive-safe name: ASCII letters, digits and hyphens only."""
    ascii_text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode("ascii")
    return slug_for(ascii_text or fallback)


def _format_id(main: StoredFile) -> str:
    """The design's size id, from its render's stored name (``{format_id}.png``)."""
    stem = (main.display_name or "").rsplit(".", 1)[0]
    return stem if stem in FORMATS else "design"


def _download(data: bytes, mime: str, filename: str) -> Response:
    return Response(
        content=data,
        media_type=mime,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": CONTENT_SECURITY_POLICY,
            "Cache-Control": CACHE_CONTROL,
        },
    )


class _ZipBuilder:
    """An in-memory ZIP with a total size cap and flat, unique, safe names."""

    def __init__(self, max_bytes: int | None = None) -> None:
        self._buffer = io.BytesIO()
        self._zip = zipfile.ZipFile(self._buffer, "w", compression=zipfile.ZIP_DEFLATED)
        self._names: set[str] = set()
        self._total = 0
        self._max = MAX_ZIP_BYTES if max_bytes is None else max_bytes

    def __enter__(self) -> _ZipBuilder:
        return self

    def __exit__(self, *_exc: object) -> None:
        # Closed on every path, including a refusal half-way through.
        self._zip.close()

    def reserve(self, size: int) -> None:
        """Refuse before reading a file that would take the archive past the cap."""
        if size < 0 or self._total + size > self._max:
            raise ConflictError(TOO_BIG)

    def add(self, stem: str, extension: str, data: bytes, when: datetime | None) -> str:
        self.reserve(len(data))
        safe_stem = _ascii_slug(stem, "design")
        name = f"{safe_stem}{extension}"
        n = 2
        while name in self._names:
            name = f"{safe_stem}-{n}{extension}"
            n += 1
        moment = when or datetime(1980, 1, 1)
        info = zipfile.ZipInfo(name, date_time=(max(moment.year, 1980), moment.month, moment.day,
                                                moment.hour, moment.minute, moment.second))
        info.compress_type = zipfile.ZIP_DEFLATED
        info.external_attr = 0o644 << 16
        self._zip.writestr(info, data)
        self._names.add(name)
        self._total += len(data)
        return name

    @property
    def count(self) -> int:
        return len(self._names)

    def finish(self) -> bytes:
        self._zip.close()
        return self._buffer.getvalue()


def _add_design(builder: _ZipBuilder, db: Session, tenant_id: str, main: StoredFile,
                preview: StoredFile | None) -> None:
    """The full-size file (and, when given, its preview), size-checked before reading."""
    stem = _format_id(main)
    builder.reserve(main.size_bytes or 0)
    builder.add(stem, _EXTENSIONS[main.mime], file_bytes(db, tenant_id, main), main.created_at)
    if preview is not None:
        builder.reserve(preview.size_bytes or 0)
        builder.add(f"{stem}-preview", _EXTENSIONS[preview.mime], file_bytes(db, tenant_id, preview),
                    preview.created_at)


def _audit_download(db: Session, current: CurrentUser, ip: str, action: str, **ids: object) -> None:
    audit.record_user_action(
        db, user=current.user, action=action, module=AUDIT_MODULE, ip_address=ip, context=ids
    )
    db.commit()


# ── Options ────────────────────────────────────────────────────────────────
@router.get("/options", response_model=OptionsOut)
def read_options(current: ContentViewer, db: DbSession) -> OptionsOut:
    return OptionsOut(
        formats=[format_out(spec) for spec in FORMATS.values()],
        models=models_out(model_choice.picker_for(db, current.organization)),
        quota=quota_out(db, current),
        limits=pipeline.input_limits(),
    )


# ── Jobs ───────────────────────────────────────────────────────────────────
@router.post("/jobs", response_model=pipeline.CreativeJobOut, status_code=202)
def create_job(
    body: pipeline.CreativeJobRequest, current: ContentWriter, db: DbSession, ip: ClientIp
) -> pipeline.CreativeJobOut:
    """Start a design job. 404 for another workspace's kit or product, 422 for
    a model off the allow-list, 429 when the month's designs are used up.

    The job is committed before a worker picks it up (the pipeline starts
    it once this transaction commits).
    """
    org = current.organization
    job = pipeline.submit_job(db, org=org, user_id=current.user.id, request=body)
    audit.record_user_action(
        db,
        user=current.user,
        action=f"started a Creative Studio request for {len(job.formats or [])} size(s)",
        module=AUDIT_MODULE,
        ip_address=ip,
        context={"job_id": job.id, "brand_kit_id": job.brand_kit_id, "product_id": job.product_id},
    )
    db.commit()
    return pipeline.job_out(db, org, job, include_copy=True)


@router.get("/jobs", response_model=list[pipeline.CreativeJobOut])
def list_jobs(
    current: ContentViewer,
    db: DbSession,
    limit: int = Query(default=50, ge=1, le=100),
) -> list[pipeline.CreativeJobOut]:
    org = current.organization
    jobs = pipeline.list_jobs(db, tenant_id=current.tenant_id, limit=limit)
    return [pipeline.job_out(db, org, job, include_copy=True) for job in jobs]


@router.get("/jobs/{job_id}", response_model=pipeline.CreativeJobOut)
def read_job(
    current: ContentViewer, db: DbSession, job_id: str = Path(pattern=_ID_PATTERN)
) -> pipeline.CreativeJobOut:
    job = pipeline.get_job(db, tenant_id=current.tenant_id, job_id=job_id)
    return pipeline.job_out(db, current.organization, job, include_copy=True)


@router.get("/jobs/{job_id}/download")
def download_job(
    current: ContentViewer,
    db: DbSession,
    ip: ClientIp,
    job_id: str = Path(pattern=_ID_PATTERN),
    format: Literal["zip"] = Query(default="zip"),  # noqa: A002 - the public query name
) -> Response:
    """Every approved design of one job, one file per size, in a ZIP."""
    job = pipeline.get_job(db, tenant_id=current.tenant_id, job_id=job_id)
    outputs = [o for o in (job.outputs or []) if isinstance(o, dict)]
    ordered = [str(o.get("asset_id") or "") for o in outputs]
    approved = load_approved_designs(db, current.tenant_id, ordered)
    if not approved:
        raise ConflictError(NOTHING_APPROVED_YET, details={"reason": "not_approved"})
    limit = max(1, len(job.formats or []))
    seen: set[str] = set()
    with _ZipBuilder() as builder:
        for asset_id in ordered:
            asset = approved.get(brand_kits.parse_id(asset_id) or "")
            if asset is None or asset.id in seen or builder.count >= limit:
                continue
            seen.add(asset.id)
            main, _preview = design_files(db, current.tenant_id, asset)
            _add_design(builder, db, current.tenant_id, main, None)
        data = builder.finish()
    _audit_download(db, current, ip, "downloaded the approved designs of a Creative Studio request",
                    job_id=job.id, asset_ids=sorted(seen), format=format)
    log.info("Creative job downloaded: tenant=%s job=%s files=%d", current.tenant_id, job.id, len(seen))
    return _download(data, "application/zip", f"designs-{job.id[:8]}.zip")


# ── Downloading one design ─────────────────────────────────────────────────
@router.get("/assets/{asset_id}/download")
def download_design(
    current: ContentViewer,
    db: DbSession,
    ip: ClientIp,
    asset_id: str = Path(pattern=_ID_PATTERN),
    format: Literal["png", "pdf", "zip"] = Query(default="png"),  # noqa: A002 - the public query name
) -> Response:
    """One approved design. PNG for screen sizes (the preview for print), PDF
    for print sizes, or a ZIP of the full file and its preview."""
    asset = load_design(db, current.tenant_id, asset_id)
    if asset.status not in DOWNLOADABLE_STATUSES:
        raise ConflictError(NOT_APPROVED_YET, details={"reason": "not_approved"})
    main, preview = design_files(db, current.tenant_id, asset)
    stem = _ascii_slug(_format_id(main), "design")

    if format == "zip":
        with _ZipBuilder() as builder:
            _add_design(builder, db, current.tenant_id, main, preview)
            data = builder.finish()
        mime, filename = "application/zip", f"{stem}.zip"
    elif format == "pdf":
        if main.mime != "application/pdf":
            raise InvalidInputError(NO_PDF, details={"reason": "no_pdf"})
        data, mime, filename = file_bytes(db, current.tenant_id, main), main.mime, f"{stem}.pdf"
    else:
        raster = main if main.mime in _RASTER_MIMES else preview
        if raster is None:
            raise ConflictError(FILE_MISSING)
        data, mime = file_bytes(db, current.tenant_id, raster), raster.mime
        filename = f"{stem}{_EXTENSIONS[raster.mime]}"
    _audit_download(db, current, ip, "downloaded an approved design", asset_id=asset.id, format=format)
    return _download(data, mime, filename)


__all__ = ["router"]
