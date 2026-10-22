"""Safe image intake for Creative Studio: uploads and AI-generated pictures.

Nothing a person uploads, and nothing a model returns, is stored as it came.
Every image is:

* capped in bytes before it is looked at;
* identified by its magic bytes, and for uploads the file extension must
  name the same type (a ``.png`` that is really a JPEG is refused);
* opened by Pillow with ``MAX_IMAGE_PIXELS`` set from
  ``CREATIVE_UPLOAD_MAX_PIXELS`` and ``DecompressionBombWarning`` raised as
  an error, then checked against the pixel cap again before decoding;
* refused if animated (GIF is refused outright, APNG and animated WebP too)
  or truncated;
* re-encoded from its pixels alone, so EXIF (including GPS position), ICC
  profiles and text chunks are not carried over;
* hashed (SHA-256 of the stored bytes) and stored with who uploaded it and
  where it came from, so any file can be audited or purged;
* counted against the workspace's storage cap before it is written.

SVG uploads are sanitised and rasterised by :mod:`app.services.creative.svg`
and stored as PNG. An SVG is never stored.
"""
from __future__ import annotations

import hashlib
import io
import unicodedata
import warnings
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass

from PIL import Image, ImageFile, ImageOps, UnidentifiedImageError
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.exceptions import AppError, ConflictError, InvalidInputError
from app.core.logging import get_logger, log_failure
from app.db.session import session_scope
from app.models.creative import STORED_FILE_MIMES, FileKind, FileSource, StoredFile
from app.services.creative.svg import SvgRejected, looks_like_svg, svg_byte_limit, svg_to_png
from app.services.storage import get_storage, tenant_usage_bytes

log = get_logger(__name__)

# ── Types ──────────────────────────────────────────────────────────────────
PNG = "image/png"
JPEG = "image/jpeg"
WEBP = "image/webp"
SVG = "image/svg+xml"

#: File extension → the type its bytes must be.
EXTENSION_TYPES: dict[str, str] = {
    ".png": PNG,
    ".jpg": JPEG,
    ".jpeg": JPEG,
    ".webp": WEBP,
    ".svg": SVG,
}
#: Pillow's format names for each raster type we accept. Many phone cameras
#: write JPEGs with an extra preview frame, which Pillow reports as "MPO";
#: only the first frame is decoded and kept.
_PIL_FORMATS: dict[str, tuple[str, ...]] = {
    PNG: ("PNG",),
    JPEG: ("JPEG", "MPO"),
    WEBP: ("WEBP",),
}
_SUFFIXES: dict[str, str] = {PNG: ".png", JPEG: ".jpg", WEBP: ".webp"}
#: Kinds a person may upload. Renders and previews are made by the system.
UPLOAD_KINDS: frozenset[str] = frozenset(
    {FileKind.LOGO, FileKind.PRODUCT_PHOTO, FileKind.BACKGROUND}
)
#: Longest file name accepted, before it is cleaned for display.
MAX_FILENAME_CHARS = 255
_DISPLAY_NAME_CHARS = 200
_JPEG_QUALITY = 90
_WEBP_QUALITY = 90

# ── Messages (shown to non-technical users) ────────────────────────────────
_MESSAGES: dict[str, str] = {
    "empty": "That file is empty. Please choose an image to upload.",
    "too_large": "That file is too big. Please upload an image under {mb} MB.",
    "unsupported": "Please upload a PNG, JPEG, WebP or SVG image.",
    "gif": "GIF images aren't supported. Please upload a PNG, JPEG or WebP image.",
    "mismatch": (
        "This file's name doesn't match what's inside it. "
        "Please upload a real PNG, JPEG, WebP or SVG image."
    ),
    "too_many_pixels": "That image is too large. Please use a smaller image.",
    "animated": "Animated images aren't supported. Please upload a still image.",
    "damaged": "We couldn't read that image. It may be damaged or incomplete.",
    "svg_too_large": "That SVG file is too big. Please upload one under {kb} KB, or a PNG.",
    "svg_invalid": (
        "We couldn't use that SVG file. Please export it again as a plain SVG, "
        "or upload a PNG instead."
    ),
    "svg_too_complex": (
        "That SVG is too complex to use. Please simplify it, or upload a PNG instead."
    ),
    "svg_busy": "We're busy preparing other images. Please try again in a moment.",
    "bad_kind": "That kind of file can't be uploaded here.",
}
STORAGE_FULL_MESSAGE = (
    "Your workspace has run out of space for images. Remove files you no longer "
    "need, or ask your administrator for more space."
)

