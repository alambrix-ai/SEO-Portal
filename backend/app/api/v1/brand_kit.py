"""Brand kits, products and image uploads for Creative Studio.

* ``GET    /brand-kit/options``               fonts and limits for the editors
* ``GET    /brand-kit/prefill``               suggestions from the brand profile (nothing saved)
* ``GET    /brand-kit/kits``                  list; ``POST`` creates one
* ``GET    /brand-kit/kits/{id}``             one kit; ``PATCH`` edits, ``DELETE`` removes
* ``POST   /brand-kit/kits/{id}/default``     make a kit the workspace default
* ``GET    /brand-kit/products``              list; ``POST`` creates one
* ``GET    /brand-kit/products/{id}``         one product; ``PATCH`` edits, ``DELETE`` archives
* ``POST   /brand-kit/uploads``               a logo, product photo or background (multipart)

Reading needs Brand Kit view access; every change needs Brand Kit write
access. The role check is a dependency, so it runs before the request body
is validated or an upload is read. Uploaded files are served by
``GET /files/{id}`` (:mod:`app.api.v1.files`).
"""
from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.deps import CurrentUser, DbSession, require_module
from app.core.config import settings
from app.core.exceptions import AppError, InvalidInputError
from app.core.rbac import Module
from app.models.creative import BrandKit, Product, StoredFile
from app.schemas.common import ActionResult, ApiModel, Toast
from app.services import audit, brand_kits, uploads
from app.services.creative.svg import svg_byte_limit

router = APIRouter(prefix="/brand-kit", tags=["brand_kit"])

BrandKitViewer = Annotated[CurrentUser, Depends(require_module(Module.BRAND_KIT))]
BrandKitWriter = Annotated[CurrentUser, Depends(require_module(Module.BRAND_KIT, write=True))]

AUDIT_MODULE = Module.BRAND_KIT.value
#: Room for the multipart boundaries, headers and the ``kind`` field.
MULTIPART_OVERHEAD_BYTES = 64 * 1024
UNREADABLE_UPLOAD = "We couldn't read that upload. Please choose one image and try again."
NO_FILE_CHOSEN = "Please choose an image to upload."


class UploadTooLarge(AppError):
    """The request body is bigger than any acceptable image."""

    status_code = 413
    code = "upload_too_large"


class UploadLengthRequired(AppError):
    """The upload did not say how big it is, so it cannot be bounded up front."""

    status_code = 411
    code = "length_required"


# ── Schemas ────────────────────────────────────────────────────────────────
class KitOut(ApiModel):
    id: str
    name: str
    is_default: bool
    logo_file_id: str | None = None
    logo_dark_file_id: str | None = None
    primary_color: str
    secondary_color: str
    accent_color: str
    text_color: str
    background_color: str
    heading_font: str
    body_font: str
    voice: str
    banned_phrases: list[str]
    disclaimer: str
    created_at: datetime
    updated_at: datetime


class ProductOut(ApiModel):
    id: str
    name: str
    description: str
    url: str
    price_text: str
    approved_claims: list[str]
    image_file_ids: list[str]
    disclaimer: str
    is_archived: bool
    created_at: datetime
    updated_at: datetime


class FileOut(ApiModel):
    id: str
    kind: str
    mime: str
    width: int
    height: int
    size_bytes: int
    display_name: str
    created_at: datetime


class FontOption(ApiModel):
    id: str
    label: str


class OptionsOut(ApiModel):
    fonts: list[FontOption]
    limits: dict[str, int]


class KitSuggestion(ApiModel):
    name: str
    voice: str


class ProductSuggestion(ApiModel):
    name: str


class PrefillOut(ApiModel):
    #: False when the workspace has no brand profile yet.
    available: bool
    kit: KitSuggestion | None = None
    products: list[ProductSuggestion]
    #: Suggestions only: a person approves a claim by saving it on a product.
    claim_suggestions: list[str]


def _strings(values: Any) -> list[str]:  # noqa: ANN401 - JSONB column
    return [v for v in (values or []) if isinstance(v, str)]


def kit_out(kit: BrandKit) -> KitOut:
    return KitOut(
        id=kit.id,
        name=kit.name,
        is_default=kit.is_default,
        logo_file_id=kit.logo_file_id,
        logo_dark_file_id=kit.logo_dark_file_id,
        primary_color=kit.primary_color,
        secondary_color=kit.secondary_color,
        accent_color=kit.accent_color,
        text_color=kit.text_color,
        background_color=kit.background_color,
        heading_font=kit.heading_font,
        body_font=kit.body_font,
        voice=kit.voice,
        banned_phrases=_strings(kit.banned_phrases),
        disclaimer=kit.disclaimer,
        created_at=kit.created_at,
        updated_at=kit.updated_at,
    )


