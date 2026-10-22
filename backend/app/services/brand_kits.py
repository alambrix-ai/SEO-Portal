"""Brand kits and products for Creative Studio.

A brand kit holds a workspace's logos, colours and fonts; a product holds
what a creative is about and the claims its copy may make. Both are typed by
marketers, so everything is validated here before it reaches the database:

* Colours go through :func:`normalise_hex` and fonts must be in
  :data:`FONT_IDS`. Both end up in rendered CSS, where escaping does not help.
* Lists (banned phrases, approved claims, product photos) and free text are
  capped in count and length, and hidden characters (controls, zero-width
  and bidirectional overrides) are removed, so what a reviewer reads is what
  the renderer draws.
* Foreign keys do not prove the referenced row belongs to the same
  workspace. Every logo and product photo id is loaded through a
  tenant-filtered query, and must be the right kind of file, before it is
  linked; an id from another workspace is refused exactly like one that does
  not exist.

"Prefill from brand profile" copies suggestions out of the stored brand
profile. That profile was summarised from a website by a model, so it is
treated as untrusted text: cleaned, clipped and returned for a person to
review. Nothing is saved and no model is called.

Every save of a kit or product is kept as a numbered version
(:mod:`app.services.history`): its fields and file ids, never file bytes.
Recording is best effort and never blocks the save. Restoring an old version
re-validates it with the same rules as the editors, and a logo or photo that
has since been deleted (or is not this workspace's) is left out, not linked.
"""
from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable, Iterable, Sequence
from typing import Annotated, Any
from urllib.parse import urlsplit

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, ValidationError
from pydantic_core import PydanticCustomError
from sqlalchemy import Select, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.exceptions import ConflictError, InvalidInputError, NotFoundError
from app.core.logging import get_logger, log_failure
from app.models.content import AssetKind, MarketingAsset
from app.models.creative import FONT_IDS, BrandKit, CreativeJob, FileKind, Product, StoredFile
from app.models.history import SnapshotKind
from app.models.workspace import Organization
from app.services import brand_profile, history
from app.services.history import config as history_config
from app.services.history import store as history_store
from app.services.creative import check_font_id, normalise_hex
from app.services.creative.fonts import FONTS
from app.services.creative.qa import MAX_BANNED_PHRASE_CHARS, MAX_BANNED_PHRASES

log = get_logger(__name__)

# ── Limits ─────────────────────────────────────────────────────────────────
MAX_KITS_PER_WORKSPACE = 20
MAX_PRODUCTS_PER_WORKSPACE = 500
MAX_KIT_NAME_CHARS = 120
MAX_VOICE_CHARS = 500
MAX_DISCLAIMER_CHARS = 500
MAX_PRODUCT_NAME_CHARS = 200
MAX_DESCRIPTION_CHARS = 2000
MAX_URL_CHARS = 1000
MAX_PRICE_TEXT_CHARS = 64
MAX_APPROVED_CLAIMS = 20
MAX_CLAIM_CHARS = 200
MAX_PRODUCT_IMAGES = 10
#: Suggestions returned by prefill; they are only shown, never saved.
MAX_PREFILL_PRODUCTS = 10
MAX_PREFILL_CLAIMS = 20

# ── Messages (shown to non-technical users) ────────────────────────────────
KIT_NOT_FOUND = "That brand kit could not be found"
PRODUCT_NOT_FOUND = "That product could not be found"
LOGO_NOT_FOUND = "That logo could not be found. Please upload it again."
PHOTO_NOT_FOUND = "One of the product photos could not be found. Please upload it again."
TOO_MANY_KITS = (
    f"You can keep up to {MAX_KITS_PER_WORKSPACE} brand kits. "
    "Delete one you no longer use, then try again."
)
TOO_MANY_PRODUCTS = (
    f"You can keep up to {MAX_PRODUCTS_PER_WORKSPACE} products. "
    "Remove one you no longer sell, then try again."
)
DEFAULT_CHANGED = "Someone else changed the default brand kit at the same time. Please try again."
KIT_IN_USE = (
    "This brand kit can't be deleted because designs were made with it. "
    "Edit the kit instead, or make another kit the default."
)
RESTORE_INVALID = "This version can't be restored because its {what} is no longer allowed. {hint}"
RESTORE_INVALID_HINT = "Please make the change in the editor instead."
LOGO_LEFT_OUT = "The logo in that version has since been deleted, so it was left out."
DARK_LOGO_LEFT_OUT = "The logo for dark backgrounds in that version has since been deleted, so it was left out."
PHOTO_LEFT_OUT = "A product photo in that version has since been deleted, so it was left out."
PHOTOS_LEFT_OUT = "{n} product photos in that version have since been deleted, so they were left out."