_SVG_REASONS: dict[str, str] = {
    "too_large": "svg_too_large",
    "invalid": "svg_invalid",
    "unsafe": "svg_invalid",
    "render_failed": "svg_invalid",
    "too_complex": "svg_too_complex",
    "busy": "svg_busy",
}


class UploadRejected(InvalidInputError):
    """An image was refused. ``details["reason"]`` is a stable machine key."""

    code = "upload_rejected"

    def __init__(self, reason: str) -> None:
        message = _MESSAGES[reason].format(
            mb=max(1, settings.creative_upload_max_bytes // (1024 * 1024)),
            kb=max(1, svg_byte_limit() // 1024),
        )
        super().__init__(message, details={"reason": reason})
        self.reason = reason


class StorageFull(ConflictError):
    """The workspace's storage cap would be exceeded."""

    code = "storage_full"


STORAGE_BUSY_MESSAGE = "We're busy saving other images. Please try again in a moment."


class StorageBusy(AppError):
    """The workspace's storage lock was not free in time. Safe to retry."""

    status_code = 429
    code = "busy"

    def __init__(self) -> None:
        super().__init__(STORAGE_BUSY_MESSAGE, details={"reason": "busy"})


@dataclass(frozen=True)
class CleanImage:
    """An image ready to store: re-encoded, metadata-free, measured and hashed."""

    data: bytes
    mime: str
    width: int
    height: int
    sha256: str

    @property
    def size_bytes(self) -> int:
        return len(self.data)


# ── Sniffing ───────────────────────────────────────────────────────────────
def sniff(data: bytes) -> str | None:
    """The type ``data`` really is, from its first bytes; ``None`` if unknown.

    GIF is recognised so it can be refused with its own message.
    """
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return PNG
    if data.startswith(b"\xff\xd8\xff"):
        return JPEG
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return WEBP
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if looks_like_svg(data):
        return SVG
    return None


def _extension(filename: str) -> str:
    base = filename.replace("\\", "/").rsplit("/", 1)[-1]
    dot = base.rfind(".")
    return base[dot:].lower() if dot > 0 else ""


def display_name_for(filename: str, mime: str) -> str:
    """A safe display name: no path, no control characters, the stored type's extension.

    Never used as a path; it is only ever shown.
    """
    base = unicodedata.normalize("NFKC", filename.replace("\\", "/").rsplit("/", 1)[-1])
    cleaned = "".join(ch for ch in base if not unicodedata.category(ch).startswith("C"))
    stem = cleaned.rsplit(".", 1)[0].strip(" .") if "." in cleaned else cleaned.strip(" .")
    suffix = _SUFFIXES.get(mime, "")
    stem = stem[: _DISPLAY_NAME_CHARS - len(suffix)] or "image"
    return f"{stem}{suffix}"


# ── Raster cleaning ────────────────────────────────────────────────────────
def _open_checked(data: bytes, expected: str) -> Image.Image:
    """Open, verify and fully decode ``data`` under the pixel and bomb guards."""
    max_pixels = settings.creative_upload_max_pixels
    Image.MAX_IMAGE_PIXELS = max_pixels
    if ImageFile.LOAD_TRUNCATED_IMAGES:
        # Something in the process relaxed Pillow; a truncated file would pass.
        raise UploadRejected("damaged")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as probe:
                if probe.format not in _PIL_FORMATS[expected]:
                    raise UploadRejected("mismatch")
                # Checked here as well as by Pillow: warnings filters are
                # process-wide, so this is the guard that cannot be raced.
                if probe.width * probe.height > max_pixels:
                    raise UploadRejected("too_many_pixels")
                animated = getattr(probe, "is_animated", False)
                if probe.format != "MPO" and (animated or getattr(probe, "n_frames", 1) > 1):
                    raise UploadRejected("animated")
                probe.verify()
            image = Image.open(io.BytesIO(data))
            image.load()
    except UploadRejected:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise UploadRejected("too_many_pixels") from exc
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError, EOFError) as exc:
        log_failure(log, "Image could not be decoded", exc, mime=expected)
        raise UploadRejected("damaged") from exc
    return image


def _reencode(image: Image.Image, mime: str) -> CleanImage:
    """Write ``image``'s pixels, and nothing else, as ``mime``."""
    image = ImageOps.exif_transpose(image)
    has_alpha = image.mode in ("RGBA", "LA", "PA") or (
        image.mode == "P" and "transparency" in image.info
    )
    if mime == JPEG:
        mode = "L" if image.mode == "L" else "RGB"
    elif image.mode in ("L", "RGB", "RGBA", "LA") and mime == PNG:
        mode = image.mode
    else:
        mode = "RGBA" if has_alpha else "RGB"
    converted = image.convert(mode) if image.mode != mode else image
    # A fresh image carries no info dict: no EXIF, ICC profile or text chunks.
    clean = Image.new(mode, converted.size)
    clean.paste(converted)

    out = io.BytesIO()
    if mime == PNG:
        clean.save(out, format="PNG", optimize=True)
    elif mime == JPEG:
        clean.save(out, format="JPEG", quality=_JPEG_QUALITY, optimize=True)
    else:
        clean.save(out, format="WEBP", quality=_WEBP_QUALITY)
    data = out.getvalue()
    if sniff(data) != mime:
        raise UploadRejected("damaged")
    return CleanImage(
        data=data,
        mime=mime,
        width=clean.width,
        height=clean.height,
        sha256=hashlib.sha256(data).hexdigest(),
    )


def _clean_raster(data: bytes, mime: str, *, max_bytes: int) -> CleanImage:
    image = _open_checked(data, mime)
    try:
        cleaned = _reencode(image, mime)
    finally:
        image.close()
    if cleaned.size_bytes > max_bytes:
        raise UploadRejected("too_large")
    return cleaned


def clean_upload(filename: str, data: bytes) -> CleanImage:
    """Validate and re-encode an uploaded image; ``UploadRejected`` otherwise.

    SVG comes back as PNG. Touches no database, so it can be tested alone.
    """
    if not isinstance(filename, str) or len(filename) > MAX_FILENAME_CHARS:
        raise UploadRejected("unsupported")
    if not data:
        raise UploadRejected("empty")
    claimed = EXTENSION_TYPES.get(_extension(filename))
    if claimed is None:
        raise UploadRejected("gif" if _extension(filename) == ".gif" else "unsupported")
    if claimed == SVG:
        if len(data) > svg_byte_limit():
            raise UploadRejected("svg_too_large")
    elif len(data) > settings.creative_upload_max_bytes:
        raise UploadRejected("too_large")

    actual = sniff(data)
    if actual == "image/gif":
        raise UploadRejected("gif")
    if actual is None:
        raise UploadRejected("unsupported")
    if actual != claimed:
        raise UploadRejected("mismatch")

    if actual == SVG:
        try:
            png = svg_to_png(data)
        except SvgRejected as exc:
            log.info("SVG upload refused: reason=%s bytes=%s", exc.reason, len(data))
            raise UploadRejected(_SVG_REASONS.get(exc.reason, "svg_invalid")) from exc
        return _clean_raster(png, PNG, max_bytes=settings.creative_upload_max_bytes)
    return _clean_raster(data, actual, max_bytes=settings.creative_upload_max_bytes)


def normalise_raster(data: bytes) -> CleanImage:
    """Clean an AI-generated or fetched image exactly as an upload is cleaned.

    The type comes from the bytes alone (there is no file name). PNG, JPEG
    and WebP only; the input cap is ``IMAGE_FETCH_MAX_BYTES``, since vendor
    images are larger than typical uploads.
    """
    if not data:
        raise UploadRejected("empty")
    if len(data) > settings.image_fetch_max_bytes:
        raise UploadRejected("too_large")
    actual = sniff(data)
    if actual == "image/gif":
        raise UploadRejected("gif")
    if actual not in _PIL_FORMATS:
        raise UploadRejected("unsupported")
    return _clean_raster(data, actual, max_bytes=settings.image_fetch_max_bytes)


# ── Storing ────────────────────────────────────────────────────────────────
#: The session factory for the short storage transaction. Replaced in tests.
_session_scope: Callable[[str | None], AbstractContextManager[Session]] = session_scope
#: How long a write waits for another write in the same workspace.
STORAGE_LOCK_TIMEOUT = "10s"
#: PostgreSQL's ``lock_not_available``: the ``lock_timeout`` ran out.
_LOCK_NOT_AVAILABLE = "55P03"
_SET_LOCK_TIMEOUT = text("SELECT set_config('lock_timeout', :v, true)")


def _lock_tenant_storage(db: Session, tenant_id: str) -> None:
    """Serialise quota checks per workspace for the rest of the transaction.

    Raises :class:`StorageBusy` (429) when the session's ``lock_timeout``
    runs out first.
    """
    bind = db.get_bind()
    if bind.dialect.name == "postgresql":
        try:
            db.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                {"key": f"stored_files:{tenant_id}"},
            )
        except DBAPIError as exc:
            if getattr(exc.orig, "sqlstate", None) != _LOCK_NOT_AVAILABLE:
                raise
            log.warning("Storage lock busy: tenant=%s", tenant_id)
            raise StorageBusy() from exc


