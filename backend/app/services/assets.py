"""The content library: create, read, publish and export marketing assets.

Content agents (articles, search campaigns, social posts, emails) all write
:class:`MarketingAsset` rows through :func:`create`. What is specific to a
kind is plugged in by the agent that owns it:

* a **publisher** — sends one asset to its platform (``register_publisher``);
* **exporters** — turn an asset into a downloadable file in a named format
  (``register_exporter``), e.g. a Google Ads Editor CSV.

The console and the approval flow call :func:`publish` / :func:`export`, so
"approve", "publish now" and "download" behave the same for every channel.
"""
from __future__ import annotations

import csv
import html
import io
import json
from collections.abc import Callable, Sequence
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Literal

from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.csv_safe import ADS_EDITOR_REJECTED, UnsafeCellError, neutralise_row
from app.core.exceptions import (
    ConflictError,
    ConnectorConfigError,
    ConnectorError,
    InvalidInputError,
    NotFoundError,
)
from app.core.logging import get_logger, log_failure
from app.db.base import utcnow
from app.models.content import AssetKind, AssetStatus, MarketingAsset
from app.models.workspace import Organization
from app.services.encryption import Ctx, OrgCipher

if TYPE_CHECKING:
    from app.models.approval import ApprovalItem
    from app.services.approvals import RevisionOutcome, RevisionRequest, RevisionSupport

log = get_logger(__name__)


@dataclass(slots=True)
class ExportFile:
    filename: str
    mime: str
    data: bytes


Publisher = Callable[[Session, Organization, MarketingAsset, dict], None]
Exporter = Callable[[MarketingAsset, dict], ExportFile]

PUBLISHERS: dict[str, Publisher] = {}
EXPORTERS: dict[tuple[str, str], Exporter] = {}
#: Human labels for export formats, per kind, in menu order.
EXPORT_LABELS: dict[str, dict[str, str]] = {}


def register_publisher(kind: str) -> Callable[[Publisher], Publisher]:
    def decorator(fn: Publisher) -> Publisher:
        PUBLISHERS[kind] = fn
        return fn

    return decorator


def register_exporter(kind: str, fmt: str, label: str) -> Callable[[Exporter], Exporter]:
    def decorator(fn: Exporter) -> Exporter:
        EXPORTERS[(kind, fmt)] = fn
        EXPORT_LABELS.setdefault(kind, {})[fmt] = label
        return fn

    return decorator


# ── Rows ───────────────────────────────────────────────────────────────────
def create(
    db: Session,
    *,
    tenant_id: str,
    cipher: OrgCipher,
    agent_slug: str,
    kind: str,
    channel: str,
    title: str,
    content: dict,
    run_id: str = "",
    scheduled_for=None,  # noqa: ANN001
    image_ids: list[str] | None = None,
    detail: dict | None = None,
) -> MarketingAsset:
    asset = MarketingAsset(
        tenant_id=tenant_id,
        agent_slug=agent_slug,
        run_id=run_id or "",
        kind=kind,
        channel=channel,
        title=title[:300],
        content_encrypted=cipher.encrypt(json.dumps(content, ensure_ascii=False), context=Ctx.ASSET_CONTENT),
        status=AssetStatus.DRAFT,
        scheduled_for=scheduled_for,
        image_ids=list(image_ids or []),
        detail=dict(detail or {}),
    )
    db.add(asset)
    db.flush()
    return asset


def content_of(asset: MarketingAsset, cipher: OrgCipher) -> dict:
    raw = cipher.decrypt(asset.content_encrypted, context=Ctx.ASSET_CONTENT)
    try:
        data = json.loads(raw) if raw else {}
    except ValueError:
        data = {}
    return data if isinstance(data, dict) else {}


def set_content(asset: MarketingAsset, cipher: OrgCipher, content: dict) -> None:
    asset.content_encrypted = cipher.encrypt(json.dumps(content, ensure_ascii=False), context=Ctx.ASSET_CONTENT)
    asset.updated_at = utcnow()


def get(db: Session, *, tenant_id: str, asset_id: str) -> MarketingAsset:
    asset = db.get(MarketingAsset, asset_id)
    if asset is None or asset.tenant_id != tenant_id:
        raise NotFoundError("That item is no longer in the library")
    return asset


# ── Publishing ─────────────────────────────────────────────────────────────
def can_publish(kind: str) -> bool:
    return kind in PUBLISHERS


