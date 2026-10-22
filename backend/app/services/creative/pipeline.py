"""Creative Studio jobs: accept a request, write, design, check and queue for review.

The lifecycle of one job:

1. **Accept** (:func:`submit_job`, in the request). The brand kit, product
   and photos are loaded through the tenant-filtered ``brand_kits`` helpers
   (another workspace's id is a 404), the text and image models are checked
   against the allow-list (``model_choice``; 422, never a silent fallback),
   the monthly job quota is taken under its advisory lock (429), and the
   request is stored encrypted. Nothing slow happens here: the job is handed
   to a worker once the request commits.
2. **Run** (:func:`run_job`, on the Celery "render" queue or a worker
   thread, never the request thread). The job is claimed with
   ``SELECT … FOR UPDATE SKIP LOCKED`` so a redelivered task does nothing,
   the model choice is checked again, and the on-demand agent runs through
   ``runner.run_on_demand`` with the chosen model as a strict override.
3. **Generate** (:func:`generate`, the agent's body). Per format: copy from
   the model with every piece of user text fenced as untrusted, parsed with
   ``schema.parse_copy`` and checked with ``qa.check_copy_content``, re-asked
   at most twice with our own description of the problem. Then one
   background (an allow-listed AI image, a product photo, or a brand-colour
   gradient), rendering, visual QA, and — in one savepoint per format,
   committed as soon as that format is done — a ``MarketingAsset`` (kind
   ``creative``), a pending approval (``approvals.queue_manual``; creatives
   are always reviewed) and a ``creative_text`` version. The render and its
   separate, smaller preview are stored by ``uploads.store_file`` in a short
   transaction of their own (removed again if the savepoint fails).

Token and image quotas are reserved before every model or image call and
settled after it, each in a short transaction of its own (``quotas``), so a
failed job still pays for what it used and the quota lock is never held for
the length of a job. Jobs left queued or running by a lost task are failed
by :func:`sweep_stale_jobs`. Briefs, feedback and copy are never logged: log lines carry ids,
counts and connector slugs only.
"""
from __future__ import annotations

import hashlib
import io
import json
import logging
import math
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Annotated, Any

from PIL import Image
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlalchemy import and_, event, or_, select
from sqlalchemy.orm import Session

from app.agents.base.context import AgentContext, LLMOverride
from app.agents.base.contracts import AgentResult
from app.agents.creative_studio import prompts
from app.agents.creative_studio.agent import NAME as AGENT_NAME
from app.agents.creative_studio.agent import SLUG as AGENT_SLUG
from app.core.config import settings
from app.core.exceptions import AppError, ConflictError, InvalidInputError, NotFoundError
from app.core.logging import get_logger, log_failure
from app.db.base import utcnow
from app.db.session import session_scope
from app.llm.base import LLMError, extract_json
from app.llm.from_connector import MAX_MODEL_ID_CHARS
from app.llm.providers import usage_reported
from app.models.approval import ApprovalItem
from app.models.content import AssetKind, AssetStatus, MarketingAsset
from app.models.creative import BrandKit, CreativeJob, CreativeJobStatus, FileKind, FileSource, Product, StoredFile
from app.models.history import SnapshotKind
from app.models.workspace import Organization
from app.schemas.common import ApiModel
from app.services import approvals, assets, audit, brand_kits, images, uploads
from app.services.connectors import build_connector_bundle
from app.services.creative import model_choice, quotas, render
from app.services.creative.compose import ComposeError, CreativeInputs, InlineImage, Layout
from app.services.creative.formats import FORMATS, FormatSpec, get_format
from app.services.creative.qa import (
    ProductFacts,
    QAIssue,
    QAReport,
    build_report,
    check_contrast,
    check_copy_content,
    check_dimensions,
    check_file_size,
    check_logo,
    contrast_ratio,
    relative_luminance,
)
from app.services.creative.render import RenderError, RenderTimeout
from app.services.creative.schema import CreativeCopy, Palette, compose_copy, parse_copy
from app.services.encryption import Ctx, OrgCipher
from app.services.storage import storage_for

log = get_logger(__name__)

# ── Limits ─────────────────────────────────────────────────────────────────
MAX_MESSAGE_CHARS = 600
MAX_AUDIENCE_CHARS = 300
MAX_NOTES_CHARS = 600
MAX_GOAL_CHARS = 100
MAX_TONE_CHARS = 100
#: Re-asks after the first reply fails validation or content QA.
MAX_REASKS = 2
#: ``CreativeJob.text_model`` / ``image_model`` column width.
MODEL_COLUMN_CHARS = 120
#: Longest side of the preview image of a print format.
PREVIEW_MAX_PX = 1600
#: Longest side of the preview image of a digital format (banner, social, email).
DIGITAL_PREVIEW_MAX_PX = 1200
#: JPEG quality for a digital preview (moderate; the full render is the deliverable).
DIGITAL_PREVIEW_JPEG_QUALITY = 75
#: Gradient tile drawn for "No AI image" without a product photo.
_GRADIENT_PX = 256

#: The approval ``type`` creatives are queued with.
APPROVAL_TYPE = "Creative"
#: ``AgentRun.triggered_by`` for a job's run: links the run (and its
#: progress step) to the job, for :func:`job_stage`.
RUN_TRIGGER_PREFIX = "creative_job:"
#: ``Session.info`` key the reviser leaves the new revision job id under, so
#: the request-changes route can return it (``RevisionOut.job_id``).
REVISION_JOB_KEY = "creative_revision_job_id"

# Progress steps written on the run, and the stage the console shows for each.
STEP_WRITING = "Writing the text"
STEP_DESIGNING = "Drawing the designs"
STEP_CHECKING = "Checking and saving the designs"
_STAGES: dict[str, str] = {STEP_WRITING: "writing", STEP_DESIGNING: "designing", STEP_CHECKING: "checking"}

# ── Plain-English failures, by ``CreativeJob.error_code`` ──────────────────
ERROR_MESSAGES: dict[str, str] = {
    "model_unavailable": (
        "The AI model chosen for this design isn't available any more. "
        "Choose another one and try again."
    ),
    "quota_exceeded": "Your workspace has used this month's Creative Studio allowance.",
    "copy_rejected": (
        "The AI model couldn't write text that passed our checks: only your approved "
        "claims and the numbers in your product details may be used. Try a different "
        "request or model."
    ),
    "model_failed": "The AI model didn't answer. Please try again in a few minutes.",
    "image_failed": (
        "The AI picture could not be made. Try again, or choose \"No AI image\"."
    ),
    "render_timeout": "Drawing this design took too long. Please try again.",
    "render_failed": "We could not draw this design. Please try again.",
    "storage_full": (
        "Your workspace has run out of file space. Remove designs or uploads you "
        "no longer need, then try again."
    ),
    "inputs_missing": (
        "The brand kit, product or picture used for this design no longer exists."
    ),
    "run_skipped": (
        "Creative Studio couldn't start this design. Check that agents haven't been "
        "stopped and that the AI model is connected, then try again."
    ),
    "dispatch_failed": "Creative Studio is busy right now. Please try again in a few minutes.",
    "busy": "Creative Studio is busy right now. Please try again in a few minutes.",
    "timed_out": "This design took too long and was stopped. Please try again.",
    "claims_changed": (
        "A product claim on this design has changed since it was written. "
        "Edit the text and choose the claims again."
    ),
    "failed": "This design couldn't be finished. Please try again.",
}


def error_message(code: str) -> str:
    """The sentence for a failure code ("" when there is no failure)."""
    if not code:
        return ""
    return ERROR_MESSAGES.get(code, ERROR_MESSAGES["failed"])


class CreativeJobFailed(AppError):
    """The job (or one format of it) cannot be finished. ``message`` is safe to show."""

    status_code = 409
    code = "creative_failed"

    def __init__(self, error_code: str, message: str | None = None) -> None:
        super().__init__(message or error_message(error_code), details={"reason": error_code})
        self.error_code = error_code


class _CopyRejected(Exception):
    """Every attempt at one format's copy failed validation or QA."""


# ── Request ────────────────────────────────────────────────────────────────
def _clean_text(value: object, *, max_chars: int, multiline: bool) -> str:
    text = brand_kits.clean_block(value) if multiline else brand_kits.clean_line(value)
    if len(text) > max_chars:
        raise ValueError(f"Keep this under {max_chars} characters")
    return text


class TextModelIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    connector: str = Field(min_length=1, max_length=64)
    model: str | None = Field(default=None, max_length=MAX_MODEL_ID_CHARS)


class ImageModelIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: An image connector slug, or ``"none"`` (product photo or brand colours).
    provider: str = Field(default=model_choice.NO_IMAGE, min_length=1, max_length=64)
    model: str | None = Field(default=None, max_length=MAX_MODEL_ID_CHARS)


