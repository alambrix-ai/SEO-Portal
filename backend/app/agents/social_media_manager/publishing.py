"""Publishing and downloading social posts.

One publisher serves every network: it reads the network from the post,
finds the connector named by the asset's ``channel``, checks the post still
obeys the network's rules (it may have been edited), and calls the
connector's ``publish_post``. Downloads are a CSV row and a readable text
file.

How the picture travels, per network:

* Facebook, LinkedIn, X: the bytes (X uploads them in chunks first).
* Pinterest: the bytes inline, except WebP, which Pinterest does not take
  inline; then a long-lived link (:mod:`app.services.media_links`).
* Instagram: a 24-hour public link; Instagram fetches it once.
* Reddit: a long-lived link, because the post itself is a link to the
  picture. Without a public address the bytes are passed and Reddit's
  connector posts without the picture (it says so in the post's note).

Reddit posts to one subreddit are kept ``reddit_min_minutes_between_posts``
apart for the whole workspace, checked against the database (the last
published post to that subreddit), not only within one process. Too soon
raises :class:`~app.connectors.reddit.connector.RedditRateLimited` with
``retry_after_seconds``: the caller reschedules rather than failing the post.
:class:`~app.connectors.reddit.connector.RedditPostRefused` (the subreddit's
rules) is a plain failure with Reddit's reason.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import and_, or_, select, text
from sqlalchemy.orm import Session

from app.agents.social_media_manager import platforms
from app.agents.social_media_manager.platforms import PUBLISH_NETWORKS
from app.core.config import settings
from app.core.exceptions import ConflictError, InvalidInputError
from app.core.logging import get_logger
from app.db.base import utcnow
from app.models.content import AssetKind, AssetStatus, GeneratedImage, MarketingAsset
from app.models.workspace import Organization
from app.services import assets, images, media_links

log = get_logger(__name__)

#: Exported image links stay valid this long, so a download can be scheduled
#: in another tool a few days ahead.
EXPORT_LINK_SECONDS = 7 * 24 * 3600
#: Recent Reddit posts read when checking the spacing between posts.
_SPACING_ROWS = 200

CSV_HEADER = [
    "Network",
    "Scheduled date",
    "Scheduled time",
    "Time zone",
    "Text",
    "Hashtags",
    "Link",
    "Pin title",
    "Image",
    "Post as written",
    "Note",
]


def network_of(asset: MarketingAsset, content: dict) -> str:
    return str(content.get("network") or (asset.detail or {}).get("network") or "")


@assets.register_publisher(AssetKind.SOCIAL_POST)
def publish_social_post(db: Session, org: Organization, asset: MarketingAsset, content: dict) -> None:
    from app.services.connectors import build_connector_bundle

    network = network_of(asset, content)
    rules = PUBLISH_NETWORKS.get(network)
    if rules is None:
        raise ConflictError("This post is not for a network that can be published to.")
    if (asset.detail or {}).get("export_only") or asset.channel != rules.connector:
        raise ConflictError(f"This {rules.label} post is for download only; download it and post it yourself.")
    issue = platforms.problem(network, content)
    if issue:
        raise InvalidInputError(issue)

    bundle = build_connector_bundle(db, org)
    try:
        connector = bundle.instances.get(rules.connector)
        if connector is None or not hasattr(connector, "publish_post"):
            raise ConflictError(
                f"{rules.label} is not connected, so this post cannot be published. Connect it under "
                "Integrations and publish it again, or download it."
            )

        image = _first_image(db, org, asset)
        if rules.needs_image and image is None:
            raise ConflictError(f"{rules.label} needs an image and this post has none. Download it instead.")
        text = platforms.compose(
            network, content.get("text") or "", list(content.get("hashtags") or []), content.get("link") or ""
        )
        kwargs: dict = {"text": text, "link": str(content.get("link") or ""), "title": str(content.get("title") or "")}
        subreddit = ""
        if network == "reddit":
            subreddit = _subreddit_for(content, connector)
            _check_reddit_spacing(db, org, asset, subreddit)
            kwargs["subreddit"] = subreddit
        if image is not None:
            kwargs.update(_image_arguments(network, org, image))
        remote_id, remote_url = connector.publish_post(**kwargs)
        # Reddit (no image or a text post instead of a link) and X (text
        # only until it is allowed images) say when a post went out
        # differently from what was asked.
        note = str(getattr(connector, "last_post_note", "") or "")
    finally:
        # Every connector the bundle opened, not only the one used.
        bundle.close()
    asset.remote_id = str(remote_id or "")[:255]
    asset.remote_url = str(remote_url or "")[:1000]
    if network == "reddit" or note:
        # Reassigned, not changed in place, so the JSON column is saved.
        detail = dict(asset.detail or {})
        if network == "reddit":
            detail["subreddit"] = subreddit.lower()
        if note:
            detail["publish_note"] = note[:500]
        asset.detail = detail


def _image_arguments(network: str, org: Organization, image: GeneratedImage) -> dict:
    """How ``image`` is handed to the network's connector (see the module docstring)."""
    if network == "instagram":
        return {"image_url": images.public_url(org.id, image.id)}
    if network == "reddit":
        if media_links.available():
            return {"image_url": media_links.published_image_url(image)}
        return {"image_bytes": image.data, "image_mime": image.mime}
    if network == "pinterest" and image.mime not in ("image/png", "image/jpeg"):
        if not media_links.available():
            raise ConflictError(
                "Pinterest can't take this picture's file type directly, and this platform isn't "
                "reachable from the internet to share it by link. Download the post instead."
            )
        return {"image_url": media_links.published_image_url(image)}
    return {"image_bytes": image.data, "image_mime": image.mime}


