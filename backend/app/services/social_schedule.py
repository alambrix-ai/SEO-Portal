"""Share an approved design on social networks, now or at a chosen time.

A person picks an approved Creative Studio design, the networks to post it
to, a caption per network and either "post now" or a local date and time in
the workspace's time zone. Nothing here calls a model: the caption is the
person's own text, checked against each network's limits
(:func:`app.agents.social_media_manager.platforms.problem`).

Each (design, network, time) becomes one ``social_post``
:class:`~app.models.content.MarketingAsset`, status ``scheduled``, with its
own copy of the design's picture as a :class:`GeneratedImage` (JPEG for
Instagram). The scheduled publisher (:func:`app.services.assets.publish_due`)
sends it when it is due; "post now" posts are due at once and are also
published straight away in the background.

Rules enforced here, server-side:

* the design belongs to the caller's workspace (another workspace's design
  is "not found") and is approved;
* each network's connection is connected and can publish (Reddit only while
  the platform has Reddit switched on);
* a time is at least a minute and at most a year ahead, and exists in the
  workspace's time zone (not skipped by a clock change);
* the same design is never scheduled twice for the same network, subreddit
  and time (an idempotency key in ``detail``, checked under a per-design
  advisory lock so two clicks cannot both pass).

Captions, titles and links are kept only in the encrypted content; logs
carry ids and counts.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.agents.social_media_manager import platforms
from app.core.exceptions import ConflictError, InvalidInputError, NotFoundError
from app.core.logging import get_logger
from app.db.base import utcnow
from app.models.content import AssetKind, AssetStatus, GeneratedImage, MarketingAsset
from app.models.creative import FileKind, StoredFile
from app.models.workspace import Organization
from app.services import assets
from app.services.encryption import OrgCipher

log = get_logger(__name__)

#: Most networks one request may schedule.
MAX_ITEMS = 12
#: Longest caption accepted (Reddit's body limit; each network's own limit
#: is checked after).
MAX_CAPTION_CHARS = 40_000
MAX_HASHTAGS = 30
MAX_HASHTAG_CHARS = 100
MAX_LINK_CHARS = 2000
MAX_TITLE_CHARS = 300
#: How soon and how far ahead a post may be scheduled.
MIN_LEAD = timedelta(minutes=1)
MAX_LEAD = timedelta(days=365)
#: Most posts one status list returns.
MAX_LIST = 100
#: Detail keys this service writes on each post.
SOURCE_KEY = "source_asset_id"
IDEMPOTENCY_KEY = "idempotency_key"
#: Statuses in which a post counts as already scheduled or sent.
ACTIVE_STATUSES = (AssetStatus.SCHEDULED, AssetStatus.PUBLISHING, AssetStatus.PUBLISHED)
#: A design must be in one of these to be shared.
SHAREABLE_STATUSES = (AssetStatus.APPROVED, AssetStatus.EXPORTED)
#: The design's file to share, as (current key, key recorded at approval):
#: the approved full render first, then the approved preview. Only raster
#: files can be posted.
_FILE_KEYS = (
    ("render_file_id", assets.APPROVED_RENDER_KEY),
    ("preview_file_id", assets.APPROVED_PREVIEW_KEY),
)
_POSTABLE_MIMES = ("image/png", "image/jpeg", "image/webp")
_NOT_APPROVED_FILE = (
    "We can't find the approved version of this design's picture. Ask for changes and "
    "approve the new version, then share it."
)
_CHANGED_SINCE_APPROVAL = (
    "This design has changed since it was approved. Ask for changes and approve the new "
    "version before sharing it."
)
#: The agent the posts are filed under when the design names none.
DEFAULT_AGENT_SLUG = "creative_studio"

_UUID_PATTERN = r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
_WHEN_PATTERN = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$"

STATUS_LABELS = {
    AssetStatus.SCHEDULED: "Scheduled",
    AssetStatus.PUBLISHING: "Posting now",
    AssetStatus.PUBLISHED: "Posted",
    AssetStatus.FAILED: "Failed",
    AssetStatus.REJECTED: "Cancelled",
}


# ── Input ──────────────────────────────────────────────────────────────────
class ScheduleItem(BaseModel):
    """One network's post. ``when_local`` is ``YYYY-MM-DDTHH:MM`` in the workspace zone; null posts now."""

    model_config = ConfigDict(extra="forbid")

    network: str = Field(min_length=1, max_length=24)
    caption: str = Field(default="", max_length=MAX_CAPTION_CHARS)
    hashtags: list[Annotated[str, Field(max_length=MAX_HASHTAG_CHARS)]] = Field(
        default_factory=list, max_length=MAX_HASHTAGS
    )
    link: str = Field(default="", max_length=MAX_LINK_CHARS)
    title: str = Field(default="", max_length=MAX_TITLE_CHARS)
    subreddit: str | None = Field(default=None, max_length=24)
    when_local: str | None = Field(default=None, pattern=_WHEN_PATTERN)


class ScheduleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    creative_asset_id: str = Field(pattern=_UUID_PATTERN)
    items: list[ScheduleItem] = Field(min_length=1, max_length=MAX_ITEMS)


@dataclass(slots=True)
class ScheduleResult:
    posts: list[MarketingAsset] = field(default_factory=list)
    #: Posts to publish straight away ("post now").
    post_now_ids: list[str] = field(default_factory=list)
    #: The workspace time zone's name, for showing local times.
    zone_name: str = "UTC"


@dataclass(slots=True)
class _Prepared:
    network: str
    rules: platforms.Network
    content: dict[str, Any]
    when: datetime | None
    subreddit: str
    key: str


# ── The design ─────────────────────────────────────────────────────────────
def load_creative(db: Session, tenant_id: str, creative_id: str) -> MarketingAsset:
    """The design ``creative_id`` of this workspace. Anything else is "not found"."""
    canonical = (creative_id or "").strip().lower()
    asset = db.execute(
        select(MarketingAsset).where(
            MarketingAsset.id == canonical,
            MarketingAsset.tenant_id == tenant_id,
            MarketingAsset.kind == AssetKind.CREATIVE,
        )
    ).scalars().first()
    if asset is None:
        raise NotFoundError("That design is no longer in the library.")
    return asset


def _design_file(db: Session, tenant_id: str, creative: MarketingAsset) -> StoredFile:
    """The design's approved picture, loaded only from this workspace.

    Only the files recorded when the design was approved
    (``detail["approved_render_file_id"]``, then the approved preview when the
    render is not a picture, e.g. a print PDF) can be posted, and only while
    the design still shows them. A design approved before approvals were
    recorded, or changed since (restored, redrawn), is refused with 409.
    """
    from app.services import brand_kits

    detail = creative.detail or {}
    approved_render = detail.get(assets.APPROVED_RENDER_KEY)
    if not isinstance(approved_render, str) or not approved_render:
        raise ConflictError(_NOT_APPROVED_FILE)
    for key, approved_key in _FILE_KEYS:
        approved = detail.get(approved_key)
        if not isinstance(approved, str) or not approved:
            continue
        current = detail.get(key) or (detail.get("file_id") if key == "render_file_id" else None)
        if current != approved:
            log.warning("design changed since approval: design=%s key=%s", creative.id, key)
            raise ConflictError(_CHANGED_SINCE_APPROVAL)
        stored = brand_kits.get_file(db, tenant_id, approved, kinds=(FileKind.RENDER, FileKind.PREVIEW))
        if stored is None:
            raise ConflictError(_NOT_APPROVED_FILE)
        if key == "render_file_id":
            digest = detail.get(assets.APPROVED_RENDER_SHA_KEY)
            if isinstance(digest, str) and digest and stored.sha256 != digest:
                log.warning("design file differs from the approved one: design=%s", creative.id)
                raise ConflictError(_CHANGED_SINCE_APPROVAL)
        if stored.mime in _POSTABLE_MIMES:
            return stored
    raise ConflictError("This design has no picture that can be posted. Download it and post it yourself.")


def _copy_picture(db: Session, org: Organization, stored: StoredFile, data: bytes, *, network: str) -> GeneratedImage:
    """A copy of the design's picture for one post. Instagram's is converted to JPEG."""
    image = GeneratedImage(
        tenant_id=org.id,
        created_at=utcnow(),
        prompt="",
        provider="creative_studio",
        mime=stored.mime,
        width=int(stored.width or 0),
        height=int(stored.height or 0),
        data=data,
    )
    if network == "instagram":
        from app.agents.social_media_manager.agent import as_jpeg

        if not as_jpeg(image):
            raise ConflictError("This design's picture couldn't be prepared for Instagram. Try another network.")
    db.add(image)
    db.flush()
    return image