class CreativeJobRequest(BaseModel):
    """``POST /creative/jobs``. Every text field is untrusted and length-capped."""

    model_config = ConfigDict(extra="forbid")

    formats: list[Annotated[str, Field(max_length=64)]] = Field(min_length=1, max_length=50)
    brand_kit_id: str = Field(min_length=1, max_length=64)
    product_id: str | None = Field(default=None, max_length=64)
    message: str = Field(default="", max_length=MAX_MESSAGE_CHARS * 4)
    audience: str = Field(default="", max_length=MAX_AUDIENCE_CHARS * 4)
    notes: str = Field(default="", max_length=MAX_NOTES_CHARS * 4)
    goal: str = Field(default="", max_length=MAX_GOAL_CHARS * 4)
    tone: str = Field(default="", max_length=MAX_TONE_CHARS * 4)
    text_model: TextModelIn
    image_model: ImageModelIn = Field(default_factory=ImageModelIn)

    @field_validator("formats")
    @classmethod
    def _formats(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value):
            raise ValueError("Each size can only be chosen once")
        if len(value) > settings.creative_max_formats_per_job:
            raise ValueError(
                f"Choose at most {settings.creative_max_formats_per_job} sizes for one request"
            )
        if any(f not in FORMATS for f in value):
            raise ValueError("One of the sizes is not one the studio can make")
        return value

    @field_validator("message")
    @classmethod
    def _message(cls, value: str) -> str:
        return _clean_text(value, max_chars=MAX_MESSAGE_CHARS, multiline=True)

    @field_validator("audience")
    @classmethod
    def _audience(cls, value: str) -> str:
        return _clean_text(value, max_chars=MAX_AUDIENCE_CHARS, multiline=False)

    @field_validator("notes")
    @classmethod
    def _notes(cls, value: str) -> str:
        return _clean_text(value, max_chars=MAX_NOTES_CHARS, multiline=True)

    @field_validator("goal", "tone", mode="before")
    @classmethod
    def _none_is_empty(cls, value: object) -> object:
        # The wizard sends null for "no goal" / "no tone"; that means "".
        return "" if value is None else value

    @field_validator("goal")
    @classmethod
    def _goal(cls, value: str) -> str:
        return _clean_text(value, max_chars=MAX_GOAL_CHARS, multiline=False)

    @field_validator("tone")
    @classmethod
    def _tone(cls, value: str) -> str:
        return _clean_text(value, max_chars=MAX_TONE_CHARS, multiline=False)


def input_limits() -> dict[str, int]:
    """The request limits, for ``GET /creative/options``."""
    return {
        "max_formats": settings.creative_max_formats_per_job,
        "message_chars": MAX_MESSAGE_CHARS,
        "audience_chars": MAX_AUDIENCE_CHARS,
        "notes_chars": MAX_NOTES_CHARS,
        "goal_chars": MAX_GOAL_CHARS,
        "tone_chars": MAX_TONE_CHARS,
        "max_revisions": settings.creative_max_revisions,
    }


# ── The stored request (encrypted in ``CreativeJob.brief_encrypted``) ──────
_ENVELOPE_VERSION = 1
_BACKGROUND_MODES = frozenset({"ai", "photo", "gradient", "file"})


@dataclass(frozen=True, slots=True)
class Background:
    """Where a job's background comes from. ``file_id`` for photo and file."""

    mode: str
    file_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"mode": self.mode, "file_id": self.file_id}

    @classmethod
    def from_dict(cls, raw: object) -> Background:
        data = raw if isinstance(raw, dict) else {}
        mode = data.get("mode") if data.get("mode") in _BACKGROUND_MODES else "gradient"
        file_id = brand_kits.parse_id(data.get("file_id"))
        if mode in ("photo", "file") and file_id is None:
            mode = "gradient"
        return cls(mode=mode, file_id=file_id if mode in ("photo", "file") else None)


@dataclass(frozen=True, slots=True)
class Envelope:
    """Everything about a job that is not a column: the request text, the
    image choice, where the background comes from and, for a revision, what
    it revises. Stored encrypted (``Ctx.CREATIVE_BRIEF``) and never logged."""

    brief: dict[str, str] = field(default_factory=dict)
    image_provider: str = model_choice.NO_IMAGE
    background: Background = Background("gradient")
    parent_job_id: str | None = None
    #: ``{"approval_id", "asset_id"}`` for a revision job.
    revises: dict[str, str] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "v": _ENVELOPE_VERSION,
            "brief": {k: self.brief.get(k, "") for k in prompts.BRIEF_FIELDS},
            "image_provider": self.image_provider,
            "background": self.background.to_dict(),
            "parent_job_id": self.parent_job_id,
            "revises": self.revises,
        }

    @classmethod
    def from_dict(cls, raw: object) -> Envelope:
        data = raw if isinstance(raw, dict) else {}
        brief_raw = data.get("brief") if isinstance(data.get("brief"), dict) else {}
        brief = {k: str(brief_raw.get(k) or "") for k in prompts.BRIEF_FIELDS}
        revises_raw = data.get("revises")
        revises = None
        if isinstance(revises_raw, dict):
            approval_id = brand_kits.parse_id(revises_raw.get("approval_id"))
            asset_id = brand_kits.parse_id(revises_raw.get("asset_id"))
            if approval_id and asset_id:
                revises = {"approval_id": approval_id, "asset_id": asset_id}
        provider = data.get("image_provider")
        return cls(
            brief=brief,
            image_provider=provider if isinstance(provider, str) and provider else model_choice.NO_IMAGE,
            background=Background.from_dict(data.get("background")),
            parent_job_id=brand_kits.parse_id(data.get("parent_job_id")),
            revises=revises,
        )


def write_envelope(org: Organization, envelope: Envelope) -> str:
    return OrgCipher.for_org(org).encrypt_json(envelope.to_dict(), context=Ctx.CREATIVE_BRIEF)


def read_envelope(org: Organization, job: CreativeJob) -> Envelope:
    if not job.brief_encrypted:
        return Envelope()
    data = OrgCipher.for_org(org).decrypt_json(job.brief_encrypted, context=Ctx.CREATIVE_BRIEF, default={})
    return Envelope.from_dict(data)


# ── Dispatch ───────────────────────────────────────────────────────────────
#: The session factory workers use. Replaced in tests.
_session_scope: Callable[[str | None], AbstractContextManager[Session]] = session_scope


def _after_commit(db: Session, fn: Callable[[], None]) -> None:
    """Run ``fn`` once ``db`` commits, so a worker can see what was written."""

    def run(_session: Session) -> None:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001 - the request itself succeeded
            log_failure(log, "creative.dispatch_failed", exc, level=logging.ERROR)

    event.listen(db, "after_commit", run, once=True)


def start_job(tenant_id: str, job_id: str) -> None:
    """Hand a committed job to a worker (Celery "render" queue, or a thread)."""
    from app.orchestration.tasks import submit_creative_job

    try:
        submit_creative_job(tenant_id, job_id)
    except Exception as exc:  # noqa: BLE001 - the job is marked failed instead
        log_failure(log, "creative.job_dispatch_failed", exc, level=logging.ERROR, tenant=tenant_id, job=job_id)
        fail_job(tenant_id, job_id, "dispatch_failed")


def schedule_rerender(db: Session, tenant_id: str, asset_id: str) -> None:
    """Redraw an asset in a worker once ``db`` commits (no model call)."""
    from app.orchestration.tasks import submit_creative_rerender

    _after_commit(db, lambda: submit_creative_rerender(tenant_id, asset_id))


# ── Loading, tenant-filtered ───────────────────────────────────────────────
def _job_row(db: Session, tenant_id: str, job_id: object) -> CreativeJob | None:
    canonical = brand_kits.parse_id(job_id)
    if canonical is None:
        return None
    return db.execute(
        select(CreativeJob).where(CreativeJob.id == canonical, CreativeJob.tenant_id == tenant_id)
    ).scalars().first()


def get_job(db: Session, *, tenant_id: str, job_id: str) -> CreativeJob:
    """One job of this workspace, or 404."""
    job = _job_row(db, tenant_id, job_id)
    if job is None:
        raise NotFoundError("That design request could not be found")
    return job


def list_jobs(db: Session, *, tenant_id: str, limit: int = 50) -> list[CreativeJob]:
    """This workspace's most recent jobs, newest first."""
    limit = max(1, min(int(limit), 100))
    return list(
        db.execute(
            select(CreativeJob)
            .where(CreativeJob.tenant_id == tenant_id)
            .order_by(CreativeJob.created_at.desc(), CreativeJob.id.desc())
            .limit(limit)
        ).scalars()
    )


def _kit_or_fail(db: Session, tenant_id: str, kit_id: str | None) -> BrandKit:
    try:
        return brand_kits.get_kit(db, tenant_id, kit_id or "")
    except NotFoundError as exc:
        raise CreativeJobFailed("inputs_missing") from exc


def _product_or_fail(db: Session, tenant_id: str, product_id: str | None) -> Product | None:
    if not product_id:
        return None
    try:
        return brand_kits.get_product(db, tenant_id, product_id)
    except NotFoundError as exc:
        raise CreativeJobFailed("inputs_missing") from exc


def _read_bytes(db: Session, row: StoredFile) -> bytes:
    return storage_for(row).get(db, tenant_id=row.tenant_id, file_id=row.id)


def _product_photo_ids(db: Session, tenant_id: str, product: Product | None) -> list[str]:
    if product is None:
        return []
    return brand_kits.visible_image_ids(db, tenant_id, [product]).get(product.id, [])