def publish(db: Session, org: Organization, asset: MarketingAsset, cipher: OrgCipher) -> MarketingAsset:
    """Send one asset to its platform now, recording success or the reason it failed."""
    if asset.status == AssetStatus.PUBLISHED:
        return asset
    publisher = PUBLISHERS.get(asset.kind)
    if publisher is None:
        raise ConflictError("This kind of content cannot be published directly; download it instead.")
    try:
        publisher(db, org, asset, content_of(asset, cipher))
    except Exception as exc:  # noqa: BLE001 - recorded on the asset, then re-raised
        from app.core.user_messages import public_error_message

        wait = retry_after_seconds(exc)
        if wait is not None:
            # The platform asked us to wait (Reddit's spacing or its rate
            # limit): the post is rescheduled, not failed.
            _defer(
                asset,
                seconds=wait,
                note=public_error_message(exc, fallback=_RATE_LIMITED),
                counter=None if is_own_spacing(exc) else DEFERRALS_KEY,
            )
            db.flush()
            return asset
        asset.status = AssetStatus.FAILED
        asset.error = public_error_message(exc, fallback="The platform did not accept this.")
        asset.updated_at = utcnow()
        db.flush()
        raise ConflictError(asset.error) from exc
    asset.status = AssetStatus.PUBLISHED
    asset.published_at = asset.published_at or utcnow()
    asset.error = ""
    asset.updated_at = utcnow()
    db.flush()
    return asset


# ── Exporting ──────────────────────────────────────────────────────────────
def formats_for(kind: str) -> dict[str, str]:
    """Export formats for a kind: the agent's own plus the generic ones."""
    own = EXPORT_LABELS.get(kind, {})
    return {**own, **{k: v for k, v in GENERIC_FORMATS.items() if k not in own}}


GENERIC_FORMATS = {"json": "JSON", "txt": "Plain text"}


def export(asset: MarketingAsset, cipher: OrgCipher, fmt: str) -> ExportFile:
    content = content_of(asset, cipher)
    exporter = EXPORTERS.get((asset.kind, fmt))
    if exporter is not None:
        # The exporter's CSV mode is decided here, by format, not by content.
        token = _csv_mode.set(CSV_MODE_ADS_IMPORT if (asset.kind, fmt) in ADS_IMPORT_FORMATS else CSV_MODE_SPREADSHEET)
        try:
            return exporter(asset, content)
        finally:
            _csv_mode.reset(token)
    stem = slug_for(asset.title or asset.kind)
    if fmt == "json":
        return ExportFile(f"{stem}.json", "application/json", json.dumps(content, ensure_ascii=False, indent=2).encode())
    if fmt == "txt":
        return ExportFile(f"{stem}.txt", "text/plain; charset=utf-8", plain_text(content).encode())
    raise InvalidInputError(f"{fmt!r} is not an export format for this item")


# ── Helpers agents' exporters share ────────────────────────────────────────
def slug_for(text: str) -> str:
    out = "".join(ch.lower() if ch.isalnum() else "-" for ch in text).strip("-")
    while "--" in out:
        out = out.replace("--", "-")
    return (out or "export")[:80]


def plain_text(value: Any, indent: int = 0) -> str:
    """A readable text rendering of any JSON content."""
    pad = "  " * indent
    if isinstance(value, dict):
        lines = []
        for key, item in value.items():
            label = str(key).replace("_", " ").capitalize()
            if isinstance(item, (dict, list)):
                lines.append(f"{pad}{label}:")
                lines.append(plain_text(item, indent + 1))
            else:
                lines.append(f"{pad}{label}: {item}")
        return "\n".join(lines)
    if isinstance(value, list):
        return "\n".join(
            f"{pad}- {plain_text(item, indent + 1).strip()}" if not isinstance(item, dict) else plain_text(item, indent)
            for item in value
        )
    return f"{pad}{value}"


#: A file a person opens in a spreadsheet: every formula-like cell is
#: neutralised with a leading apostrophe (OWASP CSV injection rule).
CSV_MODE_SPREADSHEET = "spreadsheet"
#: A file an ads platform's importer reads (Google Ads Editor, Microsoft
#: Advertising bulk). An apostrophe would be imported as data and placeholder
#: ids such as "-1" must stay as they are, so cells are validated instead.
CSV_MODE_ADS_IMPORT = "ads_import"
CsvMode = Literal["spreadsheet", "ads_import"]

#: Registered export formats that are machine imports, by (kind, format).
ADS_IMPORT_FORMATS: frozenset[tuple[str, str]] = frozenset(
    {("search_campaign", "google_ads_editor"), ("search_campaign", "microsoft_bulk")}
)

# The mode for CSVs written while an exporter runs; set by :func:`export`.
_csv_mode: ContextVar[CsvMode] = ContextVar("csv_mode", default=CSV_MODE_SPREADSHEET)

_IMPORT_PREFIX = " \t\r\n+-"


def check_import_cell(value: Any) -> str:  # noqa: ANN401 - any cell value
    """One ads-import cell, validated rather than escaped.

    A cell that (after leading whitespace and any ``+``/``-`` signs) starts
    with ``=`` or ``@`` can only be a formula, so it is refused. ``-`` and
    ``+`` are left intact: negative-keyword syntax and placeholder ids.
    """
    text = "" if value is None else str(value)
    if text.lstrip(_IMPORT_PREFIX).startswith(ADS_EDITOR_REJECTED):
        raise UnsafeCellError("A cell in this export starts with a formula character.")
    return text