# ── Reddit: one subreddit, spaced posts ────────────────────────────────────
def _subreddit_for(content: dict, connector: object) -> str:
    """The subreddit this post goes to: its own, else the connection's. Validated."""
    from app.connectors.reddit.connector import RedditPostRefused, normalise_subreddit

    raw = str(content.get("subreddit") or "").strip()
    if not raw:
        credentials = getattr(connector, "credentials", None)
        getter = getattr(credentials, "get", None)
        raw = str(getter("subreddit") or "") if callable(getter) else ""
    if not raw:
        raise RedditPostRefused("Choose the subreddit this post goes to, then publish it again.")
    return normalise_subreddit(raw)


def _spacing_lock_key(tenant_id: str, subreddit: str) -> int:
    """A signed 64-bit advisory-lock key for one workspace's subreddit."""
    digest = hashlib.sha256(f"reddit-spacing:{tenant_id}:{subreddit.lower()}".encode()).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _check_reddit_spacing(db: Session, org: Organization, asset: MarketingAsset, subreddit: str) -> None:
    """Refuse (to be rescheduled) a post too soon after the workspace's last one there.

    The last post is read from the database, so every worker and process
    sees the same history. On PostgreSQL a transaction-scoped advisory lock
    per (workspace, subreddit) makes concurrent publishers queue here rather
    than both passing the check. A post still being published counts as the
    latest for as long as the gap lasts.
    """
    from app.connectors.reddit.connector import RedditRateLimited

    minutes = settings.reddit_min_minutes_between_posts
    gap = timedelta(minutes=minutes)
    now = utcnow()
    since = now - gap
    if db.get_bind().dialect.name == "postgresql":
        db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _spacing_lock_key(org.id, subreddit)})
    rows = db.execute(
        select(MarketingAsset.status, MarketingAsset.published_at, MarketingAsset.updated_at, MarketingAsset.detail)
        .where(
            MarketingAsset.tenant_id == org.id,
            MarketingAsset.kind == AssetKind.SOCIAL_POST,
            MarketingAsset.channel == platforms.REDDIT.connector,
            MarketingAsset.id != asset.id,
            or_(
                and_(MarketingAsset.status == AssetStatus.PUBLISHED, MarketingAsset.published_at >= since),
                and_(MarketingAsset.status == AssetStatus.PUBLISHING, MarketingAsset.updated_at >= since),
            ),
        )
        .order_by(MarketingAsset.updated_at.desc())
        .limit(_SPACING_ROWS)
    ).all()
    wanted = subreddit.lower()
    latest: datetime | None = None
    for status, published_at, updated_at, detail in rows:
        if str((detail or {}).get("subreddit") or "").lower() != wanted:
            continue
        when = _aware(published_at if status == AssetStatus.PUBLISHED else updated_at)
        if when is not None and (latest is None or when > latest):
            latest = when
    if latest is None:
        return
    wait = (latest + gap - now).total_seconds()
    if wait <= 0:
        return
    log.info("reddit spacing: post %s deferred %ds", asset.id, int(wait))
    left = max(1, int((wait + 59) // 60))
    plural = "s" if left != 1 else ""
    error = RedditRateLimited(
        f"Posts to r/{subreddit} are kept at least {minutes} minutes apart. This one can go out "
        f"in about {left} minute{plural}.",
        retry_after=wait,
    )
    # Our own spacing, not Reddit's answer: not counted as a deferral.
    error.details[assets.SPACING_DETAIL] = True
    raise error


def _first_image(db: Session, org: Organization, asset: MarketingAsset) -> GeneratedImage | None:
    for image_id in asset.image_ids or []:
        image = db.get(GeneratedImage, image_id)
        if image is not None and image.tenant_id == org.id:
            return image
    return None


# ── Downloads ──────────────────────────────────────────────────────────────
def _local(asset: MarketingAsset, content: dict) -> tuple[str, str, str]:
    """(date, time, zone name) of the scheduled time, in the zone it was planned in."""
    when = asset.scheduled_for
    name = str(content.get("timezone") or "UTC")
    if when is None:
        return "", "", name
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    try:
        zone = ZoneInfo(name) if name != "UTC" else timezone.utc
    except (ZoneInfoNotFoundError, ValueError):
        zone, name = timezone.utc, "UTC"
    local = when.astimezone(zone)
    return local.strftime("%Y-%m-%d"), local.strftime("%H:%M"), name


def _image_ref(asset: MarketingAsset) -> str:
    if not asset.image_ids:
        return ""
    image_id = asset.image_ids[0]
    if images.is_publicly_reachable():
        return images.public_url(asset.tenant_id, image_id, seconds=EXPORT_LINK_SECONDS)
    return f"{assets.slug_for(asset.title)}.png (download it from this post in the library)"


def _row(asset: MarketingAsset, content: dict) -> list[str]:
    network = network_of(asset, content)
    label = PUBLISH_NETWORKS[network].label if network in PUBLISH_NETWORKS else network
    day, at, zone = _local(asset, content)
    hashtags = list(content.get("hashtags") or [])
    return [
        label,
        day,
        at,
        zone,
        str(content.get("text") or ""),
        " ".join(hashtags),
        str(content.get("link") or ""),
        str(content.get("title") or ""),
        _image_ref(asset),
        platforms.compose(network, str(content.get("text") or ""), hashtags, str(content.get("link") or ""))
        if network in PUBLISH_NETWORKS
        else "",
        str(content.get("note") or ""),
    ]


@assets.register_exporter(AssetKind.SOCIAL_POST, "csv", "Spreadsheet (CSV)")
def export_csv(asset: MarketingAsset, content: dict) -> assets.ExportFile:
    network = network_of(asset, content) or "social"
    return assets.ExportFile(
        f"{network}-{assets.slug_for(asset.title)}.csv",
        "text/csv; charset=utf-8",
        assets.csv_bytes(CSV_HEADER, [_row(asset, content)]),
    )


@assets.register_exporter(AssetKind.SOCIAL_POST, "txt", "Plain text")
def export_txt(asset: MarketingAsset, content: dict) -> assets.ExportFile:
    row = dict(zip(CSV_HEADER, _row(asset, content), strict=True))
    lines = [f"{row['Network']} post"]
    if row["Scheduled date"]:
        lines.append(f"Scheduled: {row['Scheduled date']} {row['Scheduled time']} ({row['Time zone']})")
    if row["Pin title"]:
        lines += ["", f"Title: {row['Pin title']}"]
    lines += ["", row["Post as written"] or row["Text"]]
    if row["Link"] and row["Link"] not in (row["Post as written"] or ""):
        lines += ["", f"Link: {row['Link']}"]
    if row["Image"]:
        lines += ["", f"Image: {row['Image']}"]
    if row["Note"]:
        lines += ["", f"Note: {row['Note']}"]
    return assets.ExportFile(
        f"{assets.slug_for(asset.title)}.txt", "text/plain; charset=utf-8", ("\n".join(lines) + "\n").encode()
    )