# ── Accepting a job ────────────────────────────────────────────────────────
def _check_model_width(value: str) -> None:
    if len(value) > MODEL_COLUMN_CHARS:
        raise model_choice.ModelChoiceError(
            model_choice.REASON_NOT_APPROVED,
            "That AI model can't be used in Creative Studio. Choose another one.",
        )


def create_job(
    db: Session, *, org: Organization, user_id: str, request: CreativeJobRequest
) -> CreativeJob:
    """Validate a request and admit it as a queued job. Flushes; the caller commits.

    Raises 404 for a kit or product that is not this workspace's, 422
    (``ModelChoiceError``) for a model off the allow-list or not connected,
    and 429 (``QuotaExceeded``) when the month's jobs are used up.
    """
    if not settings.creative_enabled:
        raise ConflictError("Creative Studio is turned off on this platform.")
    kit = brand_kits.get_kit(db, org.id, request.brand_kit_id)
    product = brand_kits.get_product(db, org.id, request.product_id) if request.product_id else None
    if product is not None and product.is_archived:
        raise InvalidInputError("That product is archived. Restore it or choose another one.")

    bundle = build_connector_bundle(db, org)
    text = model_choice.resolve_text(
        bundle.instances, request.text_model.connector, request.text_model.model, names=bundle.names
    )
    image = model_choice.resolve_image(
        bundle.instances, request.image_model.provider, request.image_model.model
    )
    _check_model_width(text.model)
    _check_model_width(image.model)

    if image.uses_ai:
        if settings.creative_max_images_per_job < 1:
            raise InvalidInputError(
                "AI pictures are switched off for Creative Studio. Choose \"No AI image\"."
            )
        background = Background("ai")
    else:
        photos = _product_photo_ids(db, org.id, product)
        background = Background("photo", photos[0]) if photos else Background("gradient")

    envelope = Envelope(
        brief={name: getattr(request, name) for name in prompts.BRIEF_FIELDS},
        image_provider=image.connector or model_choice.NO_IMAGE,
        background=background,
    )
    job = CreativeJob(
        tenant_id=org.id,
        created_by=user_id or "",
        status=CreativeJobStatus.QUEUED,
        brand_kit_id=kit.id,
        product_id=product.id if product is not None else None,
        formats=list(request.formats),
        text_connector=text.connector,
        text_model=text.model,
        image_model=image.model,
        brief_encrypted=write_envelope(org, envelope),
        feedback_encrypted="",
        revision_count=0,
        outputs=[],
        tokens_in=0,
        tokens_out=0,
        images_generated=0,
        error_code="",
    )
    quotas.admit_job(db, job)
    log.info(
        "Creative job accepted: tenant=%s job=%s formats=%d text=%s background=%s",
        org.id, job.id, len(job.formats), text.connector, background.mode,
    )
    return job


def submit_job(
    db: Session, *, org: Organization, user_id: str, request: CreativeJobRequest
) -> CreativeJob:
    """:func:`create_job`, then start it in a worker once the caller commits."""
    job = create_job(db, org=org, user_id=user_id, request=request)
    tenant_id, job_id = org.id, job.id
    _after_commit(db, lambda: start_job(tenant_id, job_id))
    return job


# ── Running a job (worker) ─────────────────────────────────────────────────
def _claim_statement(tenant_id: str, job_id: str, now: datetime) -> Any:  # noqa: ANN401 - a Select
    """The job, row-locked, if it is queued (or a run that died long ago).

    ``SKIP LOCKED``: a second delivery of the same task while the first holds
    the row gets nothing, and a job already running or finished is never
    picked up again.
    """
    stale = now - timedelta(seconds=settings.creative_render_time_limit + 60)
    return (
        select(CreativeJob)
        .where(
            CreativeJob.id == job_id,
            CreativeJob.tenant_id == tenant_id,
            or_(
                CreativeJob.status == CreativeJobStatus.QUEUED,
                and_(CreativeJob.status == CreativeJobStatus.RUNNING, CreativeJob.started_at < stale),
            ),
        )
        .with_for_update(skip_locked=True)
    )


def claim_job(db: Session, tenant_id: str, job_id: str) -> CreativeJob | None:
    if brand_kits.parse_id(job_id) is None:
        return None
    return db.execute(_claim_statement(tenant_id, job_id, utcnow())).scalars().first()


def _finish(job: CreativeJob, status: str, code: str = "") -> None:
    job.status = status
    job.error_code = code[:64]
    job.finished_at = utcnow()


def _reload_job_row(db: Session, job: CreativeJob) -> None:
    """See a status another session has committed (a time-limit fail)."""
    expire = getattr(db, "expire", None)
    if callable(expire):
        expire(job, ["status", "error_code", "finished_at"])


def _discard_job_files(db: Session, job: CreativeJob) -> None:
    """Remove files this run stored after the job was already failed."""
    ids: list[str] = []
    seen: set[str] = set()

    def add(value: object) -> None:
        if isinstance(value, str) and value and value not in seen:
            seen.add(value)
            ids.append(value)

    for output in job.outputs or []:
        if not isinstance(output, dict):
            continue
        for key in ("file_id", "render_file_id", "preview_file_id"):
            add(output.get(key))
    for file_id in db.execute(
        select(StoredFile.id).where(
            StoredFile.tenant_id == job.tenant_id,
            StoredFile.job_id == job.id,
        )
    ).scalars():
        add(file_id)
    uploads.discard_files(db, tenant_id=job.tenant_id, file_ids=ids)


def run_job(tenant_id: str, job_id: str) -> dict[str, str]:
    """Claim and run one job. Idempotent: a job not queued is left alone."""
    from app.models.agent import RunStatus
    from app.orchestration import runner

    with _session_scope(tenant_id) as db:
        job = claim_job(db, tenant_id, job_id)
        if job is None:
            log.info("Creative job not claimable (already taken or finished): job=%s", job_id)
            return {"status": "skipped"}
        job.status = CreativeJobStatus.RUNNING
        job.started_at = utcnow()
        job.error_code = ""
        db.commit()

        org = db.get(Organization, tenant_id)
        if org is None:
            _finish(job, CreativeJobStatus.FAILED, "inputs_missing")
            return {"status": job.status, "error_code": job.error_code}

        try:
            bundle = build_connector_bundle(db, org)
            choice = model_choice.resolve_text(
                bundle.instances, job.text_connector, job.text_model or None, names=bundle.names
            )
        except model_choice.ModelChoiceError as exc:
            log.info("Creative job %s: text model refused at run time (%s)", job.id, exc.reason)
            _finish(job, CreativeJobStatus.FAILED, "model_unavailable")
            _after_failed_revision(db, org, job)
            return {"status": job.status, "error_code": job.error_code}

        outcome = runner.run_on_demand(
            db,
            org=org,
            slug=AGENT_SLUG,
            job={"job_id": job.id},
            llm_override=LLMOverride(connector=choice.connector, model=choice.model),
            trigger="job",
            triggered_by=f"{RUN_TRIGGER_PREFIX}{job.id}",
        )
        if outcome.status == RunStatus.SUCCEEDED.value:
            _reload_job_row(db, job)
            if job.status != CreativeJobStatus.RUNNING:
                _discard_job_files(db, job)
                log.info(
                    "Creative job late finish ignored: tenant=%s job=%s status=%s code=%s",
                    tenant_id, job.id, job.status, job.error_code or "-",
                )
                return {"status": job.status, "error_code": job.error_code}
            _finish(job, CreativeJobStatus.SUCCEEDED, "")
        else:
            code = job.error_code or (
                "run_skipped" if outcome.status == RunStatus.SKIPPED.value else "failed"
            )
            _finish(job, CreativeJobStatus.FAILED, code)
            _after_failed_revision(db, org, job)
        log.info(
            "Creative job finished: tenant=%s job=%s status=%s code=%s outputs=%d",
            tenant_id, job.id, job.status, job.error_code or "-", len(job.outputs or []),
        )
        return {"status": job.status, "error_code": job.error_code}


def fail_job(tenant_id: str, job_id: str, code: str) -> None:
    """Mark a job failed from outside its run (a hard time limit, a dispatch error)."""
    try:
        with _session_scope(tenant_id) as db:
            job = _job_row(db, tenant_id, job_id)
            if job is None or job.status not in (CreativeJobStatus.QUEUED, CreativeJobStatus.RUNNING):
                return
            _finish(job, CreativeJobStatus.FAILED, code)
            org = db.get(Organization, tenant_id)
            if org is not None:
                _after_failed_revision(db, org, job)
    except Exception as exc:  # noqa: BLE001 - nothing more can be done; logged
        log_failure(log, "creative.fail_job_failed", exc, level=logging.ERROR, tenant=tenant_id, job=job_id)


#: Seconds past the render task's hard time limit after which a job still
#: queued or running is given up on by :func:`sweep_stale_jobs`.
STALE_GRACE_SECONDS = 300
#: Most jobs failed per workspace per sweep.
_SWEEP_BATCH = 200