def csv_bytes(
    header: list[str], rows: list[list[Any]], *, utf16: bool = False, mode: CsvMode | None = None
) -> bytes:
    """CSV text. ``utf16`` gives the tab-separated UTF-16 Google Ads Editor accepts.

    Formula injection (see :mod:`app.core.csv_safe`): ``mode`` says who reads
    the file. ``"spreadsheet"`` (the default) neutralises every cell;
    ``"ads_import"`` refuses formula cells and changes nothing else. When
    ``mode`` is not given, the mode :func:`export` set for the format applies
    (``ADS_IMPORT_FORMATS``); the Ads Editor's ``utf16`` file is always an
    import.
    """
    chosen: CsvMode = mode or (CSV_MODE_ADS_IMPORT if utf16 else _csv_mode.get())
    if chosen == CSV_MODE_ADS_IMPORT:
        try:
            head = [check_import_cell(cell) for cell in header]
            body = [[check_import_cell(cell) for cell in row] for row in rows]
        except UnsafeCellError as exc:
            raise InvalidInputError(
                "Some text in this campaign starts with = or @, which the ads platform would "
                "read as a formula. Edit that text, then download it again."
            ) from exc
    else:
        head = neutralise_row(header)
        body = [neutralise_row(row) for row in rows]
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter="\t" if utf16 else ",", lineterminator="\r\n")
    writer.writerow(head)
    writer.writerows(body)
    text = buffer.getvalue()
    return ("﻿" + text).encode("utf-16-le") if utf16 else ("﻿" + text).encode("utf-8")


def escape(text: str) -> str:
    return html.escape(text or "", quote=True)


# ── Approval: one path for every kind ──────────────────────────────────────
#: The approval target kind content agents use for their assets.
TARGET_KIND = "marketing_asset"


#: Detail keys recorded on a design when it is approved: the exact files a
#: person saw and approved. Sharing and downloads use only these, so a design
#: changed after approval (a restored or redrawn version) is never sent out.
APPROVED_RENDER_KEY = "approved_render_file_id"
APPROVED_PREVIEW_KEY = "approved_preview_file_id"
APPROVED_RENDER_SHA_KEY = "approved_render_sha256"


def release(db: Session, org: Organization, asset: MarketingAsset, cipher: OrgCipher) -> MarketingAsset:
    """What approving an asset does.

    Publishes now, or schedules it when it has a future date. A kind or
    channel with no publishing API is marked approved, ready to download; a
    design also records the files that were approved (:func:`_record_approved_design`).

    A social post that is due goes through the scheduled publisher
    (:func:`publish_due`), so the emergency stops, the daily limit and the
    platform's "wait" apply to it exactly as to every other post.
    """
    now = utcnow()
    if asset.status in (AssetStatus.PUBLISHED, AssetStatus.EXPORTED):
        return asset
    if not can_publish(asset.kind) or asset.detail.get("export_only"):
        asset.status = AssetStatus.APPROVED
        asset.updated_at = now
        if asset.kind == AssetKind.CREATIVE:
            _record_approved_design(db, org, asset)
        db.flush()
        return asset
    if asset.scheduled_for is not None and asset.scheduled_for > now:
        asset.status = AssetStatus.SCHEDULED
        asset.updated_at = now
        db.flush()
        return asset
    if asset.kind == AssetKind.SOCIAL_POST:
        return _release_social_now(db, org, asset, now)
    return publish(db, org, asset, cipher)


def _approved_file_id(value: object) -> str | None:
    return value if isinstance(value, str) and 0 < len(value) <= 64 else None


def _record_approved_design(db: Session, org: Organization, asset: MarketingAsset) -> None:
    """Remember which render and preview were approved, with the render's digest.

    The render is looked up only in this workspace; a missing file is
    recorded as "none", so sharing it later is refused rather than guessed.
    """
    from app.models.creative import FileKind
    from app.services import brand_kits

    detail = asset.detail or {}
    render_id = _approved_file_id(detail.get("render_file_id") or detail.get("file_id"))
    preview_id = _approved_file_id(detail.get("preview_file_id"))
    digest: str | None = None
    if render_id is not None:
        stored = brand_kits.get_file(db, org.id, render_id, kinds=(FileKind.RENDER, FileKind.PREVIEW))
        if stored is None:
            render_id = None
        else:
            digest = stored.sha256 or None
    _with_detail(
        asset,
        **{APPROVED_RENDER_KEY: render_id, APPROVED_PREVIEW_KEY: preview_id, APPROVED_RENDER_SHA_KEY: digest},
    )
    log.info("design approved: asset=%s render_recorded=%s", asset.id, render_id is not None)