def _own_transaction(db: Session) -> bool:
    """Whether the write gets a short transaction of its own (PostgreSQL)."""
    return db.get_bind().dialect.name == "postgresql"


def _write(
    db: Session,
    *,
    tenant_id: str,
    data: bytes,
    mime: str,
    kind: str,
    source: str,
    sha256: str,
    width: int,
    height: int,
    created_by: str,
    job_id: str,
    display_name: str,
) -> StoredFile:
    """Lock, check the cap, write the bytes and the row, flush."""
    _lock_tenant_storage(db, tenant_id)
    used = tenant_usage_bytes(db, tenant_id)
    if used + len(data) > settings.creative_storage_max_bytes_per_tenant:
        log.info(
            "Storage cap reached: tenant=%s used=%s incoming=%s", tenant_id, used, len(data)
        )
        raise StorageFull(STORAGE_FULL_MESSAGE)

    stored = StoredFile(
        tenant_id=tenant_id,
        kind=kind,
        source=source,
        mime=mime,
        size_bytes=len(data),
        width=width,
        height=height,
        sha256=sha256,
        display_name=display_name[:_DISPLAY_NAME_CHARS],
        created_by=created_by,
        job_id=job_id,
    )
    get_storage().put(db, stored, data)
    db.add(stored)
    db.flush()
    log.info(
        "File stored: tenant=%s file=%s kind=%s source=%s bytes=%s sha256=%s",
        tenant_id,
        stored.id,
        kind,
        source,
        len(data),
        sha256[:12],
    )
    return stored