def product_out(product: Product, image_file_ids: list[str]) -> ProductOut:
    return ProductOut(
        id=product.id,
        name=product.name,
        description=product.description,
        url=product.url,
        price_text=product.price_text,
        approved_claims=_strings(product.approved_claims),
        image_file_ids=image_file_ids,
        disclaimer=product.disclaimer,
        is_archived=product.is_archived,
        created_at=product.created_at,
        updated_at=product.updated_at,
    )


def file_out(stored: StoredFile) -> FileOut:
    return FileOut(
        id=stored.id,
        kind=stored.kind,
        mime=stored.mime,
        width=stored.width,
        height=stored.height,
        size_bytes=stored.size_bytes,
        display_name=stored.display_name,
        created_at=stored.created_at,
    )


def _products_out(db: Session, tenant_id: str, products: list[Product]) -> list[ProductOut]:
    visible = brand_kits.visible_image_ids(db, tenant_id, products)
    return [product_out(p, visible.get(p.id, [])) for p in products]


def _audit(db: Session, current: CurrentUser, action: str, **ids: str) -> None:
    """An audit entry with ids only; names and text stay out of the log."""
    audit.record_user_action(db, user=current.user, action=action, module=AUDIT_MODULE, context=ids)


# ── Options and prefill ────────────────────────────────────────────────────
@router.get("/options", response_model=OptionsOut)
def read_options(current: BrandKitViewer) -> OptionsOut:  # noqa: ARG001 - access check
    return OptionsOut.model_validate(brand_kits.options())


@router.get("/prefill", response_model=PrefillOut)
def read_prefill(current: BrandKitWriter) -> PrefillOut:
    """Suggestions to start a kit and products from. Saves nothing, calls no model."""
    return PrefillOut.model_validate(brand_kits.prefill(current.organization))


# ── Brand kits ─────────────────────────────────────────────────────────────
@router.get("/kits", response_model=list[KitOut])
def list_kits(current: BrandKitViewer, db: DbSession) -> list[KitOut]:
    return [kit_out(k) for k in brand_kits.list_kits(db, current.tenant_id)]


@router.post("/kits", response_model=KitOut, status_code=201)
def create_kit(body: brand_kits.KitIn, current: BrandKitWriter, db: DbSession) -> KitOut:
    kit = brand_kits.create_kit(db, current.tenant_id, body, actor=current.user)
    _audit(db, current, "created a brand kit", kit_id=kit.id)
    db.commit()
    return kit_out(kit)


@router.get("/kits/{kit_id}", response_model=KitOut)
def read_kit(kit_id: str, current: BrandKitViewer, db: DbSession) -> KitOut:
    return kit_out(brand_kits.get_kit(db, current.tenant_id, kit_id))


@router.patch("/kits/{kit_id}", response_model=KitOut)
def update_kit(kit_id: str, body: brand_kits.KitPatch, current: BrandKitWriter, db: DbSession) -> KitOut:
    kit = brand_kits.update_kit(db, current.tenant_id, kit_id, body, actor=current.user)
    _audit(db, current, "edited a brand kit", kit_id=kit.id)
    db.commit()
    return kit_out(kit)


@router.post("/kits/{kit_id}/default", response_model=KitOut)
def make_default_kit(kit_id: str, current: BrandKitWriter, db: DbSession) -> KitOut:
    kit = brand_kits.set_default_kit(db, current.tenant_id, kit_id)
    _audit(db, current, "made a brand kit the default", kit_id=kit.id)
    db.commit()
    return kit_out(kit)


@router.delete("/kits/{kit_id}", response_model=ActionResult)
def delete_kit(kit_id: str, current: BrandKitWriter, db: DbSession) -> ActionResult:
    deleted_id = brand_kits.delete_kit(db, current.tenant_id, kit_id)
    _audit(db, current, "deleted a brand kit", kit_id=deleted_id)
    db.commit()
    return ActionResult(toast=Toast(message="Brand kit deleted", kind="success"))


# ── Products ───────────────────────────────────────────────────────────────
@router.get("/products", response_model=list[ProductOut])
def list_products(
    current: BrandKitViewer,
    db: DbSession,
    include_archived: bool = Query(default=False),
) -> list[ProductOut]:
    products = brand_kits.list_products(db, current.tenant_id, include_archived=include_archived)
    return _products_out(db, current.tenant_id, products)