def _release_social_now(db: Session, org: Organization, asset: MarketingAsset, now: datetime) -> MarketingAsset:
    """Put a due social post on the schedule for now and run the publisher for it alone.

    The publisher commits as it goes. A post it holds (a stop, the daily
    limit, the platform asking to wait) stays scheduled; one the platform
    refused is reported, so its approval stays in the queue with the reason.
    """
    asset.status = AssetStatus.SCHEDULED
    asset.scheduled_for = now
    asset.error = ""
    asset.updated_at = now
    db.flush()
    publish_due(db, org, only_ids=[asset.id])
    if asset.status == AssetStatus.FAILED:
        raise ConflictError(asset.error or _PUBLISH_FAILED)
    return asset


def _register_approval_handlers() -> None:
    from app.services.approvals import register_applier, register_rejecter

    @register_applier(TARGET_KIND)
    def apply_asset(db: Session, org: Organization, item, payload: dict) -> None:  # noqa: ANN001
        asset = get(db, tenant_id=org.id, asset_id=payload.get("asset_id") or item.target_id or "")
        release(db, org, asset, OrgCipher.for_org(org))

    @register_rejecter(TARGET_KIND)
    def reject_asset(db: Session, org: Organization, item, payload: dict) -> None:  # noqa: ANN001
        asset = _tenant_row(db, MarketingAsset, org.id, payload.get("asset_id") or item.target_id)
        if asset is not None and asset.status not in (AssetStatus.PUBLISHED, AssetStatus.EXPORTED):
            asset.status = AssetStatus.REJECTED
            asset.updated_at = utcnow()
            db.flush()


_register_approval_handlers()


# ── Asking for changes and editing text, by asset kind ─────────────────────
#: Regenerates one asset from reviewer feedback (the creative agent's).
AssetReviser = Callable[[Session, Organization, MarketingAsset, "ApprovalItem", "RevisionRequest"], "RevisionOutcome"]
#: Validates and saves edited text on one asset. No model is called.
AssetEditor = Callable[[Session, Organization, MarketingAsset, "ApprovalItem", dict[str, str]], "RevisionOutcome"]
#: Redraws an asset after its text changed; given the saved content.
Rerenderer = Callable[[Session, Organization, MarketingAsset, dict], None]

ASSET_REVISERS: dict[str, AssetReviser] = {}
ASSET_EDITORS: dict[str, AssetEditor] = {}
RERENDERERS: dict[str, Rerenderer] = {}

#: The text a reviewer may correct on a creative. Everything else (claims,
#: disclaimer, colours) comes from approved sources and is not editable here.
CREATIVE_EDITABLE_FIELDS = frozenset({"headline", "body", "cta"})

#: Statuses in which an asset's work is finished or under way on a platform.
_SETTLED = (AssetStatus.PUBLISHING, AssetStatus.PUBLISHED, AssetStatus.EXPORTED)


def register_asset_reviser(kind: str) -> Callable[[AssetReviser], AssetReviser]:
    def decorator(fn: AssetReviser) -> AssetReviser:
        ASSET_REVISERS[kind] = fn
        return fn

    return decorator


def register_asset_editor(kind: str) -> Callable[[AssetEditor], AssetEditor]:
    def decorator(fn: AssetEditor) -> AssetEditor:
        ASSET_EDITORS[kind] = fn
        return fn

    return decorator


def register_rerenderer(kind: str) -> Callable[[Rerenderer], Rerenderer]:
    def decorator(fn: Rerenderer) -> Rerenderer:
        RERENDERERS[kind] = fn
        return fn

    return decorator


def _asset_for(db: Session, org: Organization, item: "ApprovalItem", payload: dict) -> MarketingAsset:
    asset_id = payload.get("asset_id") if isinstance(payload.get("asset_id"), str) else None
    asset = get(db, tenant_id=org.id, asset_id=asset_id or item.target_id or "")
    if asset.status in _SETTLED:
        raise ConflictError("This has already gone out, so it can't be changed here.")
    return asset


def _preview_id(asset: MarketingAsset) -> str | None:
    value = (asset.detail or {}).get("preview_file_id")
    return value if isinstance(value, str) and 0 < len(value) <= 64 else None


def _tenant_row(db: Session, model: type, tenant_id: str, row_id: object) -> Any:  # noqa: ANN401 - ORM row
    """A row by id, only from this tenant; ``None`` for anything else."""
    if not isinstance(row_id, str) or not row_id or len(row_id) > 64:
        return None
    return db.execute(
        select(model).where(model.id == row_id, model.tenant_id == tenant_id)
    ).scalars().first()


def _validation_message(exc: ValidationError) -> str:
    """The first problem in plain words. Pydantic's ``msg`` never includes
    the submitted value; our own validators' wording is fixed."""
    errors = exc.errors(include_input=False, include_url=False, include_context=False)
    message = str(errors[0].get("msg", "")) if errors else ""
    message = message.removeprefix("Value error, ")
    return message or "That text isn't allowed."