def store_file(
    db: Session,
    *,
    tenant_id: str,
    data: bytes,
    mime: str,
    kind: str,
    source: str,
    sha256: str,
    width: int = 0,
    height: int = 0,
    created_by: str = "",
    job_id: str = "",
    display_name: str = "",
) -> StoredFile:
    """Write already-validated bytes under the workspace's storage cap.

    The caller must have produced ``data`` itself (a render) or cleaned it
    with :func:`clean_upload` / :func:`normalise_raster`.
    ``CREATIVE_STORAGE_MAX_BYTES_PER_TENANT`` of 0 allows nothing.

    On PostgreSQL the per-workspace storage lock, the cap check and the
    write run in a short transaction of their own (pinned to ``tenant_id``)
    that commits at once, so the lock is never held for the rest of a long
    job or request. The row is then merged into ``db`` and returned. Waiting
    longer than ``STORAGE_LOCK_TIMEOUT`` raises :class:`StorageBusy` (429).
    Any other session (a test double) gets the write flushed in ``db``.
    """
    if mime not in STORED_FILE_MIMES:
        raise UploadRejected("unsupported")
    if hashlib.sha256(data).hexdigest() != sha256:
        raise UploadRejected("damaged")
    fields = {
        "tenant_id": tenant_id, "data": data, "mime": mime, "kind": kind, "source": source,
        "sha256": sha256, "width": width, "height": height, "created_by": created_by,
        "job_id": job_id, "display_name": display_name,
    }
    if not _own_transaction(db):
        return _write(db, **fields)
    with _session_scope(tenant_id) as session:
        session.execute(_SET_LOCK_TIMEOUT, {"v": STORAGE_LOCK_TIMEOUT})
        stored = _write(session, **fields)
    # Committed, and the lock released. Attach the row to the caller's session.
    return db.merge(stored, load=False)