# ── Checks ─────────────────────────────────────────────────────────────────
def local_to_utc(when_local: str, zone: ZoneInfo | timezone, zone_name: str, *, now: datetime) -> datetime:
    """``YYYY-MM-DDTHH:MM`` in ``zone`` as UTC, between a minute and a year from ``now``."""
    try:
        naive = datetime.strptime(when_local, "%Y-%m-%dT%H:%M")
    except (TypeError, ValueError):
        raise InvalidInputError("Choose a date and time for this post.") from None
    local = naive.replace(tzinfo=zone)
    utc = local.astimezone(timezone.utc)
    # A time skipped when the clocks go forward does not come back the same.
    if utc.astimezone(zone).replace(tzinfo=None) != naive:
        raise InvalidInputError(
            f"{when_local.replace('T', ' ')} doesn't exist in {zone_name} because the clocks change then. "
            "Choose another time."
        )
    if utc < now + MIN_LEAD:
        raise InvalidInputError("That time has already passed. Choose a time at least a minute from now, or post now.")
    if utc > now + MAX_LEAD:
        raise InvalidInputError("Posts can be scheduled up to a year ahead. Choose an earlier date.")
    return utc


def _checked_link(link: str) -> str:
    """A plain http(s) web address, or "" for none."""
    value = (link or "").strip()
    if not value:
        return ""
    try:
        parts = urlsplit(value)
    except ValueError:
        parts = None
    if (
        parts is None
        or parts.scheme.lower() not in ("http", "https")
        or not parts.hostname
        or parts.username
        or parts.password
        or any(ch.isspace() for ch in value)
    ):
        raise InvalidInputError("The link must be a web address starting with https://.")
    return value


def _checked_subreddit(raw: str | None) -> str:
    from app.connectors.reddit.connector import normalise_subreddit

    if not (raw or "").strip():
        raise InvalidInputError("Choose the subreddit this post goes to.")
    return normalise_subreddit(raw)


def idempotency_key(tenant_id: str, creative_id: str, network: str, subreddit: str, when: datetime | None) -> str:
    """One key per (design, network, subreddit, minute); "now" for post-now."""
    slot = when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%MZ") if when is not None else "now"
    raw = f"{tenant_id}:{creative_id}:{network}:{subreddit.lower()}:{slot}"
    return hashlib.sha256(raw.encode()).hexdigest()


def _prepare(
    item: ScheduleItem,
    *,
    tenant_id: str,
    creative_id: str,
    zone: ZoneInfo | timezone,
    zone_name: str,
    now: datetime,
) -> _Prepared:
    network = item.network.strip().lower()
    rules = platforms.rules_for(network)
    if rules is None:
        raise InvalidInputError("Choose a network this design can be posted to.")
    when = local_to_utc(item.when_local, zone, zone_name, now=now) if item.when_local else None
    subreddit = _checked_subreddit(item.subreddit) if network == "reddit" else ""
    content: dict[str, Any] = {
        "network": network,
        "text": item.caption.strip(),
        "hashtags": platforms.clean_hashtags(item.hashtags, MAX_HASHTAGS) if item.hashtags else [],
        "link": _checked_link(item.link),
        "title": " ".join(item.title.split()),
        "timezone": zone_name,
    }
    if subreddit:
        content["subreddit"] = subreddit
    issue = platforms.problem(network, content)
    if issue:
        raise InvalidInputError(issue, details={"network": network})
    return _Prepared(
        network=network,
        rules=rules,
        content=content,
        when=when,
        subreddit=subreddit,
        key=idempotency_key(tenant_id, creative_id, network, subreddit, when),
    )


def _publishable(db: Session, org: Organization) -> frozenset[str]:
    """Connector slugs that are connected, usable and able to publish posts."""
    from app.services.connectors import build_connector_bundle

    bundle = build_connector_bundle(db, org)
    try:
        return frozenset(slug for slug, c in bundle.instances.items() if callable(getattr(c, "publish_post", None)))
    finally:
        bundle.close()


def _check_connected(db: Session, org: Organization, prepared: list[_Prepared]) -> None:
    from app.connectors.base import registry

    usable = _publishable(db, org)
    for post in prepared:
        if not registry.is_offered(post.rules.connector):
            raise ConflictError(f"Posting to {post.rules.label} is switched off on this platform.")
        if post.rules.connector not in usable:
            raise ConflictError(
                f"{post.rules.label} isn't connected, so this design can't be posted there. "
                "Connect it under Integrations and try again."
            )