def stale_jobs_statement(tenant_id: str, now: datetime) -> Any:  # noqa: ANN401 - a Select
    """One workspace's jobs stuck queued or running (a lost task, a dead worker)."""
    cutoff = now - timedelta(seconds=settings.creative_render_time_limit + STALE_GRACE_SECONDS)
    return (
        select(CreativeJob)
        .where(
            CreativeJob.tenant_id == tenant_id,
            or_(
                and_(CreativeJob.status == CreativeJobStatus.QUEUED, CreativeJob.created_at < cutoff),
                and_(
                    CreativeJob.status == CreativeJobStatus.RUNNING,
                    or_(
                        CreativeJob.started_at < cutoff,
                        and_(CreativeJob.started_at.is_(None), CreativeJob.created_at < cutoff),
                    ),
                ),
            ),
        )
        .order_by(CreativeJob.created_at)
        .limit(_SWEEP_BATCH)
        .with_for_update(skip_locked=True)
    )


def sweep_tenant_stale_jobs(db: Session, tenant_id: str, *, now: datetime | None = None) -> int:
    """Fail one workspace's stuck jobs ("timed_out"); returns how many.

    A stuck revision puts the previous version back in the approvals queue
    (:func:`_after_failed_revision`). The caller has pinned ``tenant_id``
    and commits.
    """
    jobs = list(db.execute(stale_jobs_statement(tenant_id, now or utcnow())).scalars())
    if not jobs:
        return 0
    org = db.get(Organization, tenant_id)
    for job in jobs:
        _finish(job, CreativeJobStatus.FAILED, "timed_out")
        if org is not None:
            _after_failed_revision(db, org, job)
    db.flush()
    log.info("Stuck creative jobs failed: tenant=%s jobs=%d", tenant_id, len(jobs))
    return len(jobs)


def sweep_stale_jobs(db: Session) -> int:
    """Fail every workspace's stuck creative jobs; returns the total.

    For the periodic scheduler (every few minutes). Runs per workspace under
    that workspace's pin, committing each (``run_per_organization``), so
    ``db`` may be an unpinned session.
    """
    from app.db.session import run_per_organization

    counts = run_per_organization(
        db, lambda session, tenant_id: sweep_tenant_stale_jobs(session, tenant_id),
        label="Sweeping stuck Creative Studio jobs",
    )
    return sum(counts)


def _after_failed_revision(db: Session, org: Organization, job: CreativeJob) -> None:
    """A revision that could not be made puts the previous version back in the queue.

    The old item was superseded when changes were asked for; without this the
    design would sit with nothing for a reviewer to decide on.
    """
    envelope = read_envelope(org, job)
    if not envelope.revises:
        return
    try:
        item = approvals.get_item(db, tenant_id=org.id, item_id=envelope.revises["approval_id"])
    except NotFoundError:
        return
    pending = approvals.find_open_proposal(
        db, tenant_id=org.id, agent_slug=item.agent_slug, target_kind=item.target_kind, target_id=item.target_id
    )
    if pending is not None:
        return
    payload = {k: v for k, v in approvals.read_payload(org, item).items() if not str(k).startswith("_")}
    payload[approvals.WHY_KEY] = [
        f"The revised version could not be made. {error_message(job.error_code)} "
        "This is the previous version."
    ]
    approvals.queue_revision(db, org=org, previous=item, title=item.title, payload=payload)


# ── Generating (the agent's body) ──────────────────────────────────────────
@dataclass(slots=True)
class _Revision:
    item: ApprovalItem
    asset: MarketingAsset
    content: dict[str, Any]


@dataclass(slots=True)
class _Draft:
    spec: FormatSpec
    copy: CreativeCopy
    report: QAReport


@dataclass(frozen=True, slots=True)
class _BackgroundImage:
    image: InlineImage | None
    ref: Background


@dataclass(frozen=True, slots=True)
class Drawn:
    """One rendered format: the file to deliver, its smaller preview, the layout."""

    data: bytes
    mime: str
    width: int
    height: int
    layout: Layout
    preview: bytes | None = None
    preview_size: tuple[int, int] = (0, 0)


def _is_time_limit(exc: BaseException) -> bool:
    """Celery's soft time limit, recognised without importing Celery."""
    return type(exc).__name__ == "SoftTimeLimitExceeded"


def generate(ctx: AgentContext) -> AgentResult:
    """Write, design, check and queue every format of the job on ``ctx.job``."""
    raw = (ctx.job or {}).get("job_id")
    job = _job_row(ctx.db, ctx.org.id, raw)
    if job is None:
        raise CreativeJobFailed("inputs_missing")
    try:
        return _generate(ctx, job)
    except CreativeJobFailed as exc:
        job.error_code = exc.error_code
        raise
    except quotas.QuotaExceeded:
        job.error_code = "quota_exceeded"
        raise
    except (quotas.QuotaBusy, uploads.StorageBusy):
        job.error_code = "busy"
        raise
    except uploads.StorageFull:
        job.error_code = "storage_full"
        raise
    except Exception as exc:
        if _is_time_limit(exc):
            job.error_code = "render_timeout"
            raise CreativeJobFailed("render_timeout") from exc
        job.error_code = "failed"
        raise


def _revision_target(db: Session, org: Organization, envelope: Envelope) -> _Revision | None:
    if not envelope.revises:
        return None
    try:
        item = approvals.get_item(db, tenant_id=org.id, item_id=envelope.revises["approval_id"])
        asset = assets.get(db, tenant_id=org.id, asset_id=envelope.revises["asset_id"])
    except NotFoundError as exc:
        raise CreativeJobFailed("inputs_missing") from exc
    if asset.kind != AssetKind.CREATIVE or item.target_id != asset.id:
        raise CreativeJobFailed("inputs_missing")
    return _Revision(item=item, asset=asset, content=assets.content_of(asset, OrgCipher.for_org(org)))


def _generate(ctx: AgentContext, job: CreativeJob) -> AgentResult:
    db, org = ctx.db, ctx.org
    envelope = read_envelope(org, job)
    kit = _kit_or_fail(db, org.id, job.brand_kit_id)
    product = _product_or_fail(db, org.id, job.product_id)
    revision = _revision_target(db, org, envelope)
    feedback = approvals.read_feedback(org, job.feedback_encrypted) if job.feedback_encrypted else ""
    try:
        specs = [get_format(f) for f in (job.formats or [])]
    except ValueError as exc:
        raise CreativeJobFailed("inputs_missing") from exc
    if not specs:
        raise CreativeJobFailed("inputs_missing")

    current = ""
    if revision is not None:
        copy = revision.content.get("copy") if isinstance(revision.content.get("copy"), dict) else {}
        current = json.dumps(
            {k: copy.get(k, "") for k in ("headline", "body", "cta")}, ensure_ascii=False
        )

    errors: dict[str, str] = {}
    drafts: list[_Draft] = []
    ctx.progress(STEP_WRITING, done=0, total=len(specs))
    for index, spec in enumerate(specs, start=1):
        try:
            copy, report = write_copy(
                ctx, job, spec, kit=kit, product=product, brief=envelope.brief,
                feedback=feedback, current_copy=current,
            )
        except _CopyRejected:
            errors[spec.id] = "copy_rejected"
            continue
        drafts.append(_Draft(spec, copy, report))
        ctx.progress(STEP_WRITING, done=index, total=len(specs))
    if not drafts:
        raise CreativeJobFailed("copy_rejected")

    ctx.progress(STEP_DESIGNING, done=0, total=len(drafts))
    palette = palette_for(kit)
    generated_ids: list[str] = []
    made: dict[str, dict[str, Any]] = {}
    backgrounds: dict[str, _BackgroundImage] = {}
    try:
        backgrounds = _backgrounds(
            ctx, job, envelope, [d.spec for d in drafts], kit, product, generated_ids,
        )
        logo = _logo(db, org.id, kit, palette)
        for index, draft in enumerate(drafts, start=1):
            spec = draft.spec
            try:
                result = draw(
                    spec, draft.copy, kit=kit, product=product, palette=palette,
                    logo=logo, background=backgrounds[spec.id].image,
                )
            except RenderTimeout:
                errors[spec.id] = "render_timeout"
                continue
            except (RenderError, ComposeError) as exc:
                log_failure(log, "creative.render_failed", exc, job=job.id, format=spec.id)
                errors[spec.id] = "render_failed"
                continue
            ctx.progress(STEP_CHECKING, done=index - 1, total=len(drafts))
            # Each finished format is saved and committed on its own (files, the
            # asset, its approval and its text version, in one savepoint), so a
            # time limit later in the job keeps what is already made and paid for.
            made[spec.id] = _save_format(
                ctx, job, draft, result, combine_qa(draft.report, result, spec, palette),
                kit=kit, product=product, background=backgrounds[spec.id].ref, revision=revision,
            )
            job.outputs = _outputs(specs, made, errors, partial=True)
            db.commit()
            ctx.progress(STEP_DESIGNING, done=index, total=len(drafts))
        if not made:
            raise CreativeJobFailed(next(iter(errors.values()), "render_failed"))
        job.outputs = _outputs(specs, made, errors)
        audit.record_agent_action(
            db,
            tenant_id=org.id,
            agent_name=AGENT_NAME,
            action=(
                "proposed a revised design for approval"
                if revision is not None
                else f"proposed {len(made)} design{'s' if len(made) != 1 else ''} for approval"
            ),
            module="content",
            context={"job_id": job.id, "designs": len(made), "failed": len(errors), "awaiting_approval": True},
        )
        summary = f"{len(made)} design{'s' if len(made) != 1 else ''} ready for review"
        if errors:
            summary += f"; {len(errors)} could not be made"
        return AgentResult(
            summary=summary,
            metric_label=summary,
            detail={"creative_job_id": job.id, "designs": len(made), "failed": len(errors)},
        )
    finally:
        # Generated pictures that no saved format points at still count
        # toward the workspace cap; drop them even when some formats landed.
        if generated_ids:
            kept = {
                backgrounds[format_id].ref.file_id
                for format_id in made
                if format_id in backgrounds and backgrounds[format_id].ref.file_id
            }
            orphans = [fid for fid in dict.fromkeys(generated_ids) if fid not in kept]
            if orphans:
                uploads.discard_files(db, tenant_id=org.id, file_ids=orphans)