# C0/C1 controls (the newline is handled per field), zero-width and
# bidirectional-override characters.
_HIDDEN = re.compile("[\x00-\x09\x0b-\x1f\x7f-\x9f​-‏‪-‮⁠-⁤⁦-⁩﻿]")
_SPACES = re.compile(r"[ \t]+")


# ── Text cleaning ──────────────────────────────────────────────────────────
def clean_line(value: Any) -> str:  # noqa: ANN401 - validates untyped input
    """One line of text: hidden characters removed, whitespace collapsed."""
    if not isinstance(value, str):
        raise PydanticCustomError("text", "Please enter text")
    text = unicodedata.normalize("NFC", value)
    text = text.replace("\n", " ").replace("\r", " ").replace("\t", " ")
    return " ".join(_HIDDEN.sub("", text).split())


def clean_block(value: Any) -> str:  # noqa: ANN401 - validates untyped input
    """Multi-line text: hidden characters removed, line breaks kept."""
    if not isinstance(value, str):
        raise PydanticCustomError("text", "Please enter text")
    text = unicodedata.normalize("NFC", value)
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\t", " ")
    lines = [_SPACES.sub(" ", _HIDDEN.sub("", line)).strip() for line in text.split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


Validator = Callable[[Any], Any]


def _text(*, max_chars: int, required: bool = False, multiline: bool = False) -> Validator:
    """A validator that cleans text and enforces its length, in plain English."""

    def validate(value: Any) -> str:  # noqa: ANN401
        text = clean_block(value) if multiline else clean_line(value)
        if required and not text:
            raise PydanticCustomError("required", "Please fill this in")
        if len(text) > max_chars:
            raise PydanticCustomError(
                "too_long", "Please keep this to {n} characters or fewer", {"n": max_chars}
            )
        return text

    return validate


def _colour(value: Any) -> str:  # noqa: ANN401
    try:
        return normalise_hex(value)
    except ValueError as exc:
        raise PydanticCustomError("colour", "Please use a colour like #1a2b3c") from exc


def _font(value: Any) -> str:  # noqa: ANN401
    try:
        return check_font_id(value)
    except ValueError as exc:
        raise PydanticCustomError("font", "Please choose one of the fonts in the list") from exc


def _https_url(value: Any) -> str:  # noqa: ANN401
    """An https link with a host and no embedded user name or password, or ""."""
    text = clean_line(value)
    if not text:
        return ""
    if len(text) > MAX_URL_CHARS:
        raise PydanticCustomError("url", "That link is too long")
    try:
        parts = urlsplit(text)
        host = parts.hostname
    except ValueError as exc:
        raise PydanticCustomError("url", "Please enter a web address starting with https://") from exc
    if parts.scheme.lower() != "https" or not host or parts.username or parts.password:
        raise PydanticCustomError("url", "Please enter a web address starting with https://")
    return text


def _text_list(*, max_items: int, max_chars: int, what: str) -> Validator:
    """A validator for a list of short, non-empty lines; repeats are dropped."""

    def validate(value: Any) -> list[str]:  # noqa: ANN401
        if not isinstance(value, list):
            raise PydanticCustomError("list", "Please enter a list of {what}", {"what": what})
        if len(value) > max_items:
            raise PydanticCustomError(
                "list_too_long", "Please keep to {n} {what} or fewer", {"n": max_items, "what": what}
            )
        out: list[str] = []
        seen: set[str] = set()
        for item in value:
            if not isinstance(item, str):
                raise PydanticCustomError("list_item", "Each of the {what} must be text", {"what": what})
            text = clean_line(item)
            if not text:
                raise PydanticCustomError(
                    "list_item_empty", "Remove the empty line from the {what}", {"what": what}
                )
            if len(text) > max_chars:
                raise PydanticCustomError(
                    "list_item_too_long",
                    "Each of the {what} must be {n} characters or fewer",
                    {"what": what, "n": max_chars},
                )
            if text.casefold() not in seen:
                seen.add(text.casefold())
                out.append(text)
        return out

    return validate


def _file_ids(value: Any) -> list[str]:  # noqa: ANN401
    if not isinstance(value, list):
        raise PydanticCustomError("list", "Please choose the product photos again")
    if len(value) > MAX_PRODUCT_IMAGES:
        raise PydanticCustomError(
            "list_too_long", "Please use {n} product photos or fewer", {"n": MAX_PRODUCT_IMAGES}
        )
    out: list[str] = []
    for item in value:
        file_id = parse_id(item)
        if file_id is None:
            raise PydanticCustomError("file_id", "Please choose the product photos again")
        if file_id not in out:
            out.append(file_id)
    return out


def _logo_id(value: Any) -> str | None:  # noqa: ANN401
    """A logo file id, or ``None`` (no logo)."""
    if value is None or value == "":
        return None
    file_id = parse_id(value)
    if file_id is None:
        raise PydanticCustomError("file_id", "Please upload the logo again")
    return file_id


def _optional(validate: Validator) -> Validator:
    """``validate``, but ``None`` passes through (a patch leaving the field alone)."""

    def wrapper(value: Any) -> Any:  # noqa: ANN401
        return None if value is None else validate(value)

    return wrapper


_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def parse_id(value: Any) -> str | None:  # noqa: ANN401
    """A canonical lower-case UUID string, or ``None`` if ``value`` is not one."""
    if not isinstance(value, str) or len(value) != 36:
        return None
    candidate = value.lower()
    return candidate if _UUID.fullmatch(candidate) else None


# ── Field types ────────────────────────────────────────────────────────────
# Validation happens in one BeforeValidator per field, so an error is
# reported against the field itself, with a message a marketer can act on.
_KIT_NAME = _text(max_chars=MAX_KIT_NAME_CHARS, required=True)
_PRODUCT_NAME = _text(max_chars=MAX_PRODUCT_NAME_CHARS, required=True)
_VOICE = _text(max_chars=MAX_VOICE_CHARS, multiline=True)
_DISCLAIMER = _text(max_chars=MAX_DISCLAIMER_CHARS, multiline=True)
_DESCRIPTION = _text(max_chars=MAX_DESCRIPTION_CHARS, multiline=True)
_PRICE_TEXT = _text(max_chars=MAX_PRICE_TEXT_CHARS)
_BANNED = _text_list(
    max_items=MAX_BANNED_PHRASES, max_chars=MAX_BANNED_PHRASE_CHARS, what="banned phrases"
)
_CLAIMS = _text_list(max_items=MAX_APPROVED_CLAIMS, max_chars=MAX_CLAIM_CHARS, what="claims")

Colour = Annotated[str, BeforeValidator(_colour)]
FontId = Annotated[str, BeforeValidator(_font)]
KitName = Annotated[str, BeforeValidator(_KIT_NAME)]
ProductName = Annotated[str, BeforeValidator(_PRODUCT_NAME)]
Voice = Annotated[str, BeforeValidator(_VOICE)]
Disclaimer = Annotated[str, BeforeValidator(_DISCLAIMER)]
Description = Annotated[str, BeforeValidator(_DESCRIPTION)]
PriceText = Annotated[str, BeforeValidator(_PRICE_TEXT)]
HttpsUrl = Annotated[str, BeforeValidator(_https_url)]
BannedPhrases = Annotated[list[str], BeforeValidator(_BANNED)]
ApprovedClaims = Annotated[list[str], BeforeValidator(_CLAIMS)]
FileIds = Annotated[list[str], BeforeValidator(_file_ids)]
LogoId = Annotated[str | None, BeforeValidator(_logo_id)]

# The same, for patches: ``None`` means "leave this field as it is".
OptColour = Annotated[str | None, BeforeValidator(_optional(_colour))]
OptFontId = Annotated[str | None, BeforeValidator(_optional(_font))]
OptKitName = Annotated[str | None, BeforeValidator(_optional(_KIT_NAME))]
OptProductName = Annotated[str | None, BeforeValidator(_optional(_PRODUCT_NAME))]
OptVoice = Annotated[str | None, BeforeValidator(_optional(_VOICE))]
OptDisclaimer = Annotated[str | None, BeforeValidator(_optional(_DISCLAIMER))]
OptDescription = Annotated[str | None, BeforeValidator(_optional(_DESCRIPTION))]
OptPriceText = Annotated[str | None, BeforeValidator(_optional(_PRICE_TEXT))]
OptHttpsUrl = Annotated[str | None, BeforeValidator(_optional(_https_url))]
OptBannedPhrases = Annotated[list[str] | None, BeforeValidator(_optional(_BANNED))]
OptApprovedClaims = Annotated[list[str] | None, BeforeValidator(_optional(_CLAIMS))]
OptFileIds = Annotated[list[str] | None, BeforeValidator(_optional(_file_ids))]


# ── Request schemas ────────────────────────────────────────────────────────
class _Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class KitIn(_Input):
    """A new brand kit. Defaults match the model's."""

    name: KitName
    is_default: bool = False
    logo_file_id: LogoId = None
    logo_dark_file_id: LogoId = None
    primary_color: Colour = "#1f2937"
    secondary_color: Colour = "#4b5563"
    accent_color: Colour = "#2563eb"
    text_color: Colour = "#111827"
    background_color: Colour = "#ffffff"
    heading_font: FontId = "inter"
    body_font: FontId = "inter"
    voice: Voice = ""
    banned_phrases: BannedPhrases = Field(default_factory=list)
    disclaimer: Disclaimer = ""


class KitPatch(_Input):
    """Changes to a brand kit; only the fields sent are changed.

    A logo sent as ``null`` is removed; any other field sent as ``null`` is
    left as it is.
    """

    name: OptKitName = None
    logo_file_id: LogoId = None
    logo_dark_file_id: LogoId = None
    primary_color: OptColour = None
    secondary_color: OptColour = None
    accent_color: OptColour = None
    text_color: OptColour = None
    background_color: OptColour = None
    heading_font: OptFontId = None
    body_font: OptFontId = None
    voice: OptVoice = None
    banned_phrases: OptBannedPhrases = None
    disclaimer: OptDisclaimer = None


class ProductIn(_Input):
    name: ProductName
    description: Description = ""
    url: HttpsUrl = ""
    price_text: PriceText = ""
    approved_claims: ApprovedClaims = Field(default_factory=list)
    image_file_ids: FileIds = Field(default_factory=list)
    disclaimer: Disclaimer = ""


class ProductPatch(_Input):
    """Changes to a product; only the fields sent (and not ``null``) are changed."""

    name: OptProductName = None
    description: OptDescription = None
    url: OptHttpsUrl = None
    price_text: OptPriceText = None
    approved_claims: OptApprovedClaims = None
    image_file_ids: OptFileIds = None
    disclaimer: OptDisclaimer = None
    is_archived: bool | None = None


#: Fields a patch may set to ``null`` (meaning "remove"); for every other
#: field ``null`` means "leave as it is".
_NULLABLE_KIT_FIELDS = frozenset({"logo_file_id", "logo_dark_file_id"})


def _changes(patch: BaseModel, nullable: frozenset[str] = frozenset()) -> dict[str, Any]:
    sent = patch.model_dump(exclude_unset=True)
    return {k: v for k, v in sent.items() if v is not None or k in nullable}


# ── Tenant-filtered loading ────────────────────────────────────────────────
def file_query(tenant_id: str, file_id: str, kinds: Iterable[str] | None = None) -> Select[tuple[StoredFile]]:
    """The one row ``file_id`` in ``tenant_id``, optionally of one of ``kinds``."""
    stmt = select(StoredFile).where(StoredFile.id == file_id, StoredFile.tenant_id == tenant_id)
    if kinds is not None:
        stmt = stmt.where(StoredFile.kind.in_(tuple(kinds)))
    return stmt


def get_file(db: Session, tenant_id: str, file_id: str, kinds: Iterable[str] | None = None) -> StoredFile | None:
    """A stored file of this workspace (and kind), or ``None``. Never loads the bytes."""
    canonical = parse_id(file_id)
    if canonical is None:
        return None
    return db.execute(file_query(tenant_id, canonical, kinds)).scalar_one_or_none()


def owned_file_ids(db: Session, tenant_id: str, file_ids: Sequence[str], kind: str) -> set[str]:
    """Which of ``file_ids`` are files of ``kind`` in ``tenant_id``."""
    wanted = [i for i in (parse_id(f) for f in file_ids) if i is not None]
    if not wanted:
        return set()
    rows = db.execute(
        select(StoredFile.id).where(
            StoredFile.tenant_id == tenant_id,
            StoredFile.kind == kind,
            StoredFile.id.in_(wanted),
        )
    ).scalars()
    return {str(r) for r in rows}


def get_kit(db: Session, tenant_id: str, kit_id: str) -> BrandKit:
    canonical = parse_id(kit_id)
    kit = None
    if canonical is not None:
        kit = db.execute(
            select(BrandKit).where(BrandKit.id == canonical, BrandKit.tenant_id == tenant_id)
        ).scalar_one_or_none()
    if kit is None:
        raise NotFoundError(KIT_NOT_FOUND)
    return kit


def get_product(db: Session, tenant_id: str, product_id: str) -> Product:
    canonical = parse_id(product_id)
    product = None
    if canonical is not None:
        product = db.execute(
            select(Product).where(Product.id == canonical, Product.tenant_id == tenant_id)
        ).scalar_one_or_none()
    if product is None:
        raise NotFoundError(PRODUCT_NOT_FOUND)
    return product


def _require_logo(db: Session, tenant_id: str, file_id: str | None, field: str) -> str | None:
    """``file_id`` if it is a logo in this workspace; 422 otherwise."""
    if file_id is None:
        return None
    if get_file(db, tenant_id, file_id, (FileKind.LOGO,)) is None:
        log.info("Logo link refused: tenant=%s field=%s", tenant_id, field)
        raise InvalidInputError(LOGO_NOT_FOUND, details={"field": field})
    return file_id


def _require_photos(db: Session, tenant_id: str, file_ids: Sequence[str]) -> list[str]:
    """``file_ids`` if every one is a product photo in this workspace; 422 otherwise."""
    if not file_ids:
        return []
    owned = owned_file_ids(db, tenant_id, file_ids, FileKind.PRODUCT_PHOTO)
    if any(f not in owned for f in file_ids):
        log.info(
            "Product photo link refused: tenant=%s sent=%s found=%s", tenant_id, len(file_ids), len(owned)
        )
        raise InvalidInputError(PHOTO_NOT_FOUND, details={"field": "image_file_ids"})
    return list(file_ids)


# ── Brand kits ─────────────────────────────────────────────────────────────
def list_kits(db: Session, tenant_id: str) -> list[BrandKit]:
    return list(
        db.execute(
            select(BrandKit)
            .where(BrandKit.tenant_id == tenant_id)
            .order_by(BrandKit.is_default.desc(), BrandKit.name, BrandKit.id)
            .limit(MAX_KITS_PER_WORKSPACE)
        ).scalars()
    )


def _count(db: Session, model: type[BrandKit] | type[Product], tenant_id: str) -> int:
    stmt = select(func.count()).select_from(model).where(model.tenant_id == tenant_id)
    if model is Product:
        stmt = stmt.where(Product.is_archived.is_(False))
    return int(db.execute(stmt).scalar_one())


def _clear_default(db: Session, tenant_id: str, keep_id: str | None) -> None:
    stmt = update(BrandKit).where(BrandKit.tenant_id == tenant_id, BrandKit.is_default.is_(True))
    if keep_id is not None:
        stmt = stmt.where(BrandKit.id != keep_id)
    db.execute(stmt.values(is_default=False).execution_options(synchronize_session="fetch"))


def _flush_default(db: Session, tenant_id: str) -> None:
    """Flush, turning a lost race on the one-default index into a friendly 409."""
    try:
        with db.begin_nested():
            db.flush()
    except IntegrityError as exc:
        log_failure(log, "Default brand kit race", exc, tenant_id=tenant_id)
        raise ConflictError(DEFAULT_CHANGED) from exc


def create_kit(db: Session, tenant_id: str, data: KitIn, *, actor: Any = None) -> BrandKit:  # noqa: ANN401
    """Create a kit. The workspace's first kit becomes its default.

    ``actor`` is the User saving it, credited in the kit's version history.
    """
    existing = _count(db, BrandKit, tenant_id)
    if existing >= MAX_KITS_PER_WORKSPACE:
        raise ConflictError(TOO_MANY_KITS)
    values = data.model_dump()
    values["logo_file_id"] = _require_logo(db, tenant_id, values["logo_file_id"], "logo_file_id")
    values["logo_dark_file_id"] = _require_logo(
        db, tenant_id, values["logo_dark_file_id"], "logo_dark_file_id"
    )
    make_default = bool(values.pop("is_default")) or existing == 0
    if make_default:
        _clear_default(db, tenant_id, None)
    kit = BrandKit(tenant_id=tenant_id, is_default=make_default, **values)
    db.add(kit)
    _flush_default(db, tenant_id)
    log.info("Brand kit created: tenant=%s kit=%s default=%s", tenant_id, kit.id, make_default)
    _record_version(db, tenant_id, SnapshotKind.BRAND_KIT, kit.id, kit_document(kit), actor)
    return kit


def update_kit(
    db: Session, tenant_id: str, kit_id: str, patch: KitPatch, *, actor: Any = None  # noqa: ANN401
) -> BrandKit:
    kit = get_kit(db, tenant_id, kit_id)
    changes = _changes(patch, _NULLABLE_KIT_FIELDS)
    for field in ("logo_file_id", "logo_dark_file_id"):
        if field in changes:
            changes[field] = _require_logo(db, tenant_id, changes[field], field)
    for key, value in changes.items():
        setattr(kit, key, value)
    db.flush()
    log.info("Brand kit updated: tenant=%s kit=%s fields=%s", tenant_id, kit.id, sorted(changes))
    _record_version(db, tenant_id, SnapshotKind.BRAND_KIT, kit.id, kit_document(kit), actor)
    return kit


def set_default_kit(db: Session, tenant_id: str, kit_id: str) -> BrandKit:
    kit = get_kit(db, tenant_id, kit_id)
    if not kit.is_default:
        _clear_default(db, tenant_id, kit.id)
        kit.is_default = True
        _flush_default(db, tenant_id)
        log.info("Default brand kit set: tenant=%s kit=%s", tenant_id, kit.id)
    return kit


def kit_in_use(db: Session, tenant_id: str, kit_id: str) -> bool:
    """Whether any creative job or design of this workspace names the kit.

    Designs keep the kit id in ``detail``; redrawing, revising or restoring
    one needs the kit, so a kit in use is never deleted.
    """
    job = db.execute(
        select(CreativeJob.id)
        .where(CreativeJob.tenant_id == tenant_id, CreativeJob.brand_kit_id == kit_id)
        .limit(1)
    ).scalar_one_or_none()
    if job is not None:
        return True
    asset = db.execute(
        select(MarketingAsset.id)
        .where(
            MarketingAsset.tenant_id == tenant_id,
            MarketingAsset.kind == AssetKind.CREATIVE,
            MarketingAsset.detail["brand_kit_id"].astext == kit_id,
        )
        .limit(1)
    ).scalar_one_or_none()
    return asset is not None


def _next_default(db: Session, tenant_id: str, excluding: str) -> BrandKit | None:
    """The kit that becomes the default when the default one is deleted."""
    return db.execute(
        select(BrandKit)
        .where(BrandKit.tenant_id == tenant_id, BrandKit.id != excluding)
        .order_by(BrandKit.name, BrandKit.id)
        .limit(1)
    ).scalar_one_or_none()


def delete_kit(db: Session, tenant_id: str, kit_id: str) -> str:
    """Delete a kit and return its id.

    Refused (409) while a creative job or design uses it. Deleting the
    default kit makes another kit (the first by name) the default, so a
    workspace with kits always has one.
    """
    kit = get_kit(db, tenant_id, kit_id)
    deleted_id = kit.id
    if kit_in_use(db, tenant_id, deleted_id):
        log.info("Brand kit delete refused, in use: tenant=%s kit=%s", tenant_id, deleted_id)
        raise ConflictError(KIT_IN_USE, details={"reason": "kit_in_use"})
    was_default = bool(kit.is_default)
    db.delete(kit)
    db.flush()
    successor = _next_default(db, tenant_id, deleted_id) if was_default else None
    if successor is not None:
        _clear_default(db, tenant_id, successor.id)
        successor.is_default = True
        _flush_default(db, tenant_id)
    log.info(
        "Brand kit deleted: tenant=%s kit=%s new_default=%s",
        tenant_id, deleted_id, successor.id if successor is not None else "",
    )
    return deleted_id


# ── Products ───────────────────────────────────────────────────────────────
def list_products(db: Session, tenant_id: str, *, include_archived: bool = False) -> list[Product]:
    stmt = select(Product).where(Product.tenant_id == tenant_id)
    if not include_archived:
        stmt = stmt.where(Product.is_archived.is_(False))
    return list(
        db.execute(stmt.order_by(Product.name, Product.id).limit(MAX_PRODUCTS_PER_WORKSPACE)).scalars()
    )


def visible_image_ids(db: Session, tenant_id: str, products: Sequence[Product]) -> dict[str, list[str]]:
    """Each product's photo ids that still exist in this workspace, in order.

    A deleted file, or one that is somehow not this workspace's, is skipped
    rather than shown.
    """
    wanted: list[str] = []
    for product in products:
        wanted.extend(i for i in (product.image_file_ids or []) if isinstance(i, str))
    owned = owned_file_ids(db, tenant_id, wanted, FileKind.PRODUCT_PHOTO)
    return {
        product.id: [i for i in (product.image_file_ids or []) if isinstance(i, str) and i in owned]
        for product in products
    }


def create_product(db: Session, tenant_id: str, data: ProductIn, *, actor: Any = None) -> Product:  # noqa: ANN401
    if _count(db, Product, tenant_id) >= MAX_PRODUCTS_PER_WORKSPACE:
        raise ConflictError(TOO_MANY_PRODUCTS)
    values = data.model_dump()
    values["image_file_ids"] = _require_photos(db, tenant_id, values["image_file_ids"])
    product = Product(tenant_id=tenant_id, **values)
    db.add(product)
    db.flush()
    log.info(
        "Product created: tenant=%s product=%s claims=%s photos=%s",
        tenant_id,
        product.id,
        len(product.approved_claims),
        len(product.image_file_ids),
    )
    _record_version(db, tenant_id, SnapshotKind.PRODUCT, product.id, product_document(product), actor)
    return product


def update_product(
    db: Session, tenant_id: str, product_id: str, patch: ProductPatch, *, actor: Any = None  # noqa: ANN401
) -> Product:
    product = get_product(db, tenant_id, product_id)
    changes = _changes(patch)
    if "image_file_ids" in changes:
        changes["image_file_ids"] = _require_photos(db, tenant_id, changes["image_file_ids"])
    if changes.get("is_archived") is False and product.is_archived:
        if _count(db, Product, tenant_id) >= MAX_PRODUCTS_PER_WORKSPACE:
            raise ConflictError(TOO_MANY_PRODUCTS)
    for key, value in changes.items():
        setattr(product, key, value)
    db.flush()
    log.info("Product updated: tenant=%s product=%s fields=%s", tenant_id, product.id, sorted(changes))
    _record_version(db, tenant_id, SnapshotKind.PRODUCT, product.id, product_document(product), actor)
    return product


def archive_product(db: Session, tenant_id: str, product_id: str) -> Product:
    """Hide a product from new creatives. Past creatives still refer to it."""
    product = get_product(db, tenant_id, product_id)
    product.is_archived = True
    db.flush()
    log.info("Product archived: tenant=%s product=%s", tenant_id, product.id)
    return product


# ── Version history ────────────────────────────────────────────────────────
#: What a version of a kit or product holds: the fields its editor saves,
#: with file ids but never file bytes.
KIT_VERSION_FIELDS: tuple[str, ...] = tuple(KitIn.model_fields)
PRODUCT_VERSION_FIELDS: tuple[str, ...] = (*ProductIn.model_fields, "is_archived")
#: ``db.info`` key where a restore leaves plain-English notes (files left out)
#: for whoever answers the request.
RESTORE_NOTES_KEY = "history_restore_notes"
#: Validation errors raised by this module's own validators carry a message
#: written for a marketer; any other error gets the generic hint.
_OWN_ERROR_TYPES = frozenset({
    "text", "required", "too_long", "colour", "font", "url", "list", "list_too_long",
    "list_item", "list_item_empty", "list_item_too_long", "file_id",
})


def _version_value(value: Any) -> Any:  # noqa: ANN401 - a column value
    return list(value) if isinstance(value, list) else value


def kit_document(kit: BrandKit) -> dict[str, Any]:
    """The kit as one version: its fields and logo ids."""
    return {field: _version_value(getattr(kit, field)) for field in KIT_VERSION_FIELDS}


def product_document(product: Product) -> dict[str, Any]:
    """The product as one version: its fields and photo ids."""
    return {field: _version_value(getattr(product, field)) for field in PRODUCT_VERSION_FIELDS}


def _record_version(
    db: Session, tenant_id: str, kind: str, subject_id: str, document: dict[str, Any], actor: Any  # noqa: ANN401
) -> None:
    """Keep ``document`` as the next version. Never fails the save: errors are logged."""

    def record() -> None:
        org = db.get(Organization, tenant_id)
        if org is None or org.id != tenant_id:
            raise LookupError("workspace not loaded")
        history.record_version(db, org, kind, subject_id, document, actor=actor)

    history_store.guarded(db, kind, record)


def _revalidate(schema: type[BaseModel], kind: str, document: dict[str, Any]) -> BaseModel:
    """A stored version, checked by the same rules as the editor (it may predate them)."""
    fields = {k: v for k, v in document.items() if k in schema.model_fields}
    try:
        return schema.model_validate(fields)
    except ValidationError as exc:
        error = exc.errors()[0]
        loc = error.get("loc") or ()
        key = str(loc[0]) if loc else ""
        labels = {spec.key: spec.label for spec in history_config.fields_for(kind)}
        hint = str(error.get("msg", "")) + "." if error.get("type") in _OWN_ERROR_TYPES else RESTORE_INVALID_HINT
        log.info("Version restore refused: kind=%s field=%s reason=%s", kind, key, error.get("type"))
        raise InvalidInputError(
            RESTORE_INVALID.format(what=labels.get(key, "a setting").lower(), hint=hint),
            details={"field": key},
        ) from exc


def _leave_notes(db: Session, notes: list[str]) -> None:
    if notes:
        db.info.setdefault(RESTORE_NOTES_KEY, []).extend(notes)


@history.register_restorer(SnapshotKind.BRAND_KIT)
def restore_kit_version(
    db: Session, org: Organization, subject_id: str, document: dict[str, Any], *, user: Any  # noqa: ANN401
) -> dict[str, Any]:
    """Write an old kit version back; the history service records the new version.

    Whether the kit is the default is not restored: that is changed only by
    "make default", so a restore never leaves a workspace with two or none.
    """
    kit = get_kit(db, org.id, subject_id)
    values = _revalidate(KitIn, SnapshotKind.BRAND_KIT, document).model_dump(exclude={"is_default"})
    notes: list[str] = []
    for field, note in (("logo_file_id", LOGO_LEFT_OUT), ("logo_dark_file_id", DARK_LOGO_LEFT_OUT)):
        file_id = values[field]
        if file_id is not None and get_file(db, org.id, file_id, (FileKind.LOGO,)) is None:
            values[field] = None
            notes.append(note)
    for key, value in values.items():
        setattr(kit, key, value)
    db.flush()
    _leave_notes(db, notes)
    log.info("Brand kit restored: tenant=%s kit=%s files_left_out=%s", org.id, kit.id, len(notes))
    return kit_document(kit)


@history.register_restorer(SnapshotKind.PRODUCT)
def restore_product_version(
    db: Session, org: Organization, subject_id: str, document: dict[str, Any], *, user: Any  # noqa: ANN401
) -> dict[str, Any]:
    """Write an old product version back; the history service records the new version."""
    product = get_product(db, org.id, subject_id)
    values = _revalidate(ProductIn, SnapshotKind.PRODUCT, document).model_dump()
    archived = document.get("is_archived", product.is_archived)
    if not isinstance(archived, bool):
        archived = product.is_archived
    if product.is_archived and not archived and _count(db, Product, org.id) >= MAX_PRODUCTS_PER_WORKSPACE:
        raise ConflictError(TOO_MANY_PRODUCTS)
    wanted: list[str] = values["image_file_ids"]
    owned = owned_file_ids(db, org.id, wanted, FileKind.PRODUCT_PHOTO)
    values["image_file_ids"] = [i for i in wanted if i in owned]
    left_out = len(wanted) - len(values["image_file_ids"])
    for key, value in values.items():
        setattr(product, key, value)
    product.is_archived = archived
    db.flush()
    if left_out:
        _leave_notes(db, [PHOTO_LEFT_OUT if left_out == 1 else PHOTOS_LEFT_OUT.format(n=left_out)])
    log.info("Product restored: tenant=%s product=%s photos_left_out=%s", org.id, product.id, left_out)
    return product_document(product)


# ── Prefill from the brand profile ─────────────────────────────────────────
def _suggestions(values: Any, *, max_items: int, max_chars: int) -> list[str]:  # noqa: ANN401
    """Clean, clipped, de-duplicated lines from an untrusted profile list."""
    if not isinstance(values, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, str):
            continue
        text = clean_line(value)[:max_chars].strip()
        if text and text.casefold() not in seen:
            seen.add(text.casefold())
            out.append(text)
        if len(out) >= max_items:
            break
    return out


def _suggested_text(value: Any, limit: int) -> str:  # noqa: ANN401
    return clean_line(value)[:limit].strip() if isinstance(value, str) else ""


def prefill(org: Organization) -> dict[str, Any]:
    """Kit and product suggestions from the stored brand profile.

    The profile is model output summarised from a website, so every value is
    cleaned and clipped to the same limits a person's input gets, and the
    result is only ever shown for review. Claims in particular are
    *suggestions*: a person must approve them by saving a product.
    """
    profile = brand_profile.get(org)
    if not profile:
        return {"available": False, "kit": None, "products": [], "claim_suggestions": []}
    name = _suggested_text(profile.get("business_name"), MAX_KIT_NAME_CHARS) or _suggested_text(
        org.name, MAX_KIT_NAME_CHARS
    )
    products = _suggestions(
        profile.get("products_services"), max_items=MAX_PREFILL_PRODUCTS, max_chars=MAX_PRODUCT_NAME_CHARS
    )
    propositions = profile.get("value_propositions")
    differentiators = profile.get("differentiators")
    claims = _suggestions(
        [
            *(propositions if isinstance(propositions, list) else []),
            *(differentiators if isinstance(differentiators, list) else []),
        ],
        max_items=MAX_PREFILL_CLAIMS,
        max_chars=MAX_CLAIM_CHARS,
    )
    return {
        "available": True,
        "kit": {
            "name": name or "My brand",
            "voice": _suggested_text(profile.get("tone"), MAX_VOICE_CHARS),
        },
        "products": [{"name": p} for p in products],
        "claim_suggestions": claims,
    }


def options() -> dict[str, Any]:
    """What the kit and product editors may offer, and the limits they enforce."""
    return {
        "fonts": [{"id": f, "label": FONTS[f].family if f in FONTS else f} for f in FONT_IDS],
        "limits": {
            "kits": MAX_KITS_PER_WORKSPACE,
            "products": MAX_PRODUCTS_PER_WORKSPACE,
            "kit_name_chars": MAX_KIT_NAME_CHARS,
            "voice_chars": MAX_VOICE_CHARS,
            "disclaimer_chars": MAX_DISCLAIMER_CHARS,
            "banned_phrases": MAX_BANNED_PHRASES,
            "banned_phrase_chars": MAX_BANNED_PHRASE_CHARS,
            "product_name_chars": MAX_PRODUCT_NAME_CHARS,
            "description_chars": MAX_DESCRIPTION_CHARS,
            "price_text_chars": MAX_PRICE_TEXT_CHARS,
            "approved_claims": MAX_APPROVED_CLAIMS,
            "claim_chars": MAX_CLAIM_CHARS,
            "product_images": MAX_PRODUCT_IMAGES,
        },
    }