def edit_creative_text(
    db: Session, org: Organization, asset: MarketingAsset, item: "ApprovalItem", fields: dict[str, str]
) -> "RevisionOutcome":
    """Apply a reviewer's text correction to a creative.

    Re-checked exactly like generated copy: the format's limits and the
    approved-claim indices (``parse_copy``), then the content QA (banned
    phrases, unprovable promises, numbers the product does not state). On
    success the copy is saved and the design is redrawn by the registered
    re-renderer. Nothing here calls a model.

    Content contract for a creative asset: ``content["format_id"]`` and
    ``content["copy"]`` (``headline``, ``body``, ``cta``, ``claim_ids``);
    ``detail["product_id"]`` and ``detail["brand_kit_id"]``.
    """
    from app.models.creative import BrandKit, Product
    from app.services.approvals import RevisionOutcome
    from app.services.creative.formats import get_format
    from app.services.creative.qa import ProductFacts, check_copy_content
    from app.services.creative.schema import parse_copy

    unknown = sorted(set(fields) - CREATIVE_EDITABLE_FIELDS)
    if unknown:
        raise InvalidInputError(
            "Only the headline, body text and button text can be edited here.",
            details={"fields": unknown},
        )
    rerender = RERENDERERS.get(asset.kind)
    if rerender is None:
        raise ConflictError("This design can't be redrawn with new text yet. Ask for changes instead.")

    cipher = OrgCipher.for_org(org)
    content = content_of(asset, cipher)
    copy = content.get("copy")
    format_id = content.get("format_id")
    if not isinstance(copy, dict) or not isinstance(format_id, str):
        raise ConflictError("This design has no text that can be edited.")
    try:
        spec = get_format(format_id)
    except ValueError as exc:
        raise ConflictError("This design's size is no longer available, so its text can't be edited.") from exc

    detail = asset.detail or {}
    product = _tenant_row(db, Product, org.id, detail.get("product_id"))
    kit = _tenant_row(db, BrandKit, org.id, detail.get("brand_kit_id"))
    approved = list(product.approved_claims or []) if product is not None else []

    merged = {key: copy[key] for key in ("headline", "body", "cta", "claim_ids") if key in copy}
    merged.update(fields)
    try:
        parsed = parse_copy(merged, format_id=format_id, approved_claims=approved)
    except ValidationError as exc:
        raise InvalidInputError(_validation_message(exc)) from exc

    report = check_copy_content(
        parsed,
        spec=spec,
        facts=ProductFacts.from_product(product) if product is not None else ProductFacts(),
        banned_phrases=(kit.banned_phrases or []) if kit is not None else (),
    )
    if not report.passed:
        first = next(i for i in report.issues if i.severity == "error")
        raise InvalidInputError(first.message, details={"qa": report.to_dict()})

    updated = {**content, "copy": parsed.model_dump()}
    set_content(asset, cipher, updated)
    if asset.status == AssetStatus.FAILED:
        asset.status = AssetStatus.DRAFT
        asset.error = ""
    db.flush()
    rerender(db, org, asset, updated)
    return RevisionOutcome(approval_id=item.id, message="Text updated. The design has been redrawn.")


register_asset_editor(AssetKind.CREATIVE)(edit_creative_text)


def _register_revision_handlers() -> None:
    from app.services.approvals import (
        RevisionSupport,
        register_editor,
        register_reviser,
        register_support_probe,
    )

    @register_reviser(TARGET_KIND)
    def revise_asset(db: Session, org: Organization, item, payload: dict, request) -> "RevisionOutcome":  # noqa: ANN001
        asset = _asset_for(db, org, item, payload)
        reviser = ASSET_REVISERS.get(asset.kind)
        if reviser is None:
            raise ConflictError("Changes can't be requested for this kind of content. Edit it instead.")
        return reviser(db, org, asset, item, request)

    @register_editor(TARGET_KIND)
    def edit_asset(db: Session, org: Organization, item, payload: dict, fields: dict[str, str]) -> "RevisionOutcome":  # noqa: ANN001
        asset = _asset_for(db, org, item, payload)
        editor = ASSET_EDITORS.get(asset.kind)
        if editor is None:
            raise ConflictError("The text of this kind of content can't be edited here.")
        return editor(db, org, asset, item, fields)

    @register_support_probe(TARGET_KIND)
    def asset_support(db: Session, org: Organization, entries: list) -> dict[str, "RevisionSupport"]:
        ids = {
            item.id: (payload.get("asset_id") if isinstance(payload.get("asset_id"), str) else None)
            or item.target_id
            for item, payload in entries
        }
        wanted = sorted({value for value in ids.values() if value})
        if not wanted:
            return {}
        rows = db.execute(
            select(MarketingAsset).where(
                MarketingAsset.tenant_id == org.id, MarketingAsset.id.in_(wanted)
            )
        ).scalars()
        by_id = {asset.id: asset for asset in rows}
        out: dict[str, RevisionSupport] = {}
        for item_id, asset_id in ids.items():
            asset = by_id.get(asset_id or "")
            if asset is None:
                continue
            open_ = asset.status not in _SETTLED
            out[item_id] = RevisionSupport(
                revise=open_ and asset.kind in ASSET_REVISERS,
                edit=open_ and asset.kind in ASSET_EDITORS and asset.kind in RERENDERERS,
                preview_file_id=_preview_id(asset),
            )
        return out


