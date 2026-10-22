"""Creative Studio: stored files, brand kits, products and creative jobs.

* ``stored_files``  every image or document the studio keeps: an uploaded
                    logo or product photo (already sniffed, capped and
                    re-encoded by the upload service), an AI background, a
                    rendered banner or PDF. Carries provenance (who, when,
                    from where, SHA-256) so any file can be audited or purged.
* ``brand_kits``    a workspace's logos, colours and fonts. At most one per
                    workspace is the default, enforced by a partial unique
                    index rather than by application code.
* ``products``      what a creative is about, with the approved claims the
                    copy is grounded against.
* ``creative_jobs`` one request to make a set of creatives. The brief and any
                    revision feedback are customer text and are encrypted
                    under the organisation's key (``Ctx.CREATIVE_BRIEF`` and
                    ``Ctx.CREATIVE_FEEDBACK``). ``outputs`` holds ids and QA
                    results only, never copy.

Colours and fonts end up inside rendered HTML and CSS, where autoescaping
does not help, so both are validated twice: in Python as the attribute is set
(:func:`normalise_hex` and :func:`check_font_id`), and by a CHECK constraint in
the database, so a row written by any other path cannot hold anything else.

Every table is tenant-scoped and under row-level security (``app/db/rls.py``).
Foreign keys between these tables do not prove the referenced row belongs to
the same organisation; the services that set them must load the referenced
row through a tenant-filtered query first.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, validates

from app.db.base import FK, PK, Base, TenantScopedMixin, TimestampMixin, new_id

# ── Allow-lists shared with the migration's CHECK constraints ──────────────
# The migration (0019_creative_studio) freezes its own copy of these values.
# Widening a list here therefore also needs a migration that replaces the
# matching constraint.

#: Self-hosted, OFL-licensed fonts the renderer ships (research R5).
FONT_IDS: tuple[str, ...] = (
    "inter",
    "lato",
    "poppins",
    "montserrat",
    "lora",
    "playfair_display",
    "oswald",
    "bebas_neue",
)

#: The only types a stored file may be served as.
STORED_FILE_MIMES: tuple[str, ...] = (
    "image/png",
    "image/jpeg",
    "image/webp",
    "application/pdf",
)

#: ``#rrggbb``, lower case. Short forms, names and ``rgb()`` are refused: the
#: value is written into CSS, so only one unambiguous shape is accepted.
_HEX_COLOUR = re.compile(r"^#[0-9a-f]{6}$")
#: The same rule in PostgreSQL regex syntax, for the CHECK constraints.
HEX_COLOUR_SQL = "^#[0-9a-f]{6}$"
_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")


def normalise_hex(value: Any) -> str:  # noqa: ANN401 - validates untyped input
    """Return ``value`` as ``#rrggbb`` or raise ``ValueError``.

    Upper-case digits are folded to lower case; nothing else is repaired.
    Whitespace, including a trailing newline, is refused rather than trimmed:
    ``fullmatch`` (not ``$``, which matches before a final ``\\n``) keeps the
    accepted shape identical to the database CHECK.
    """
    if not isinstance(value, str):
        raise ValueError("Colour must be a hex value such as #1a2b3c")
    candidate = value.lower()
    if not _HEX_COLOUR.fullmatch(candidate):
        raise ValueError("Colour must be a hex value such as #1a2b3c")
    return candidate


def check_font_id(value: Any) -> str:  # noqa: ANN401 - validates untyped input
    """Return ``value`` if it is an allow-listed font id, else raise ``ValueError``."""
    if not isinstance(value, str) or value not in FONT_IDS:
        raise ValueError("Font must be one of the fonts the studio provides")
    return value


def _sql_in(column: str, values: tuple[str, ...]) -> str:
    quoted = ", ".join(f"'{v}'" for v in values)
    return f"{column} IN ({quoted})"


# ── Stored files ───────────────────────────────────────────────────────────
class FileKind:
    LOGO = "logo"
    PRODUCT_PHOTO = "product_photo"
    #: An AI-generated or stock background, normalised like an upload.
    BACKGROUND = "background"
    #: A finished creative (PNG, JPEG or PDF).
    RENDER = "render"
    #: A small preview of a render, for the approvals queue.
    PREVIEW = "preview"


class FileSource:
    UPLOAD = "upload"
    GENERATED = "generated"
    RENDERED = "rendered"


class StorageBackend:
    POSTGRES = "postgres"
    S3 = "s3"


class StoredFile(Base, TimestampMixin, TenantScopedMixin):
    __tablename__ = "stored_files"
    __table_args__ = (
        Index("ix_stored_files_tenant_kind", "tenant_id", "kind"),
        Index("ix_stored_files_tenant_sha256", "tenant_id", "sha256"),
        CheckConstraint(_sql_in("mime", STORED_FILE_MIMES), name="ck_stored_files_mime"),
        CheckConstraint("size_bytes >= 0", name="ck_stored_files_size"),
        CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name="ck_stored_files_sha256"),
    )

    id: Mapped[str] = mapped_column(PK, primary_key=True, default=new_id)
    kind: Mapped[str] = mapped_column(String(24), nullable=False)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    mime: Mapped[str] = mapped_column(String(40), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    width: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    height: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    #: SHA-256 of the stored bytes (after re-encoding), lower-case hex.
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    #: Display name only, already sanitised by the upload service. Never used
    #: as a path; downloads build their filename with ``slug_for``.
    display_name: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    #: User id of the uploader, or "" for files the system made.
    created_by: Mapped[str] = mapped_column(String(36), default="", nullable=False)
    #: The creative job that produced it, or "" for uploads.
    job_id: Mapped[str] = mapped_column(String(36), default="", nullable=False)
    storage_backend: Mapped[str] = mapped_column(
        String(16), default=StorageBackend.POSTGRES, nullable=False
    )
    #: Object key for an external backend; "" when the bytes live in ``data``.
    storage_key: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    #: The bytes, for the Postgres backend. Deferred so listing files never
    #: pulls blobs into memory.
    data: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True, deferred=True)

    @validates("mime")
    def _validate_mime(self, _key: str, value: str) -> str:
        if value not in STORED_FILE_MIMES:
            raise ValueError("Unsupported file type")
        return value

    @validates("sha256")
    def _validate_sha256(self, _key: str, value: str) -> str:
        if not isinstance(value, str) or not _SHA256_HEX.fullmatch(value):
            raise ValueError("sha256 must be 64 lower-case hex characters")
        return value


# ── Brand kits ─────────────────────────────────────────────────────────────
_KIT_COLOURS = (
    "primary_color",
    "secondary_color",
    "accent_color",
    "text_color",
    "background_color",
)
_KIT_FONTS = ("heading_font", "body_font")


class BrandKit(Base, TimestampMixin, TenantScopedMixin):
    __tablename__ = "brand_kits"
    __table_args__ = (
        # One default kit per workspace: a second is refused by the database.
        Index(
            "uq_brand_kits_one_default",
            "tenant_id",
            unique=True,
            postgresql_where=text("is_default"),
        ),
        *(
            CheckConstraint(f"{col} ~ '{HEX_COLOUR_SQL}'", name=f"ck_brand_kits_{col}")
            for col in _KIT_COLOURS
        ),
        *(
            CheckConstraint(_sql_in(col, FONT_IDS), name=f"ck_brand_kits_{col}")
            for col in _KIT_FONTS
        ),
    )

    id: Mapped[str] = mapped_column(PK, primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    #: Logo for light backgrounds. Deleting the file clears it, not the kit.
    logo_file_id: Mapped[str | None] = mapped_column(
        FK, ForeignKey("stored_files.id", ondelete="SET NULL"), nullable=True
    )
    #: Logo variant for dark backgrounds.
    logo_dark_file_id: Mapped[str | None] = mapped_column(
        FK, ForeignKey("stored_files.id", ondelete="SET NULL"), nullable=True
    )
    primary_color: Mapped[str] = mapped_column(String(7), default="#1f2937", nullable=False)
    secondary_color: Mapped[str] = mapped_column(String(7), default="#4b5563", nullable=False)
    accent_color: Mapped[str] = mapped_column(String(7), default="#2563eb", nullable=False)
    text_color: Mapped[str] = mapped_column(String(7), default="#111827", nullable=False)
    background_color: Mapped[str] = mapped_column(String(7), default="#ffffff", nullable=False)
    heading_font: Mapped[str] = mapped_column(String(32), default="inter", nullable=False)
    body_font: Mapped[str] = mapped_column(String(32), default="inter", nullable=False)
    #: How the brand sounds, in a sentence or two. Reference material for the
    #: model only, always wrapped as untrusted text.
    voice: Mapped[str] = mapped_column(String(500), default="", nullable=False)
    #: Words and phrases the copy must never use.
    banned_phrases: Mapped[list[Any]] = mapped_column(JSONB, default=list, nullable=False)
    #: Added by the template, never by the model.
    disclaimer: Mapped[str] = mapped_column(String(500), default="", nullable=False)

    @validates(*_KIT_COLOURS)
    def _validate_colour(self, _key: str, value: str) -> str:
        return normalise_hex(value)

    @validates(*_KIT_FONTS)
    def _validate_font(self, _key: str, value: str) -> str:
        return check_font_id(value)


# ── Products ───────────────────────────────────────────────────────────────
class Product(Base, TimestampMixin, TenantScopedMixin):
    __tablename__ = "products"
    __table_args__ = (Index("ix_products_tenant_name", "tenant_id", "name"),)

    id: Mapped[str] = mapped_column(PK, primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    #: Landing page for the product. Validated as https by the API, and only
    #: ever rendered as text or an escaped link.
    url: Mapped[str] = mapped_column(String(1000), default="", nullable=False)
    #: Free text such as "From $49/month", shown only if the user supplied it.
    price_text: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    #: The only claims copy may make, referenced by index by the model and
    #: checked by ``ground_claims``.
    approved_claims: Mapped[list[Any]] = mapped_column(JSONB, default=list, nullable=False)
    #: StoredFile ids (kind ``product_photo``), in display order. Readers must
    #: load each through a tenant-filtered query; a deleted file is skipped.
    image_file_ids: Mapped[list[Any]] = mapped_column(JSONB, default=list, nullable=False)
    disclaimer: Mapped[str] = mapped_column(String(500), default="", nullable=False)
    is_archived: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


# ── Creative jobs ──────────────────────────────────────────────────────────
class CreativeJobStatus:
    QUEUED = "queued"
    RUNNING = "running"
    #: Outputs rendered and queued for review.
    SUCCEEDED = "succeeded"
    #: ``error_code`` says why, as a key the console turns into plain English.
    FAILED = "failed"
    CANCELLED = "cancelled"


class CreativeJob(Base, TimestampMixin, TenantScopedMixin):
    __tablename__ = "creative_jobs"
    __table_args__ = (
        Index("ix_creative_jobs_tenant_status", "tenant_id", "status"),
        # Monthly quota counts and the job list both range over created_at.
        Index("ix_creative_jobs_tenant_created", "tenant_id", "created_at"),
        CheckConstraint("revision_count >= 0", name="ck_creative_jobs_revisions"),
    )

    id: Mapped[str] = mapped_column(PK, primary_key=True, default=new_id)
    #: User id of the requester.
    created_by: Mapped[str] = mapped_column(String(36), default="", nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), default=CreativeJobStatus.QUEUED, nullable=False
    )
    brand_kit_id: Mapped[str | None] = mapped_column(
        FK, ForeignKey("brand_kits.id", ondelete="SET NULL"), nullable=True
    )
    product_id: Mapped[str | None] = mapped_column(
        FK, ForeignKey("products.id", ondelete="SET NULL"), nullable=True
    )
    #: Format ids from the format catalogue, e.g. ``["banner_300x250"]``.
    formats: Mapped[list[Any]] = mapped_column(JSONB, default=list, nullable=False)
    #: Connector slug and model that wrote the copy, checked against the
    #: allow-list when the job was accepted.
    text_connector: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    text_model: Mapped[str] = mapped_column(String(120), default="", nullable=False)
    #: Image model, or "" for "No AI image: use my photos".
    image_model: Mapped[str] = mapped_column(String(120), default="", nullable=False)
    #: The brief, encrypted (Ctx.CREATIVE_BRIEF). Never logged.
    brief_encrypted: Mapped[str] = mapped_column(Text, default="", nullable=False)
    #: The latest revision feedback, encrypted (Ctx.CREATIVE_FEEDBACK).
    feedback_encrypted: Mapped[str] = mapped_column(Text, default="", nullable=False)
    revision_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    #: Per format: asset id, file ids and QA results. Ids and numbers only —
    #: no copy, which lives encrypted on the marketing asset.
    outputs: Mapped[list[Any]] = mapped_column(JSONB, default=list, nullable=False)
    #: Model tokens. NULL means the model server did not report usage
    #: (migration 0022); the token reservation then stays in ``tokens_out``
    #: for the quota (``quotas.settle_tokens``).
    tokens_in: Mapped[int | None] = mapped_column(Integer, default=0, nullable=True)
    tokens_out: Mapped[int | None] = mapped_column(Integer, default=0, nullable=True)
    images_generated: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    #: A short machine key ("model_unavailable", "quota_exceeded" …), never
    #: raw exception or vendor text.
    error_code: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