@router.post("/products", response_model=ProductOut, status_code=201)
def create_product(body: brand_kits.ProductIn, current: BrandKitWriter, db: DbSession) -> ProductOut:
    product = brand_kits.create_product(db, current.tenant_id, body, actor=current.user)
    _audit(db, current, "added a product", product_id=product.id)
    db.commit()
    return _products_out(db, current.tenant_id, [product])[0]


@router.get("/products/{product_id}", response_model=ProductOut)
def read_product(product_id: str, current: BrandKitViewer, db: DbSession) -> ProductOut:
    product = brand_kits.get_product(db, current.tenant_id, product_id)
    return _products_out(db, current.tenant_id, [product])[0]


@router.patch("/products/{product_id}", response_model=ProductOut)
def update_product(
    product_id: str, body: brand_kits.ProductPatch, current: BrandKitWriter, db: DbSession
) -> ProductOut:
    product = brand_kits.update_product(db, current.tenant_id, product_id, body, actor=current.user)
    _audit(db, current, "edited a product", product_id=product.id)
    db.commit()
    return _products_out(db, current.tenant_id, [product])[0]


@router.delete("/products/{product_id}", response_model=ActionResult)
def archive_product(product_id: str, current: BrandKitWriter, db: DbSession) -> ActionResult:
    product = brand_kits.archive_product(db, current.tenant_id, product_id)
    _audit(db, current, "archived a product", product_id=product.id)
    db.commit()
    return ActionResult(toast=Toast(message="Product archived", kind="success"))


# ── Uploads ────────────────────────────────────────────────────────────────
def upload_byte_limit() -> int:
    """The largest single image any upload kind accepts (raster or SVG)."""
    return max(settings.creative_upload_max_bytes, svg_byte_limit())


def _check_declared_size(request: Request) -> None:
    """Refuse an oversized upload from its Content-Length, before reading it.

    The server enforces the declared length, so a body cannot grow past it.
    """
    declared = request.headers.get("content-length")
    if declared is None:
        raise UploadLengthRequired(UNREADABLE_UPLOAD)
    try:
        size = int(declared)
    except ValueError as exc:
        raise InvalidInputError(UNREADABLE_UPLOAD) from exc
    if size < 0:
        raise InvalidInputError(UNREADABLE_UPLOAD)
    if size > upload_byte_limit() + MULTIPART_OVERHEAD_BYTES:
        mb = max(1, settings.creative_upload_max_bytes // (1024 * 1024))
        raise UploadTooLarge(f"That file is too big. Please upload an image under {mb} MB.")


def _store_upload(
    db: Session, current: CurrentUser, *, filename: str, data: bytes, kind: str
) -> StoredFile:
    stored = uploads.accept_image(
        db,
        tenant_id=current.tenant_id,
        user_id=current.user.id,
        filename=filename,
        data=data,
        kind=kind,
    )
    _audit(db, current, "uploaded a brand image", file_id=stored.id, kind=kind)
    db.commit()
    return stored


@router.post("/uploads", response_model=FileOut, status_code=201)
async def upload_image(request: Request, current: BrandKitWriter, db: DbSession) -> FileOut:
    """Upload a logo, product photo or background as multipart form data.

    Fields: ``kind`` (``logo``, ``product_photo`` or ``background``) and
    ``file``. The image is checked, cleaned and stored by
    :func:`app.services.uploads.accept_image`; nothing else stores uploads.
    """
    _check_declared_size(request)
    try:
        form = await request.form(max_files=1, max_fields=1)
    except StarletteHTTPException as exc:
        raise InvalidInputError(UNREADABLE_UPLOAD) from exc
    try:
        kind = form.get("kind")
        upload = form.get("file")
        if not isinstance(kind, str) or kind not in uploads.UPLOAD_KINDS:
            raise uploads.UploadRejected("bad_kind")
        if not isinstance(upload, UploadFile):
            raise InvalidInputError(NO_FILE_CHOSEN)
        limit = upload_byte_limit()
        data = await upload.read(limit + 1)
        if len(data) > limit:
            raise uploads.UploadRejected("too_large")
        filename = upload.filename or ""
    finally:
        await form.close()
    # Decoding and re-encoding is CPU work; keep it off the event loop.
    stored = await run_in_threadpool(
        _store_upload, db, current, filename=filename, data=data, kind=kind
    )
    return file_out(stored)