_register_revision_handlers()


# ── Scheduled publishing ───────────────────────────────────────────────────
#: Most items one pass claims in one workspace; the rest wait for the next pass.
PUBLISH_BATCH = 25
#: A post still "publishing" after this long was interrupted mid-send (a
#: worker died). It is failed with a "check the platform" note, never resent:
#: the platform may already have it.
STALE_PUBLISHING_MINUTES = 30
#: Times a post may be moved later because the platform asked us to wait,
#: before it is failed. Reddit asks for an hour at most, so this is two days.
MAX_DEFERRALS = 48
#: Longest wait a platform may ask for before we retry.
MAX_DEFER_SECONDS = 6 * 3600
#: Detail key counting the platform's "wait" answers (see :data:`MAX_DEFERRALS`).
DEFERRALS_KEY = "deferrals"
#: Set in a rate-limit error's ``details`` when the wait is this platform's own
#: spacing between posts (Reddit), which is not counted as a deferral.
SPACING_DETAIL = "spacing"
#: Longest gap between retries of a post the platform could not take.
MAX_RETRY_MINUTES = 24 * 60
#: Social posts one workspace may publish in any 24 hours, unless its
#: settings say otherwise (``org.settings[DAILY_CAP_SETTING]``, bounded).
#: The server-wide default is ``SOCIAL_DAILY_POST_CAP``; this is its default.
DEFAULT_DAILY_SOCIAL_POSTS = 25
MAX_DAILY_SOCIAL_POSTS = 500
DAILY_CAP_SETTING = "social_daily_post_cap"

_RATE_LIMITED = "The platform asked us to wait, so this post has been moved a little later."
_INTERRUPTED = (
    "Publishing was interrupted, so we couldn't confirm whether this post went out. "
    "Check the platform before posting it again."
)
_PUBLISH_FAILED = "The platform did not accept this."
_TOO_MANY_WAITS = "The platform kept asking us to wait, so we stopped trying. Schedule it again later."
_STOPPED_NOTE = "Agents were stopped, so this post waits. It goes out once they are resumed."


def retry_after_seconds(exc: BaseException) -> int | None:
    """Seconds the platform asked us to wait, or ``None`` when it did not ask.

    Any connector error coded ``rate_limited`` with a numeric
    ``details["retry_after_seconds"]`` (Reddit's ``RedditRateLimited``, the
    workspace's own Reddit spacing) counts. Bounded to
    :data:`MAX_DEFER_SECONDS`.
    """
    if not isinstance(exc, ConnectorError) or isinstance(exc, ConnectorConfigError):
        return None
    if getattr(exc, "code", "") != "rate_limited":
        return None
    value = (getattr(exc, "details", None) or {}).get("retry_after_seconds")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(min(max(value, 1), MAX_DEFER_SECONDS))


def is_transient(exc: BaseException) -> bool:
    """Whether trying the same post again later may work.

    A :class:`ConnectorError` is the platform being unreachable or failing;
    a :class:`ConnectorConfigError` (a refused post, a missing permission) and
    every other error will fail the same way again.
    """
    return isinstance(exc, ConnectorError) and not isinstance(exc, ConnectorConfigError)


def daily_social_cap(org: Organization) -> int:
    """Social posts this workspace may publish in any 24 hours."""
    raw = (getattr(org, "settings", None) or {}).get(DAILY_CAP_SETTING)
    if isinstance(raw, bool) or not isinstance(raw, int):
        raw = settings.social_daily_post_cap
    return min(max(raw, 1), MAX_DAILY_SOCIAL_POSTS)


def _with_detail(asset: MarketingAsset, **values: object) -> None:
    """Set keys on ``detail``. Reassigned, not changed in place, so the JSON column is saved."""
    detail = dict(asset.detail or {})
    detail.update(values)
    asset.detail = detail


def _defer(asset: MarketingAsset, *, seconds: int, note: str, counter: str | None = None) -> None:
    """Put the post back on the schedule ``seconds`` from now, with a plain note.

    ``counter`` names the detail key this wait adds one to: ``"deferrals"``
    when the platform asked us to wait. A retry is already counted in
    ``publish_attempts`` and our own spacing between posts is not counted,
    so neither uses up the other's allowance.
    """
    now = utcnow()
    asset.status = AssetStatus.SCHEDULED
    asset.scheduled_for = now + timedelta(seconds=seconds)
    asset.error = ""
    asset.updated_at = now
    values: dict[str, object] = {"publish_note": note[:500]}
    if counter is not None:
        values[counter] = int((asset.detail or {}).get(counter) or 0) + 1
    _with_detail(asset, **values)