def _failed_output(format_id: str, code: str) -> dict[str, Any]:
    return {
        "format_id": format_id, "asset_id": None, "approval_id": None, "file_id": None,
        "render_file_id": None,
        "preview_file_id": None, "qa": {"passed": False, "issues": []}, "error_code": code,
    }


def _outputs(
    specs: Sequence[FormatSpec],
    made: Mapping[str, dict[str, Any]],
    errors: Mapping[str, str],
    *,
    partial: bool = False,
) -> list[dict[str, Any]]:
    """``CreativeJob.outputs`` in request order. ``partial``: only the formats
    settled so far (made or failed), while the job is still running."""
    return [
        made.get(spec.id) or _failed_output(spec.id, errors.get(spec.id, "failed"))
        for spec in specs
        if not partial or spec.id in made or spec.id in errors
    ]


def _save_format(
    ctx: AgentContext,
    job: CreativeJob,
    draft: _Draft,
    result: Drawn,
    report: QAReport,
    *,
    kit: BrandKit,
    product: Product | None,
    background: Background,
    revision: _Revision | None,
) -> dict[str, Any]:
    """Save one drawn format in a savepoint; the caller commits.

    Stored files commit on their own (``uploads.store_file``), so if the
    savepoint fails they are removed again rather than left counting toward
    the workspace's storage cap.
    """
    saved: list[str] = []
    try:
        with ctx.db.begin_nested():
            return _persist(
                ctx, job, draft, result, report, kit=kit, product=product,
                background=background, revision=revision, saved=saved,
            )
    except Exception:
        uploads.discard_files(ctx.db, tenant_id=ctx.org.id, file_ids=saved)
        raise


# ── Copy ───────────────────────────────────────────────────────────────────
def write_copy(
    ctx: AgentContext,
    job: CreativeJob,
    spec: FormatSpec,
    *,
    kit: BrandKit,
    product: Product | None,
    brief: Mapping[str, str],
    feedback: str = "",
    current_copy: str = "",
) -> tuple[CreativeCopy, QAReport]:
    """Copy for one format that passes the schema and content QA.

    At most ``1 + MAX_REASKS`` calls. Each reserves the per-call token
    ceiling first and settles it after; a reply with no usage keeps the
    reservation. Raises :class:`_CopyRejected` when every reply failed, and
    :class:`CreativeJobFailed` ("model_failed") when the model did not answer.
    """
    approved = list(product.approved_claims or []) if product is not None else []
    facts = ProductFacts.from_product(product) if product is not None else ProductFacts()
    banned = list(kit.banned_phrases or [])
    cap = settings.creative_max_tokens_per_call
    problem = ""
    for attempt in range(MAX_REASKS + 1):
        prompt, system = prompts.copy_prompt(
            spec=spec,
            voice=kit.voice or "",
            banned_phrases=banned,
            product_name=product.name if product is not None else "",
            product_description=product.description if product is not None else "",
            price_text=product.price_text if product is not None else "",
            approved_claims=approved,
            brief=brief,
            feedback=feedback,
            current_copy=current_copy,
            problem=problem,
        )
        reserved = quotas.reserve_tokens(ctx.db, job, cap)
        try:
            response = ctx.ask_response(prompt, system=system, max_tokens=cap)
        except LLMError as exc:
            quotas.settle_tokens(ctx.db, job, reserved, tokens_in=0, tokens_out=0)
            log_failure(log, "creative.copy_call_failed", exc, job=job.id, format=spec.id, attempt=attempt)
            raise CreativeJobFailed("model_failed") from exc
        if usage_reported(response.usage):
            quotas.settle_tokens(
                ctx.db, job, reserved,
                tokens_in=response.usage.input_tokens, tokens_out=response.usage.output_tokens,
            )
        else:
            quotas.settle_tokens(ctx.db, job, reserved, tokens_in=None, tokens_out=None)

        try:
            data = extract_json(response.text)
            copy = parse_copy(data if isinstance(data, Mapping) else {"_": data}, format_id=spec.id, approved_claims=approved)
        except LLMError:
            problem = prompts.PROBLEM_NOT_JSON
            log.info("Creative copy refused: job=%s format=%s attempt=%d reason=not_json", job.id, spec.id, attempt)
            continue
        except ValidationError as exc:
            problem = prompts.problem_from_validation(exc)
            log.info(
                "Creative copy refused: job=%s format=%s attempt=%d reason=schema errors=%d",
                job.id, spec.id, attempt, exc.error_count(),
            )
            continue
        report = check_copy_content(copy, spec=spec, facts=facts, banned_phrases=banned)
        if not report.passed:
            problem = prompts.problem_from_report(report)
            log.info(
                "Creative copy refused: job=%s format=%s attempt=%d reason=qa codes=%s",
                job.id, spec.id, attempt, ",".join(i.code for i in report.issues if i.severity == "error"),
            )
            continue
        return copy, report
    log.info("Creative copy rejected after %d attempts: job=%s format=%s", MAX_REASKS + 1, job.id, spec.id)
    raise _CopyRejected()


# ── Palette, logo and background ───────────────────────────────────────────
_WHITE = "#ffffff"
_INK = "#111111"


def palette_for(kit: BrandKit) -> Palette:
    """The kit's colours; button text is white or ink, whichever reads better."""
    accent = kit.accent_color
    on_accent = _WHITE if contrast_ratio(_WHITE, accent) >= contrast_ratio(_INK, accent) else _INK
    return Palette(background=kit.background_color, text=kit.text_color, accent=accent, on_accent=on_accent)


def _logo(db: Session, tenant_id: str, kit: BrandKit, palette: Palette) -> InlineImage | None:
    """The kit's logo (the dark-background variant on a dark design), or None."""
    wanted = kit.logo_file_id
    if relative_luminance(palette.background) < 0.4 and kit.logo_dark_file_id:
        wanted = kit.logo_dark_file_id
    if not wanted:
        return None
    row = brand_kits.get_file(db, tenant_id, wanted, (FileKind.LOGO,))
    if row is None:
        return None
    try:
        return InlineImage.from_bytes(_read_bytes(db, row))
    except ComposeError as exc:
        log_failure(log, "creative.logo_unusable", exc, file=row.id)
        return None


def gradient_png(kit: BrandKit) -> bytes:
    """A soft vertical gradient between two brand colours, as PNG bytes."""
    top = _rgb(kit.background_color)
    bottom = _rgb(kit.secondary_color)
    img = Image.new("RGB", (_GRADIENT_PX, _GRADIENT_PX))
    pixels = img.load()
    for y in range(_GRADIENT_PX):
        t = y / (_GRADIENT_PX - 1)
        colour = tuple(round(a + (b - a) * t * 0.6) for a, b in zip(top, bottom, strict=True))
        for x in range(_GRADIENT_PX):
            pixels[x, y] = colour  # type: ignore[index]
    out = io.BytesIO()
    img.save(out, format="PNG", optimize=True)
    return out.getvalue()


def _rgb(colour: str) -> tuple[int, int, int]:
    value = colour.lstrip("#")
    return int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)


def shape_for(spec: FormatSpec) -> str:
    ratio = spec.width_px / spec.height_px
    if ratio > 1.25:
        return "landscape"
    if ratio < 0.8:
        return "portrait"
    return "square"


def _stored_background(db: Session, tenant_id: str, ref: Background, product: Product | None) -> InlineImage:
    if ref.mode == "photo":
        if ref.file_id not in _product_photo_ids(db, tenant_id, product):
            raise CreativeJobFailed("inputs_missing")
        kinds: tuple[str, ...] = (FileKind.PRODUCT_PHOTO,)
    else:
        kinds = (FileKind.BACKGROUND, FileKind.PRODUCT_PHOTO)
    row = brand_kits.get_file(db, tenant_id, ref.file_id or "", kinds)
    if row is None:
        raise CreativeJobFailed("inputs_missing")
    try:
        return InlineImage.from_bytes(_read_bytes(db, row))
    except ComposeError as exc:
        raise CreativeJobFailed("inputs_missing") from exc


def background_image(
    db: Session, tenant_id: str, ref: Background, *, kit: BrandKit, product: Product | None
) -> InlineImage:
    """The picture for a stored background reference (no AI call)."""
    if ref.mode in ("photo", "file"):
        return _stored_background(db, tenant_id, ref, product)
    return InlineImage.from_bytes(gradient_png(kit))