def discard_files(db: Session, *, tenant_id: str, file_ids: list[str]) -> None:
    """Remove files a failed step stored but nothing refers to (best effort).

    :func:`store_file` commits on its own, so when the work that was to use
    a file fails afterwards the file would otherwise count toward the
    workspace's cap for ever. Tenant-filtered; logs ids and counts only.
    """
    from app.services.storage import storage_for

    wanted = [i for i in file_ids if isinstance(i, str) and i]
    if not wanted:
        return

    def remove(session: Session) -> None:
        for file_id in wanted:
            row = session.get(StoredFile, file_id)
            if row is None or row.tenant_id != tenant_id:
                continue
            storage_for(row).delete(session, tenant_id=tenant_id, file_id=file_id)

    try:
        if _own_transaction(db):
            with _session_scope(tenant_id) as session:
                remove(session)
        else:
            remove(db)
    except Exception as exc:  # noqa: BLE001 - cleanup only; the caller's failure is what matters
        log_failure(log, "uploads.discard_failed", exc, tenant=tenant_id, files=len(wanted))
        return
    log.info("Unused files removed: tenant=%s files=%d", tenant_id, len(wanted))


def accept_image(
    db: Session,
    *,
    tenant_id: str,
    user_id: str,
    filename: str,
    data: bytes,
    kind: str,
) -> StoredFile:
    """Validate, clean and store an image a person uploaded.

    ``tenant_id`` and ``user_id`` come from the authenticated request, never
    from the request body. Returns the flushed row; the caller commits.
    """
    if kind not in UPLOAD_KINDS:
        raise UploadRejected("bad_kind")
    cleaned = clean_upload(filename, data)
    return store_file(
        db,
        tenant_id=tenant_id,
        data=cleaned.data,
        mime=cleaned.mime,
        kind=kind,
        source=FileSource.UPLOAD,
        sha256=cleaned.sha256,
        width=cleaned.width,
        height=cleaned.height,
        created_by=user_id,
        display_name=display_name_for(filename, cleaned.mime),
    )


def store_generated_image(
    db: Session,
    *,
    tenant_id: str,
    data: bytes,
    job_id: str,
    kind: str = FileKind.BACKGROUND,
) -> StoredFile:
    """Clean (:func:`normalise_raster`) and store an AI-generated image for a job."""
    return store_cleaned_image(
        db, tenant_id=tenant_id, cleaned=normalise_raster(data), job_id=job_id, kind=kind
    )


def store_cleaned_image(
    db: Session,
    *,
    tenant_id: str,
    cleaned: CleanImage,
    job_id: str,
    kind: str = FileKind.BACKGROUND,
) -> StoredFile:
    """Store an AI-generated image the caller has already cleaned with
    :func:`normalise_raster` (so it is not decoded and re-encoded twice)."""
    return store_file(
        db,
        tenant_id=tenant_id,
        data=cleaned.data,
        mime=cleaned.mime,
        kind=kind,
        source=FileSource.GENERATED,
        sha256=cleaned.sha256,
        width=cleaned.width,
        height=cleaned.height,
        job_id=job_id,
        display_name=f"{kind}{_SUFFIXES[cleaned.mime]}",
    )