def _lock_design(db: Session, tenant_id: str, creative_id: str) -> None:
    """Serialise scheduling of one design, so two clicks cannot both pass the duplicate check."""
    if db.get_bind().dialect.name != "postgresql":
        return
    digest = hashlib.sha256(f"social-schedule:{tenant_id}:{creative_id}".encode()).digest()
    db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": int.from_bytes(digest[:8], "big", signed=True)})


def _existing_keys(db: Session, tenant_id: str, creative_id: str) -> set[str]:
    rows = db.execute(
        select(MarketingAsset.detail).where(
            MarketingAsset.tenant_id == tenant_id,
            MarketingAsset.kind == AssetKind.SOCIAL_POST,
            MarketingAsset.status.in_(ACTIVE_STATUSES),
            MarketingAsset.detail[SOURCE_KEY].astext == creative_id,
        )
    ).scalars()
    return {str((detail or {}).get(IDEMPOTENCY_KEY) or "") for detail in rows} - {""}


def _duplicate_message(post: _Prepared) -> str:
    where = f" r/{post.subreddit}" if post.subreddit else ""
    if post.when is None:
        return (
            f"This design has already been posted to {post.rules.label}{where}. "
            "To post it again, choose a time instead of posting now."
        )
    return f"This design is already scheduled for {post.rules.label}{where} at that time."


# ── Scheduling ─────────────────────────────────────────────────────────────
def schedule(db: Session, org: Organization, *, user_id: str, request: ScheduleRequest) -> ScheduleResult:
    """Create one scheduled post per item. Flushes; the caller audits and commits."""
    creative = load_creative(db, org.id, request.creative_asset_id)
    if creative.status not in SHAREABLE_STATUSES:
        raise ConflictError("Approve this design before sharing it.")

    zone, zone_name = platforms.zone_for(getattr(org, "settings", None))
    now = utcnow()
    prepared = [
        _prepare(item, tenant_id=org.id, creative_id=creative.id, zone=zone, zone_name=zone_name, now=now)
        for item in request.items
    ]
    seen: set[str] = set()
    for post in prepared:
        if post.key in seen:
            raise InvalidInputError(f"{post.rules.label} is listed twice for the same time. Remove one.")
        seen.add(post.key)

    _check_connected(db, org, prepared)
    _lock_design(db, org.id, creative.id)
    existing = _existing_keys(db, org.id, creative.id)
    for post in prepared:
        if post.key in existing:
            raise ConflictError(_duplicate_message(post))

    from app.services.storage import storage_for

    stored = _design_file(db, org.id, creative)
    data = storage_for(stored).get(db, tenant_id=org.id, file_id=stored.id)
    cipher = OrgCipher.for_org(org)
    result = ScheduleResult(zone_name=zone_name)
    for post in prepared:
        image = _copy_picture(db, org, stored, data, network=post.network)
        detail: dict[str, Any] = {
            "network": post.network,
            SOURCE_KEY: creative.id,
            IDEMPOTENCY_KEY: post.key,
            "scheduled_by": user_id,
            "post_now": post.when is None,
        }
        if post.subreddit:
            detail["subreddit"] = post.subreddit.lower()
        asset = assets.create(
            db,
            tenant_id=org.id,
            cipher=cipher,
            agent_slug=creative.agent_slug or DEFAULT_AGENT_SLUG,
            kind=AssetKind.SOCIAL_POST,
            channel=post.rules.connector,
            title=f"{post.rules.label}: {creative.title or 'Design'}",
            content=post.content,
            scheduled_for=post.when or now,
            image_ids=[image.id],
            detail=detail,
        )
        asset.status = AssetStatus.SCHEDULED
        result.posts.append(asset)
        if post.when is None:
            result.post_now_ids.append(asset.id)
    db.flush()
    log.info(
        "social posts scheduled: design=%s posts=%d now=%d",
        creative.id,
        len(result.posts),
        len(result.post_now_ids),
    )
    return result


# ── Status and cancelling ──────────────────────────────────────────────────
def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def safe_link(url: str) -> str:
    """The platform's link to the post, only when it is a plain https address; otherwise "".

    Every place that hands a stored ``remote_url`` to the browser uses this.
    """
    value = (url or "").strip()
    try:
        parts = urlsplit(value)
    except ValueError:
        return ""
    return value if parts.scheme == "https" and parts.hostname and not parts.username else ""