def _backgrounds(
    ctx: AgentContext,
    job: CreativeJob,
    envelope: Envelope,
    specs: Sequence[FormatSpec],
    kit: BrandKit,
    product: Product | None,
    generated_ids: list[str],
) -> dict[str, _BackgroundImage]:
    db, org = ctx.db, ctx.org
    ref = envelope.background
    if ref.mode != "ai":
        image = background_image(db, org.id, ref, kit=kit, product=product)
        return {spec.id: _BackgroundImage(image, ref) for spec in specs}

    # An allow-listed AI picture, one per shape, up to the per-job cap. The
    # choice is checked again now: the allow-list may have changed.
    bundle = build_connector_bundle(db, org)
    try:
        choice = model_choice.resolve_image(bundle.instances, envelope.image_provider, job.image_model or None)
    except model_choice.ModelChoiceError as exc:
        raise CreativeJobFailed("model_unavailable") from exc
    if not choice.uses_ai:
        raise CreativeJobFailed("model_unavailable")
    shapes = list(dict.fromkeys(shape_for(spec) for spec in specs))[: settings.creative_max_images_per_job]
    if not shapes:
        raise CreativeJobFailed("image_failed")
    prompt = prompts.background_prompt(
        product_name=product.name if product is not None else kit.name,
        colours=(kit.primary_color, kit.secondary_color, kit.accent_color),
    )
    made: dict[str, _BackgroundImage] = {}
    for shape in shapes:
        quotas.reserve_images(db, job, 1)
        try:
            generated = images.generate_bytes(
                db, org, prompt, shape=shape, provider=choice.connector,
                model_override=choice.model, strict=True,
            )
            # Cleaned once; the same clean bytes are stored and drawn.
            cleaned = uploads.normalise_raster(generated.data)
            stored = uploads.store_cleaned_image(db, tenant_id=org.id, cleaned=cleaned, job_id=job.id)
            generated_ids.append(stored.id)
            image = InlineImage.from_bytes(cleaned.data)
        except (uploads.StorageFull, uploads.StorageBusy):
            raise
        except Exception as exc:
            if _is_time_limit(exc):
                raise
            # Not made, so not charged; the vendor's reply is logged redacted only.
            quotas.release_images(db, job, 1)
            log_failure(log, "creative.image_failed", exc, job=job.id, connector=choice.connector, shape=shape)
            raise CreativeJobFailed("image_failed") from exc
        made[shape] = _BackgroundImage(image, Background("file", stored.id))
    first = next(iter(made.values()))
    return {spec.id: made.get(shape_for(spec), first) for spec in specs}


# ── Drawing and visual QA ──────────────────────────────────────────────────
def draw(
    spec: FormatSpec,
    copy: CreativeCopy,
    *,
    kit: BrandKit,
    product: Product | None,
    palette: Palette,
    logo: InlineImage | None,
    background: InlineImage | None,
) -> Drawn:
    """Render one format. Blocking; call from a worker only."""
    approved = list(product.approved_claims or []) if product is not None else []
    try:
        composed = compose_copy(
            copy,
            approved_claims=approved,
            product_disclaimer=product.disclaimer if product is not None else "",
            kit_disclaimer=kit.disclaimer or "",
        )
    except ValueError as exc:
        # A claim index that no longer points at approved text.
        raise ComposeError("A claim on this design is no longer approved") from exc
    inputs = CreativeInputs(
        format_id=spec.id, copy=composed, palette=palette, heading_font=kit.heading_font,
        body_font=kit.body_font, logo=logo, background=background,
    )
    if spec.kind == "print":
        pdf = render.render_creative(inputs, output="pdf")
        preview = render.render_creative(inputs, output="png")
        small, size = _downscale(preview.data)
        return Drawn(pdf.data, pdf.mime, pdf.width, pdf.height, pdf.layout, small, size)
    png = render.render_creative(inputs, output="png")
    data, mime = fit_file_size(png.data, spec)
    # A separate, smaller JPEG preview: the full render is only served once
    # the design is approved; reviewers see this before that. Capped to half
    # the longest side so GET /files/{preview} is never a full-size copy.
    small, size = _downscale(png.data, DIGITAL_PREVIEW_MAX_PX, jpeg=True)
    return Drawn(data, mime, png.width, png.height, png.layout, small, size)


def _flatten_on_white(img: Image.Image) -> Image.Image:
    """RGB, with transparency composited onto white (never black)."""
    if img.mode == "RGB":
        return img
    rgba = img.convert("RGBA")
    background = Image.new("RGB", rgba.size, (255, 255, 255))
    background.paste(rgba, mask=rgba.split()[3])
    return background


def _downscale(data: bytes, max_px: int = PREVIEW_MAX_PX, *, jpeg: bool = False) -> tuple[bytes, tuple[int, int]]:
    with Image.open(io.BytesIO(data)) as img:
        cap = max_px
        if jpeg:
            longest = max(img.size)
            cap = min(max_px, math.ceil(longest * 0.5)) if longest else 1
            cap = max(1, cap)
        img.thumbnail((cap, cap))
        rgb = _flatten_on_white(img)
        out = io.BytesIO()
        if jpeg:
            rgb.save(out, format="JPEG", quality=DIGITAL_PREVIEW_JPEG_QUALITY, optimize=True)
        else:
            rgb.save(out, format="PNG", optimize=True)
        return out.getvalue(), rgb.size


def fit_file_size(data: bytes, spec: FormatSpec) -> tuple[bytes, str]:
    """Re-encode a PNG that is over the format's cap (a 150 KB banner, say).

    Tries an optimised 256-colour PNG, then JPEG at falling quality. If none
    fits, the smallest is returned and QA flags it (``file_too_large``).
    """
    cap = spec.max_bytes
    if cap is None or len(data) <= cap:
        return data, "image/png"
    with Image.open(io.BytesIO(data)) as img:
        rgb = img.convert("RGB")
    candidates: list[tuple[bytes, str]] = []
    out = io.BytesIO()
    rgb.quantize(colors=256, method=Image.Quantize.MEDIANCUT).save(out, format="PNG", optimize=True)
    candidates.append((out.getvalue(), "image/png"))
    if len(candidates[-1][0]) <= cap:
        return candidates[-1]
    for quality in (85, 75, 65):
        out = io.BytesIO()
        rgb.save(out, format="JPEG", quality=quality, optimize=True)
        candidates.append((out.getvalue(), "image/jpeg"))
        if len(candidates[-1][0]) <= cap:
            return candidates[-1]
    return min(candidates, key=lambda c: len(c[0]))


_TEXT_DOES_NOT_FIT = QAIssue("text_does_not_fit", "The text does not fit this size. Try shorter wording.")


def visual_qa(result: Drawn, spec: FormatSpec, palette: Palette) -> list[QAIssue]:
    """Contrast, text that overflowed, logo size and space, file size and pixel size."""
    layout = result.layout
    found: list[QAIssue | None] = []
    if layout.overflowing:
        found.append(_TEXT_DOES_NOT_FIT)
    for page in layout.pages:
        for block in page.texts:
            behind = palette.accent if block.role == "cta" else palette.background
            found.append(check_contrast(block.colour, behind, size_px=block.size_px, bold=block.weight >= 600))
    for box in layout.logo_boxes:
        found.extend(check_logo(box, spec, others=layout.text_boxes))
    found.append(check_file_size(len(result.data), spec))
    if result.mime != "application/pdf":
        found.append(check_dimensions(result.width, result.height, spec))
    return [i for i in found if i is not None]


def combine_qa(content: QAReport, result: Drawn, spec: FormatSpec, palette: Palette) -> QAReport:
    """Content QA plus visual QA, one issue per code."""
    seen: set[str] = set()
    issues: list[QAIssue] = []
    for issue in (*content.issues, *visual_qa(result, spec, palette)):
        if issue.code not in seen:
            seen.add(issue.code)
            issues.append(issue)
    return build_report(issues)


# ── Saving ─────────────────────────────────────────────────────────────────
_EXTENSIONS: dict[str, str] = {"image/png": ".png", "image/jpeg": ".jpg", "application/pdf": ".pdf"}


def store_drawn(
    db: Session, tenant_id: str, job_id: str, spec: FormatSpec, result: Drawn,
    *, saved: list[str] | None = None,
) -> tuple[StoredFile, StoredFile]:
    """Store a render and its separate preview. Returns ``(file, preview)``.

    The two are always different files (kind ``render`` and ``preview``):
    the preview id is what goes on cards before approval, and must never be
    the full render. The id of each file stored is appended to ``saved``.
    """
    preview_data, preview_size = result.preview, result.preview_size
    if preview_data is None:
        if result.mime == "application/pdf":
            raise RenderError()
        preview_data, preview_size = _downscale(result.data, DIGITAL_PREVIEW_MAX_PX, jpeg=True)
    preview_mime = "image/png" if result.mime == "application/pdf" else "image/jpeg"
    main = uploads.store_file(
        db, tenant_id=tenant_id, data=result.data, mime=result.mime, kind=FileKind.RENDER,
        source=FileSource.RENDERED, sha256=hashlib.sha256(result.data).hexdigest(),
        width=result.width, height=result.height, job_id=job_id,
        display_name=f"{spec.id}{_EXTENSIONS.get(result.mime, '')}",
    )
    if saved is not None:
        saved.append(main.id)
    preview = uploads.store_file(
        db, tenant_id=tenant_id, data=preview_data, mime=preview_mime, kind=FileKind.PREVIEW,
        source=FileSource.RENDERED, sha256=hashlib.sha256(preview_data).hexdigest(),
        width=preview_size[0], height=preview_size[1], job_id=job_id,
        display_name=f"{spec.id}-preview{_EXTENSIONS.get(preview_mime, '')}",
    )
    if saved is not None:
        saved.append(preview.id)
    return main, preview