def is_own_spacing(exc: BaseException) -> bool:
    """Whether a "wait" came from this platform's own spacing between posts, not the network."""
    return bool((getattr(exc, "details", None) or {}).get(SPACING_DETAIL))


def _fail(asset: MarketingAsset, reason: str) -> None:
    asset.status = AssetStatus.FAILED
    asset.error = reason[:1000]
    asset.updated_at = utcnow()


def _claim_due(db: Session, org: Organization, now: datetime, only_ids: Sequence[str] | None) -> list[MarketingAsset]:
    """Lock the workspace's due posts and mark them "publishing".

    ``FOR UPDATE SKIP LOCKED``: rows another publisher has already locked are
    skipped, not waited for. The caller commits the new status before calling
    any platform, so a second publisher arriving later sees "publishing" and
    leaves the row alone. Together, no post can be claimed twice.
    """
    stmt = select(MarketingAsset).where(
        MarketingAsset.tenant_id == org.id,
        MarketingAsset.status == AssetStatus.SCHEDULED,
        MarketingAsset.scheduled_for <= now,
    )
    if only_ids is not None:
        stmt = stmt.where(MarketingAsset.id.in_(list(only_ids)))
    stmt = stmt.order_by(MarketingAsset.scheduled_for).limit(PUBLISH_BATCH).with_for_update(skip_locked=True)
    claimed = list(db.execute(stmt).scalars())
    for asset in claimed:
        asset.status = AssetStatus.PUBLISHING
        asset.updated_at = now
    db.flush()
    return claimed


def _recover_interrupted(db: Session, org: Organization, now: datetime) -> int:
    """Fail posts left "publishing" by a publisher that never finished."""
    stale = list(
        db.execute(
            select(MarketingAsset)
            .where(
                MarketingAsset.tenant_id == org.id,
                MarketingAsset.status == AssetStatus.PUBLISHING,
                MarketingAsset.updated_at < now - timedelta(minutes=STALE_PUBLISHING_MINUTES),
            )
            .limit(PUBLISH_BATCH)
            .with_for_update(skip_locked=True)
        ).scalars()
    )
    for asset in stale:
        _fail(asset, _INTERRUPTED)
        log.warning("publish interrupted: asset=%s marked failed", asset.id)
    return len(stale)


def _posted_last_day(db: Session, tenant_id: str, now: datetime) -> tuple[int, datetime | None]:
    """How many social posts went out in the last 24 hours, and the oldest of them."""
    row = db.execute(
        select(func.count(MarketingAsset.id), func.min(MarketingAsset.published_at)).where(
            MarketingAsset.tenant_id == tenant_id,
            MarketingAsset.kind == AssetKind.SOCIAL_POST,
            MarketingAsset.status == AssetStatus.PUBLISHED,
            MarketingAsset.published_at >= now - timedelta(days=1),
        )
    ).one()
    return int(row[0] or 0), row[1]


def _hold_for_cap(asset: MarketingAsset, *, cap: int, oldest: datetime | None, now: datetime) -> None:
    """Move a post past the daily limit to when the limit frees up. Not an attempt."""
    if oldest is not None and oldest.tzinfo is None:
        oldest = oldest.replace(tzinfo=timezone.utc)
    free_at = (oldest + timedelta(days=1, minutes=1)) if oldest is not None else now + timedelta(
        minutes=settings.social_publish_retry_minutes
    )
    asset.status = AssetStatus.SCHEDULED
    asset.scheduled_for = max(free_at, now + timedelta(minutes=1))
    asset.updated_at = now
    _with_detail(
        asset,
        publish_note=f"This workspace has reached its limit of {cap} posts a day, so this one goes out a little later.",
    )


def _hold_for_stop(held: Sequence[MarketingAsset], *, now: datetime) -> None:
    """Put claimed posts back on the schedule, still due, because a stop was pressed. Not an attempt."""
    for asset in held:
        if asset.status != AssetStatus.PUBLISHING:
            continue
        asset.status = AssetStatus.SCHEDULED
        asset.updated_at = now
        _with_detail(asset, publish_note=_STOPPED_NOTE)