def post_view(asset: MarketingAsset, zone: ZoneInfo | timezone, zone_name: str) -> dict[str, Any]:
    """What the status list shows for one post: no caption, no internal ids beyond its own."""
    detail = asset.detail or {}
    network = str(detail.get("network") or "")
    rules = platforms.rules_for(network)
    when = _aware(asset.scheduled_for)
    status = asset.status
    return {
        "id": asset.id,
        "creative_asset_id": str(detail.get(SOURCE_KEY) or ""),
        "network": network,
        "network_label": rules.label if rules is not None else network.title(),
        "subreddit": str(detail.get("subreddit") or ""),
        "status": status,
        "status_label": STATUS_LABELS.get(status, status.title()),
        "scheduled_for": when,
        "scheduled_local": when.astimezone(zone).strftime("%Y-%m-%dT%H:%M") if when is not None else "",
        "timezone": zone_name,
        "published_at": _aware(asset.published_at),
        "link": safe_link(asset.remote_url) if status == AssetStatus.PUBLISHED else "",
        "reason": asset.error if status == AssetStatus.FAILED else "",
        "note": str(detail.get("publish_note") or "")[:500],
        "can_cancel": status == AssetStatus.SCHEDULED,
    }


def list_posts(db: Session, org: Organization, *, creative_id: str | None, limit: int = MAX_LIST) -> list[dict[str, Any]]:
    """The workspace's shared-design posts, newest first; one design's when ``creative_id`` is given."""
    stmt = select(MarketingAsset).where(
        MarketingAsset.tenant_id == org.id,
        MarketingAsset.kind == AssetKind.SOCIAL_POST,
    )
    if creative_id:
        stmt = stmt.where(MarketingAsset.detail[SOURCE_KEY].astext == creative_id.lower())
    else:
        stmt = stmt.where(MarketingAsset.detail.has_key(SOURCE_KEY))
    rows = db.execute(stmt.order_by(MarketingAsset.created_at.desc()).limit(min(max(limit, 1), MAX_LIST))).scalars()
    zone, zone_name = platforms.zone_for(getattr(org, "settings", None))
    return [post_view(asset, zone, zone_name) for asset in rows]


def cancel(db: Session, org: Organization, *, post_id: str, user_id: str) -> MarketingAsset:
    """Cancel a post that has not gone out yet. Locked, so the publisher can't claim it meanwhile."""
    asset = db.execute(
        select(MarketingAsset)
        .where(
            MarketingAsset.id == post_id.lower(),
            MarketingAsset.tenant_id == org.id,
            MarketingAsset.kind == AssetKind.SOCIAL_POST,
        )
        .with_for_update()
    ).scalars().first()
    if asset is None:
        raise NotFoundError("That post is no longer in the library.")
    if asset.status != AssetStatus.SCHEDULED:
        label = STATUS_LABELS.get(asset.status, asset.status).lower()
        raise ConflictError(f"Only scheduled posts can be cancelled. This one is {label}.")
    now = utcnow()
    asset.status = AssetStatus.REJECTED
    asset.updated_at = now
    detail = dict(asset.detail or {})
    detail.update(cancelled=True, cancelled_by=user_id, cancelled_at=now.isoformat())
    asset.detail = detail
    db.flush()
    return asset


def publish_now(tenant_id: str, post_ids: list[str]) -> None:
    """Publish "post now" posts straight away, on a session of its own (a background task).

    If this does not run (the process stops first), the scheduled publisher
    sends them on its next pass: they are already due.
    """
    from app.core.logging import log_failure
    from app.db.session import session_scope

    try:
        with session_scope(tenant_id) as db:
            org = db.get(Organization, tenant_id)
            if org is None:
                return
            sent = assets.publish_due(db, org, only_ids=post_ids)
            log.info("post now: workspace=%s requested=%d sent=%d", tenant_id, len(post_ids), sent)
    except Exception as exc:  # noqa: BLE001 - nothing is left to report it to; the scheduler retries
        log_failure(log, "post now failed outside the request", exc, tenant_id=tenant_id)


__all__ = [
    "MAX_ITEMS",
    "STATUS_LABELS",
    "ScheduleItem",
    "ScheduleRequest",
    "ScheduleResult",
    "cancel",
    "idempotency_key",
    "list_posts",
    "load_creative",
    "local_to_utc",
    "post_view",
    "publish_now",
    "safe_link",
    "schedule",
]