def claim_hashes(approved_claims: Sequence[Any], claim_ids: Sequence[int]) -> list[str]:
    """A SHA-256 of the approved text behind each claim index on a design.

    Stored next to the indices (``detail["claim_hashes"]``) so a redraw can
    tell when the product's claims were edited and an index now points at
    different words.
    """
    out: list[str] = []
    for index in claim_ids:
        claim = approved_claims[index] if 0 <= index < len(approved_claims) else None
        text = claim.strip() if isinstance(claim, str) else ""
        out.append(hashlib.sha256(text.encode("utf-8")).hexdigest())
    return out


def approval_payload(asset: MarketingAsset, spec: FormatSpec, preview_id: str, report: QAReport) -> dict[str, Any]:
    """What a reviewer's card carries: ids and fixed QA wording, no copy."""
    payload: dict[str, Any] = {
        "asset_id": asset.id,
        "format_id": spec.id,
        "format": spec.label,
        "preview_file_id": preview_id,
    }
    notes = [i.message for i in report.issues]
    if notes:
        payload[approvals.WHY_KEY] = notes
    return payload


def record_text_version(
    db: Session, org: Organization, asset_id: str, content: dict[str, Any], *,
    summary: str, agent_slug: str | None = AGENT_SLUG, model: str | None = None,
) -> None:
    """A ``creative_text`` version of one asset; a history failure never fails the work."""
    from app.services import history
    from app.services.history import store

    store.guarded(
        db, f"creative_text {asset_id}", history.record_version,
        db, org, SnapshotKind.CREATIVE_TEXT, asset_id, content,
        actor=None, agent_slug=agent_slug, model=model, summary=summary,
    )


def _persist(
    ctx: AgentContext,
    job: CreativeJob,
    draft: _Draft,
    result: Drawn,
    report: QAReport,
    *,
    kit: BrandKit,
    product: Product | None,
    background: Background,
    revision: _Revision | None,
    saved: list[str] | None = None,
) -> dict[str, Any]:
    db, org = ctx.db, ctx.org
    spec = draft.spec
    main, preview = store_drawn(db, org.id, job.id, spec, result, saved=saved)
    content = {"format_id": spec.id, "copy": draft.copy.model_dump()}
    approved = list(product.approved_claims or []) if product is not None else []
    detail = {
        "product_id": product.id if product is not None else None,
        "brand_kit_id": kit.id,
        "claim_hashes": claim_hashes(approved, draft.copy.claim_ids),
        "preview_file_id": preview.id,
        # The full-size output: the PNG (or JPEG) for digital formats, the
        # PDF for print. Social scheduling posts this when present.
        "render_file_id": main.id,
        "file_id": main.id,
        "job_id": job.id,
        "background": background.to_dict(),
        "qa": report.to_dict(),
        "preview_pending": False,
    }
    if revision is not None:
        asset = revision.asset
        assets.set_content(asset, ctx.cipher, content)
        asset.detail = {**(asset.detail or {}), **detail}
        if asset.status in (AssetStatus.FAILED, AssetStatus.REJECTED):
            asset.status = AssetStatus.DRAFT
            asset.error = ""
        db.flush()
        item = approvals.queue_revision(
            db, org=org, previous=revision.item, title=revision.item.title,
            payload=approval_payload(asset, spec, preview.id, report),
        )
        summary = "Revised with reviewer feedback"
    else:
        name = product.name if product is not None else kit.name
        asset = assets.create(
            db, tenant_id=org.id, cipher=ctx.cipher, agent_slug=AGENT_SLUG, kind=AssetKind.CREATIVE,
            channel="download", title=f"{spec.label}: {name}", content=content,
            run_id=ctx.run_id or "", detail=detail,
        )
        item = approvals.queue_manual(
            db, org=org, type_=APPROVAL_TYPE, title=asset.title, agent_slug=AGENT_SLUG,
            agent_name=AGENT_NAME, target_kind=assets.TARGET_KIND, target_id=asset.id,
            payload=approval_payload(asset, spec, preview.id, report),
        )
        summary = "Written by Creative Studio"
    record_text_version(db, org, asset.id, content, summary=summary, model=ctx.llm_model or None)
    return {
        "format_id": spec.id,
        "asset_id": asset.id,
        "approval_id": item.id,
        "file_id": main.id,
        "render_file_id": main.id,
        "preview_file_id": preview.id,
        "qa": report.to_dict(),
        "error_code": "",
    }


# ── Revisions (asked for from the approvals queue) ─────────────────────────
def create_revision_job(
    db: Session,
    org: Organization,
    asset: MarketingAsset,
    item: ApprovalItem,
    request: approvals.RevisionRequest,
) -> CreativeJob:
    """Queue a job that rewrites one creative with the reviewer's feedback.

    Runs in the request: checks only, no model call. The new job reuses the
    original request, model and background (so no new picture is paid for),
    carries the feedback still encrypted, counts as a job for the quota, and
    starts in a worker after the caller commits. Its id is left on
    ``db.info[REVISION_JOB_KEY]`` for the route to return.
    """
    cipher = OrgCipher.for_org(org)
    content = assets.content_of(asset, cipher)
    format_id = content.get("format_id")
    if not isinstance(format_id, str) or format_id not in FORMATS:
        raise ConflictError("This design's size is no longer available, so it can't be regenerated.")
    detail = asset.detail or {}
    parent = _job_row(db, org.id, detail.get("job_id"))
    if parent is None:
        raise ConflictError(
            "The request this design was made from is no longer available, so it can't be "
            "regenerated. Edit the text instead."
        )
    kit = brand_kits.get_kit(db, org.id, detail.get("brand_kit_id") or "")
    product_id = detail.get("product_id")
    product = brand_kits.get_product(db, org.id, product_id) if product_id else None

    bundle = build_connector_bundle(db, org)
    text = model_choice.resolve_text(
        bundle.instances, parent.text_connector, parent.text_model or None, names=bundle.names
    )
    parent_envelope = read_envelope(org, parent)
    stored = Background.from_dict(detail.get("background"))
    envelope = Envelope(
        brief=dict(parent_envelope.brief),
        image_provider=model_choice.NO_IMAGE,
        background=stored if stored.mode != "ai" else Background("gradient"),
        parent_job_id=parent.id,
        revises={"approval_id": item.id, "asset_id": asset.id},
    )
    job = CreativeJob(
        tenant_id=org.id,
        created_by=request.actor_id or "",
        status=CreativeJobStatus.QUEUED,
        brand_kit_id=kit.id,
        product_id=product.id if product is not None else None,
        formats=[format_id],
        text_connector=text.connector,
        text_model=text.model,
        image_model="",
        brief_encrypted=write_envelope(org, envelope),
        feedback_encrypted=request.feedback_encrypted,
        revision_count=request.revision,
        outputs=[],
        tokens_in=0,
        tokens_out=0,
        images_generated=0,
        error_code="",
    )
    quotas.admit_job(db, job)
    db.info[REVISION_JOB_KEY] = job.id
    tenant_id, job_id = org.id, job.id
    _after_commit(db, lambda: start_job(tenant_id, job_id))
    log.info(
        "Creative revision job accepted: tenant=%s job=%s parent=%s revision=%d feedback_chars=%d",
        org.id, job.id, parent.id, request.revision, request.feedback_chars,
    )
    return job


# ── Redrawing after an edit or restore (no model call) ─────────────────────
def rerender_asset(tenant_id: str, asset_id: str) -> dict[str, str]:
    """Redraw one creative from its saved text (worker). Never calls a model."""
    with _session_scope(tenant_id) as db:
        org = db.get(Organization, tenant_id)
        asset = db.execute(
            select(MarketingAsset).where(
                MarketingAsset.id == asset_id, MarketingAsset.tenant_id == tenant_id
            )
        ).scalars().first()
        if org is None or asset is None or asset.kind != AssetKind.CREATIVE:
            return {"status": "skipped"}
        return redraw(db, org, asset)


def _redraw_failed(
    db: Session, org: Organization, asset: MarketingAsset, detail: dict[str, Any], code: str,
    saved: list[str],
) -> dict[str, str]:
    """Record a failed redraw on the card: the old preview stays, the spinner stops."""
    uploads.discard_files(db, tenant_id=org.id, file_ids=saved)
    asset.detail = {**detail, "preview_pending": False, "redraw_error": code}
    db.flush()
    return {"status": "failed", "error_code": code}