def _relock_publishing(db: Session, org: Organization, asset: MarketingAsset) -> MarketingAsset | None:
    """The claimed row read again under a row lock, or ``None`` when it is no longer "publishing".

    Between the claim and the send another publisher may have marked it
    interrupted, or a person cancelled it; then it must not be sent.
    """
    row = db.execute(
        select(MarketingAsset)
        .where(MarketingAsset.id == asset.id, MarketingAsset.tenant_id == org.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalars().first()
    if row is None or row.status != AssetStatus.PUBLISHING:
        return None
    return row


def _send_claimed(db: Session, org: Organization, asset: MarketingAsset, cipher: OrgCipher) -> bool:
    """Publish one claimed post and record the outcome on it. True when it went out.

    * No longer "publishing" when read again under a lock → left alone.
    * The platform asked us to wait → back on the schedule at that time.
    * A social post the platform could not take right now → retried with
      growing gaps, ``social_publish_max_attempts`` times in all.
    * Anything else, or attempts used up → failed with a plain reason.
    """
    from app.core.user_messages import public_error_message

    locked = _relock_publishing(db, org, asset)
    if locked is None:
        log.info("publish skipped: asset=%s is no longer publishing", asset.id)
        return False
    asset = locked
    publisher = PUBLISHERS.get(asset.kind)
    if publisher is None:
        _fail(asset, "This kind of content cannot be published directly; download it instead.")
        return False
    try:
        publisher(db, org, asset, content_of(asset, cipher))
    except Exception as exc:  # noqa: BLE001 - every outcome is recorded on the asset and logged
        if isinstance(exc, SQLAlchemyError):
            # The transaction is unusable; the claimed status is already
            # committed, so roll back and record the failure afresh.
            db.rollback()
        detail = asset.detail or {}
        wait = retry_after_seconds(exc)
        if wait is not None:
            spacing = is_own_spacing(exc)
            if not spacing and int(detail.get(DEFERRALS_KEY) or 0) >= MAX_DEFERRALS:
                _fail(asset, _TOO_MANY_WAITS)
            else:
                _defer(
                    asset,
                    seconds=wait,
                    note=public_error_message(exc, fallback=_RATE_LIMITED),
                    counter=None if spacing else DEFERRALS_KEY,
                )
            log.info("publish deferred: asset=%s wait=%ss spacing=%s", asset.id, wait, spacing)
            return False
        attempts = int(detail.get("publish_attempts") or 0) + 1
        _with_detail(asset, publish_attempts=attempts)
        reason = public_error_message(exc, fallback=_PUBLISH_FAILED)
        if asset.kind == AssetKind.SOCIAL_POST and is_transient(exc) and attempts < settings.social_publish_max_attempts:
            minutes = min(settings.social_publish_retry_minutes * 2 ** (attempts - 1), MAX_RETRY_MINUTES)
            _defer(asset, seconds=minutes * 60, note=f"{reason} We'll try again in {minutes} minutes.")
            log_failure(log, "scheduled publish will retry", exc, asset_id=asset.id, attempt=attempts)
            return False
        _fail(asset, reason)
        log_failure(log, "scheduled publish failed", exc, asset_id=asset.id, attempt=attempts)
        return False
    asset.status = AssetStatus.PUBLISHED
    asset.published_at = asset.published_at or utcnow()
    asset.error = ""
    asset.updated_at = utcnow()
    return True


def publish_due(db: Session, org: Organization, *, only_ids: Sequence[str] | None = None) -> int:
    """Publish scheduled assets whose time has come. Returns how many went out.

    Safe to run from several schedulers at once: each due row is claimed
    with ``SELECT … FOR UPDATE SKIP LOCKED`` and committed as "publishing"
    before its platform is called (:func:`_claim_due`). Commits as it goes,
    one post at a time, so a crash part-way loses nothing already recorded.

    Nothing is sent while an emergency stop is on (the platform's or the
    workspace's). The stop is read again before every post, so one pressed
    part-way through a pass holds the rest: they go back on the schedule,
    still due. Social posts respect the workspace's daily limit
    (:func:`daily_social_cap`). ``only_ids`` limits the pass to those posts
    ("post now").
    """
    from app.services import agent_safety

    if agent_safety.halt_reason(db, org):
        log.info("scheduled publishing held: workspace=%s is stopped", org.id)
        return 0
    cipher = OrgCipher.for_org(org)
    now = utcnow()
    _recover_interrupted(db, org, now)
    claimed = _claim_due(db, org, now, only_ids)
    db.commit()
    if not claimed:
        return 0

    cap = daily_social_cap(org)
    posted, oldest = _posted_last_day(db, org.id, now)
    sent = 0
    for index, asset in enumerate(claimed):
        if agent_safety.halt_reason(db, org):
            _hold_for_stop(claimed[index:], now=utcnow())
            db.commit()
            log.info("scheduled publishing held mid-pass: workspace=%s held=%d", org.id, len(claimed) - index)
            break
        if asset.kind == AssetKind.SOCIAL_POST and posted >= cap:
            _hold_for_cap(asset, cap=cap, oldest=oldest, now=utcnow())
            log.info("publish held by daily cap: asset=%s cap=%d", asset.id, cap)
        elif _send_claimed(db, org, asset, cipher):
            sent += 1
            if asset.kind == AssetKind.SOCIAL_POST:
                posted += 1
                oldest = oldest or asset.published_at
        db.commit()
    return sent