def redraw(db: Session, org: Organization, asset: MarketingAsset) -> dict[str, str]:
    """Render ``asset`` from its saved copy, store it and point the card at it."""
    cipher = OrgCipher.for_org(org)
    content = assets.content_of(asset, cipher)
    detail = dict(asset.detail or {})
    saved: list[str] = []
    try:
        spec = get_format(str(content.get("format_id") or ""))
        kit = _kit_or_fail(db, org.id, detail.get("brand_kit_id"))
        product = _product_or_fail(db, org.id, detail.get("product_id"))
        approved = list(product.approved_claims or []) if product is not None else []
        copy = parse_copy(content.get("copy") or {}, format_id=spec.id, approved_claims=approved)
        recorded = detail.get("claim_hashes")
        if isinstance(recorded, list) and recorded != claim_hashes(approved, copy.claim_ids):
            # A claim index now points at different approved words.
            raise CreativeJobFailed("claims_changed")
        palette = palette_for(kit)
        background = background_image(
            db, org.id, Background.from_dict(detail.get("background")), kit=kit, product=product
        )
        result = draw(
            spec, copy, kit=kit, product=product, palette=palette,
            logo=_logo(db, org.id, kit, palette), background=background,
        )
        facts = ProductFacts.from_product(product) if product is not None else ProductFacts()
        content_report = check_copy_content(copy, spec=spec, facts=facts, banned_phrases=kit.banned_phrases or [])
        report = combine_qa(content_report, result, spec, palette)
        with db.begin_nested():
            main, preview = store_drawn(
                db, org.id, str(detail.get("job_id") or ""), spec, result, saved=saved
            )
    except (CreativeJobFailed, ValidationError, ValueError, RenderError, ComposeError) as exc:
        code = exc.error_code if isinstance(exc, CreativeJobFailed) else (
            "render_timeout" if isinstance(exc, RenderTimeout) else "render_failed"
        )
        log.info("Creative redraw failed: asset=%s reason=%s (%s)", asset.id, code, type(exc).__name__)
        return _redraw_failed(db, org, asset, detail, code, saved)
    except uploads.StorageFull:
        log.info("Creative redraw failed: asset=%s reason=storage_full", asset.id)
        return _redraw_failed(db, org, asset, detail, "storage_full", saved)
    except Exception as exc:  # noqa: BLE001 - recorded on the card; preview_pending must not stick
        code = "render_timeout" if _is_time_limit(exc) else (
            "busy" if isinstance(exc, (uploads.StorageBusy, quotas.QuotaBusy)) else "render_failed"
        )
        log_failure(log, "creative.redraw_failed", exc, asset=asset.id, reason=code)
        # The transaction may be unusable (a database error): start clean.
        db.rollback()
        return _redraw_failed(db, org, asset, detail, code, saved)

    asset.detail = {
        **detail,
        "preview_file_id": preview.id,
        "render_file_id": main.id,
        "file_id": main.id,
        "qa": report.to_dict(),
        "preview_pending": False,
        "redraw_error": "",
    }
    asset.updated_at = utcnow()
    pending = approvals.find_open_proposal(
        db, tenant_id=org.id, agent_slug=AGENT_SLUG, target_kind=assets.TARGET_KIND, target_id=asset.id
    )
    if pending is not None:
        payload = approval_payload(asset, spec, preview.id, report)
        previous = approvals.read_payload(org, pending)
        for key in (approvals.REVISION_KEY, approvals.REVISES_KEY):
            if key in previous:
                payload[key] = previous[key]
        pending.payload_encrypted = cipher.encrypt_json(payload, context=Ctx.APPROVAL_PAYLOAD)
        pending.payload_digest = approvals.payload_digest(payload)
    db.flush()
    log.info("Creative redrawn: asset=%s file=%s", asset.id, main.id)
    return {"status": "ok"}


# ── Reading jobs (for the API) ─────────────────────────────────────────────
class CreativeCopyOut(ApiModel):
    headline: str = ""
    body: str = ""
    cta: str = ""


class CreativeQAIssueOut(ApiModel):
    code: str
    severity: str
    message: str


class CreativeQAOut(ApiModel):
    passed: bool = False
    issues: list[CreativeQAIssueOut] = Field(default_factory=list)


class CreativeOutputOut(ApiModel):
    format_id: str
    asset_id: str | None = None
    approval_id: str | None = None
    preview_file_id: str | None = None
    qa: CreativeQAOut = Field(default_factory=CreativeQAOut)
    approval_status: str = ""
    #: Serialised as ``copy``; renamed here because ``BaseModel.copy`` exists.
    copy_: CreativeCopyOut | None = Field(default=None, alias="copy")
    error: str = ""


class CreativeJobOut(ApiModel):
    id: str
    status: str
    stage: str
    formats: list[str]
    brand_kit_id: str | None = None
    product_id: str | None = None
    parent_job_id: str | None = None
    revision_count: int = 0
    error_code: str = ""
    error: str = ""
    created_at: datetime | None = None
    finished_at: datetime | None = None
    outputs: list[CreativeOutputOut] = Field(default_factory=list)


def job_stage(db: Session, job: CreativeJob) -> str:
    """queued | writing | designing | checking while it runs; the status after."""
    if job.status != CreativeJobStatus.RUNNING:
        return job.status
    from app.models.agent import AgentRun

    step = db.execute(
        select(AgentRun.step)
        .where(
            AgentRun.tenant_id == job.tenant_id,
            AgentRun.agent_slug == AGENT_SLUG,
            AgentRun.triggered_by == f"{RUN_TRIGGER_PREFIX}{job.id}",
        )
        .order_by(AgentRun.started_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    return _STAGES.get(step or "", "writing")


def _rows_by_id(db: Session, model: type, tenant_id: str, ids: Sequence[str]) -> dict[str, Any]:
    wanted = sorted({i for i in ids if isinstance(i, str) and i})
    if not wanted:
        return {}
    rows = db.execute(select(model).where(model.tenant_id == tenant_id, model.id.in_(wanted))).scalars()
    return {row.id: row for row in rows}


def _qa_out(raw: object) -> CreativeQAOut:
    data = raw if isinstance(raw, dict) else {}
    issues = [
        CreativeQAIssueOut(code=str(i.get("code", ""))[:64], severity=str(i.get("severity", "error"))[:16],
                           message=str(i.get("message", ""))[:300])
        for i in (data.get("issues") or [])
        if isinstance(i, dict)
    ]
    return CreativeQAOut(passed=bool(data.get("passed")), issues=issues)


def job_out(db: Session, org: Organization, job: CreativeJob, *, include_copy: bool = True) -> CreativeJobOut:
    """The API view of one job. The caller has checked the job is this workspace's."""
    outputs = [o for o in (job.outputs or []) if isinstance(o, dict)]
    items = _rows_by_id(db, ApprovalItem, org.id, [o.get("approval_id") for o in outputs])
    rows = _rows_by_id(db, MarketingAsset, org.id, [o.get("asset_id") for o in outputs]) if include_copy else {}
    cipher = OrgCipher.for_org(org) if rows else None
    out: list[CreativeOutputOut] = []
    for o in outputs:
        item = items.get(o.get("approval_id") or "")
        asset = rows.get(o.get("asset_id") or "")
        copy = None
        preview = o.get("preview_file_id")
        if asset is not None and cipher is not None:
            raw = assets.content_of(asset, cipher).get("copy")
            if isinstance(raw, dict):
                copy = CreativeCopyOut(
                    headline=str(raw.get("headline", "")), body=str(raw.get("body", "")), cta=str(raw.get("cta", ""))
                )
            preview = (asset.detail or {}).get("preview_file_id") or preview
        out.append(
            CreativeOutputOut(
                format_id=str(o.get("format_id", "")),
                asset_id=o.get("asset_id"),
                approval_id=o.get("approval_id"),
                preview_file_id=preview,
                qa=_qa_out(o.get("qa")),
                approval_status=item.status if item is not None else "",
                copy_=copy,
                error=error_message(str(o.get("error_code") or "")),
            )
        )
    return CreativeJobOut(
        id=job.id,
        status=job.status,
        stage=job_stage(db, job),
        formats=[str(f) for f in (job.formats or [])],
        brand_kit_id=job.brand_kit_id,
        product_id=job.product_id,
        parent_job_id=read_envelope(org, job).parent_job_id,
        revision_count=job.revision_count or 0,
        error_code=job.error_code or "",
        error=error_message(job.error_code or "") if job.status == CreativeJobStatus.FAILED else "",
        created_at=job.created_at,
        finished_at=job.finished_at,
        outputs=out,
    )


__all__ = [
    "APPROVAL_TYPE",
    "ERROR_MESSAGES",
    "MAX_REASKS",
    "REVISION_JOB_KEY",
    "Background",
    "CreativeJobFailed",
    "CreativeJobOut",
    "CreativeJobRequest",
    "CreativeOutputOut",
    "Envelope",
    "ImageModelIn",
    "TextModelIn",
    "claim_hashes",
    "claim_job",
    "create_job",
    "create_revision_job",
    "error_message",
    "fail_job",
    "generate",
    "get_job",
    "input_limits",
    "job_out",
    "list_jobs",
    "read_envelope",
    "redraw",
    "rerender_asset",
    "run_job",
    "schedule_rerender",
    "start_job",
    "submit_job",
    "sweep_stale_jobs",
    "sweep_tenant_stale_jobs",
    "write_copy",
]
