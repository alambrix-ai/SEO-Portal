"""Social Media Manager — plans and writes a week of posts per network.

Each run looks at the seven days starting tomorrow (in the workspace's time
zone, or UTC) and, for every chosen network, fills the days that do not have
a post yet:

1. **Plan.** Post types are assigned in code so the week is mixed — tips,
   product or service highlights, questions, links to recently published
   articles, and social proof only when the brand profile states something
   that supports it. Topics used by recent posts are given to the model to
   avoid.
2. **Write.** One model call per network writes that network's posts from the
   brand profile. Each network's rules (length, hashtags, links) are then
   enforced in code by :mod:`platforms` — the model is not trusted with them.
3. **Illustrate.** With an image integration connected, one image per post.
   A failed image means a post without one; Instagram and Pinterest cannot
   publish without an image, so those become downloads.
4. **Propose.** Each post is a library asset with a posting time. Approving
   it schedules it; the platform's scheduler publishes due posts every few
   minutes. A network that is not connected gets a download instead.
"""
from __future__ import annotations

import io
from datetime import datetime, time, timedelta, timezone

from sqlalchemy import select

from app.agents.base.context import AgentContext
from app.agents.base.contracts import (
    AgentAction,
    AgentCategory,
    AgentResult,
    AgentSpec,
    BaseAgent,
    Impact,
)
from app.agents.base.contracts import ConnectorUse, UseMode
from app.agents.social_media_manager import platforms, prompts
from app.agents.social_media_manager.platforms import NETWORKS
from app.connectors.base.connector import Capability
from app.db.base import utcnow
from app.models.content import AssetKind, AssetStatus, MarketingAsset
from app.services import assets, images

SLUG = "social_media_manager"
#: Posts written per run at most (each may also be an image), however high
#: the daily limit is set.
MAX_POSTS_PER_RUN = 40
#: When the daily counter reads 0 (a fresh day), the batch the run writes.
DEFAULT_BATCH = 15
#: After this many image failures in a row, the rest of the run goes without.
MAX_IMAGE_FAILURES = 2
#: Previous posts read to avoid repeating topics and re-sharing articles.
HISTORY = 60
#: Published articles offered for sharing.
ARTICLES = 5

#: Why a post is a download rather than published directly (structural codes).
NOTES = {
    "not_connected": "{label} is not connected, so this post is ready to download and post yourself.",
    "no_image": "{label} needs an image and none could be made, so this post is ready to download. "
    "Add an image when you post it.",
    "no_link": "A Pin needs a link to your website and none is known (set your website on the "
    "workspace), so this Pin is ready to download.",
    "no_public_address": "Instagram downloads images from a public web address and this platform is "
    "not reachable from the internet, so this post is ready to download.",
    "image_format": "Instagram accepts JPEG images only and this image could not be converted, so "
    "this post is ready to download.",
}


class SocialMediaManagerAgent(BaseAgent):
    spec = AgentSpec(
        slug=SLUG,
        name="Social Media Manager",
        category=AgentCategory.CONTENT,
        description=(
            "Plans a week of posts for your social networks, with an image for each, and "
            "publishes them at their scheduled times."
        ),
        default_interval=timedelta(days=7),
        default_schedule="Weekly",
        max_impact=Impact.HIGH,
        default_max_actions_per_day=25,
        requires_llm=True,
        connector_use=(
            ConnectorUse(
                Capability.PUBLISH_SOCIAL,
                UseMode.ALL,
                question="Which social networks should this agent write posts for?",
                group="social networks",
                note=(
                    "With none connected and none chosen, Facebook, Instagram and "
                    "LinkedIn posts are made ready to download."
                ),
                replaces_setting="networks",
            ),
            ConnectorUse(
                Capability.GENERATE_IMAGE,
                UseMode.ONE,
                question="Which AI service should create post images?",
            ),
        ),
    )

    def run(self, ctx: AgentContext) -> AgentResult:
        networks, unknown = chosen_networks(ctx)
        if not networks:
            return AgentResult.skip(
                "None of the social networks chosen for this agent is connected. Connect one, "
                "or choose others in this agent's settings."
            )
        per_week = int_setting(ctx, "posts_per_week", 3, 1, 14)
        hashtag_limit = int_setting(ctx, "hashtags_per_post", 3, 0, 30)
        tz, tz_name = platforms.zone_for(getattr(ctx.org, "settings", None))
        days = platforms.week_days(utcnow().astimezone(tz).date())
        start = datetime.combine(days[0], time(0), tzinfo=tz).astimezone(timezone.utc)
        end = datetime.combine(days[-1] + timedelta(days=1), time(0), tzinfo=tz).astimezone(timezone.utc)

        ctx.progress("Reading your recent posts")
        history = [(row, assets.content_of(row, ctx.cipher)) for row in recent_posts(ctx)]
        wanted = open_slots(networks, per_week, days, tz, history, start, end)
        total = sum(len(v) for v in wanted.values())
        if total == 0:
            return AgentResult.skip("This week's posts are already planned for every network")

        cap = max(1, min(MAX_POSTS_PER_RUN, ctx.remaining_actions or DEFAULT_BATCH))
        wanted, deferred = share_out(wanted, cap)

        profile = ctx.brand_profile or {}
        articles = recent_articles(ctx)
        proof = prompts.proof_claims(profile)
        domain = (getattr(ctx.org, "primary_domain", "") or "").strip().strip("/")
        homepage = f"https://{domain}/" if domain else ""
        avoid = [str(c.get("topic") or "") for _, c in history if c.get("topic")][:30]
        shared_links = {(c.get("network"), c.get("link")) for _, c in history if c.get("link")}
        # An image connection without an image model chosen: posts are still
        # written, without pictures, and the summary says why.
        image_problem = images.image_setup_problem(ctx)
        image_maker = ctx.with_capability(Capability.GENERATE_IMAGE) is not None and not image_problem
        image_failures = 0
        public = images.is_publicly_reachable()

        actions: list[AgentAction] = []
        counts: dict[str, int] = {}
        downloads: dict[str, int] = {}
        images_failed = 0
        done = 0
        for network_index, network in enumerate(networks):
            times = wanted.get(network) or []
            if not times:
                continue
            rules = NETWORKS[network]
            connected = rules.connector in ctx.connectors
            items = plan_items(
                network,
                len(times),
                offset=network_index,
                profile=profile,
                articles=[a for a in articles if (network, a["url"]) not in shared_links],
                proof=proof,
                homepage=homepage,
            )
            links = sorted({a["url"] for a in articles} | ({homepage} if homepage else set()))
            ctx.progress(f"Writing {rules.label} posts", done=done, total=total)
            try:
                data = ctx.ask_json(
                    prompts.week_plan(
                        brand_brief=ctx.brand_brief,
                        business=ctx.org.name,
                        network=network,
                        items=items,
                        hashtags_per_post=hashtag_limit,
                        allowed_links=links,
                        avoid_topics=avoid,
                    ),
                    system=prompts.SYSTEM,
                    schema_hint=prompts.SCHEMA_HINT,
                )
            except Exception as exc:  # noqa: BLE001 - one network failing must not stop the rest
                ctx.log_warning("Writing %s posts failed: %s", network, exc)
                continue
            drafts = prompts.parse_posts(data)

            for number, (when, item) in enumerate(zip(times, items, strict=False), start=1):
                draft = drafts.get(number)
                if draft is None:
                    continue
                done += 1
                ctx.progress(f"Preparing {rules.label} posts", done=done, total=total)
                link = str(draft.get("link") or "").strip()
                if link not in links:
                    link = item.get("link") or ""
                if network == "pinterest" and not link:
                    link = homepage
                post = platforms.fit(network, {**draft, "link": link}, hashtag_limit=hashtag_limit)

                reason = "" if connected else "not_connected"
                image = None
                # X posts are published as text; an image is only worth making
                # for one that will be downloaded and posted by hand.
                wants_image = image_maker and image_failures < MAX_IMAGE_FAILURES and not (
                    network == "x" and connected
                )
                if wants_image:
                    try:
                        image = images.generate(
                            ctx.db,
                            ctx.org,
                            image_prompt(post, item, rules.label, ctx.org.name),
                            shape=rules.shape,
                            provider=images.provider_for(ctx),
                        )
                        image_failures = 0
                    except Exception as exc:  # noqa: BLE001 - a post without an image is still a post
                        ctx.log_warning("Image for a %s post failed: %s", network, exc)
                        image_failures += 1
                        images_failed += 1
                if image is not None and network == "instagram" and not as_jpeg(image):
                    reason = reason or "image_format"
                if rules.needs_image and image is None:
                    reason = reason or "no_image"
                if rules.needs_link and not post["link"]:
                    reason = reason or "no_link"
                if network == "instagram" and connected and not public:
                    reason = reason or "no_public_address"

                local = when.astimezone(tz)
                content = {
                    "network": network,
                    "post_type": item["type"],
                    "topic": " ".join(str(draft.get("topic") or item.get("subject") or "").split())[:120],
                    "text": post["text"],
                    "hashtags": post["hashtags"],
                    "link": post["link"],
                    "title": post["title"],
                    "image_prompt": str(draft.get("image_prompt") or "")[:1000],
                    "timezone": tz_name,
                    "local_time": local.isoformat(),
                }
                if reason:
                    content["note"] = NOTES[reason].format(label=rules.label)
                detail = {
                    "network": network,
                    "post_type": item["type"],
                    "characters": platforms.length(
                        network, platforms.compose(network, post["text"], post["hashtags"], post["link"])
                    ),
                    "hashtags": len(post["hashtags"]),
                    "has_image": image is not None,
                }
                if reason:
                    detail.update(export_only=True, export_reason=reason)
                asset = assets.create(
                    ctx.db,
                    tenant_id=ctx.tenant_id,
                    cipher=ctx.cipher,
                    agent_slug=SLUG,
                    kind=AssetKind.SOCIAL_POST,
                    channel=f"export:{network}" if reason else rules.connector,
                    title=f"{rules.label} · {local:%a %d %b}: {content['topic'] or item['type']}",
                    content=content,
                    run_id=ctx.run_id or "",
                    scheduled_for=when,
                    image_ids=[image.id] if image is not None else [],
                    detail=detail,
                )
                counts[network] = counts.get(network, 0) + 1
                if reason:
                    downloads[network] = downloads.get(network, 0) + 1
                at = f"{local:%a %d %b, %H:%M} {tz_name}"
                body = platforms.compose(network, post["text"], post["hashtags"], post["link"])
                actions.append(
                    AgentAction(
                        kind="social_post",
                        title=(f"Download {rules.label} post for {at}" if reason else f"Post to {rules.label} on {at}"),
                        impact=Impact.HIGH,
                        approval_type="Social Post",
                        target_kind=assets.TARGET_KIND,
                        target_id=asset.id,
                        payload={
                            "asset_id": asset.id,
                            "network": rules.label,
                            "text": body[:500],
                            "scheduled_for": when.isoformat(),
                            "export_only": bool(reason),
                        },
                        apply=lambda c, a=asset: assets.release(c.db, c.org, a, c.cipher),
                        audit=(
                            f"approved a {rules.label} post to download"
                            if reason
                            else f"scheduled a {rules.label} post for {at}"
                        ),
                    )
                )

        if not actions:
            return AgentResult(summary="No posts could be written this run; it will try again next run.")
        parts = [
            f"{NETWORKS[n].label} {counts[n]}" + (" (to download)" if downloads.get(n) == counts[n] else "")
            for n in networks
            if counts.get(n)
        ]
        summary = f"{len(actions)} posts planned for {days[0]:%d %b}–{days[-1]:%d %b}: " + ", ".join(parts)
        if images_failed:
            summary += f"; {images_failed} images could not be made"
        if image_problem:
            summary += "; posts have no pictures. " + image_problem
        if deferred:
            summary += f"; {deferred} more next run (daily limit)"
        if unknown:
            summary += "; ignored unknown networks: " + ", ".join(unknown)
        return AgentResult(
            summary=summary,
            actions=actions,
            metric_label=f"{len(actions)} posts planned this week",
            detail={"networks": list(counts), "posts": len(actions), "downloads": sum(downloads.values())},
        )


# ── Settings ───────────────────────────────────────────────────────────────
def int_setting(ctx: AgentContext, key: str, default: int, low: int, high: int) -> int:
    try:
        value = int(float(str(ctx.setting(key, "") or default)))
    except ValueError:
        value = default
    return max(low, min(high, value))


#: The setting that chose networks by name before the connector selection
#: existed. Still honoured until this agent's settings are next saved, which
#: replaces it (see ConnectorUse.replaces_setting).
LEGACY_NETWORKS_SETTING = "networks"


def chosen_networks(ctx: AgentContext) -> tuple[list[str], list[str]]:
    """The networks to write for, and any names in the legacy setting that are not networks.

    In order:

    1. The social networks selected in this agent's settings — only those
       that are still connected. One source of truth from here on.
    2. Otherwise, a network list saved in the old ``networks`` setting (which
       may name networks that are not connected; their posts are downloads).
    3. Otherwise, every connected network — or, with none connected,
       :data:`platforms.FALLBACK_NETWORKS` as downloads.
    """
    selected = getattr(ctx, "selected_slugs", None)
    if callable(selected) and selected(Capability.PUBLISH_SOCIAL):
        using = {c.slug for c in ctx.all_with_capability(Capability.PUBLISH_SOCIAL)}
        return [key for key, rules in NETWORKS.items() if rules.connector in using], []

    raw = str(ctx.setting(LEGACY_NETWORKS_SETTING, "") or "")
    names = [part.strip().lower() for part in raw.replace(";", ",").split(",") if part.strip()]
    aliases = {"twitter": "x", "x (twitter)": "x", "fb": "facebook", "ig": "instagram"}
    names = [aliases.get(name, name) for name in names]
    chosen = list(dict.fromkeys(name for name in names if name in NETWORKS))
    unknown = [name for name in names if name not in NETWORKS]
    if not chosen:
        finder = getattr(ctx, "all_with_capability", None)
        connected = (
            {c.slug for c in finder(Capability.PUBLISH_SOCIAL)}
            if callable(finder)
            else set(ctx.connectors)
        )
        chosen = [key for key, rules in NETWORKS.items() if rules.connector in connected]
    return chosen or list(platforms.FALLBACK_NETWORKS), unknown


# ── Planning ───────────────────────────────────────────────────────────────
def recent_posts(ctx: AgentContext) -> list[MarketingAsset]:
    return list(
        ctx.db.execute(
            select(MarketingAsset)
            .where(
                MarketingAsset.tenant_id == ctx.tenant_id,
                MarketingAsset.agent_slug == SLUG,
                MarketingAsset.kind == AssetKind.SOCIAL_POST,
            )
            .order_by(MarketingAsset.created_at.desc())
            .limit(HISTORY)
        ).scalars()
    )


def recent_articles(ctx: AgentContext) -> list[dict]:
    """Published articles with a live address, newest first."""
    rows = ctx.db.execute(
        select(MarketingAsset)
        .where(
            MarketingAsset.tenant_id == ctx.tenant_id,
            MarketingAsset.kind == AssetKind.ARTICLE,
            MarketingAsset.status == AssetStatus.PUBLISHED,
            MarketingAsset.remote_url != "",
        )
        .order_by(MarketingAsset.published_at.desc())
        .limit(ARTICLES)
    ).scalars()
    return [{"title": row.title, "url": row.remote_url} for row in rows if row.remote_url.startswith("http")]


def open_slots(
    networks: list[str],
    per_week: int,
    days: list,
    tz,  # noqa: ANN001
    history: list[tuple[MarketingAsset, dict]],
    start: datetime,
    end: datetime,
) -> dict[str, list[datetime]]:
    """Posting times still to fill per network: the week's plan minus days already covered."""
    covered: dict[str, set] = {}
    for row, content in history:
        when = row.scheduled_for
        if when is None or row.status in (AssetStatus.REJECTED, AssetStatus.FAILED):
            continue
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        if start <= when < end:
            network = content.get("network") or (row.detail or {}).get("network")
            covered.setdefault(network, set()).add(when.astimezone(tz).date())
    out: dict[str, list[datetime]] = {}
    for network in networks:
        have = covered.get(network, set())
        need = max(0, per_week - len(have))
        free = [s for s in platforms.slots(network, per_week, days, tz) if s.astimezone(tz).date() not in have]
        out[network] = free[:need]
    return out


def share_out(wanted: dict[str, list[datetime]], cap: int) -> tuple[dict[str, list[datetime]], int]:
    """At most ``cap`` posts, taken round-robin so every network gets some."""
    total = sum(len(v) for v in wanted.values())
    if total <= cap:
        return wanted, 0
    kept: dict[str, list[datetime]] = {network: [] for network in wanted}
    queues = {network: list(times) for network, times in wanted.items()}
    taken = 0
    while taken < cap:
        for network, queue in queues.items():
            if queue and taken < cap:
                kept[network].append(queue.pop(0))
                taken += 1
    return kept, total - cap


def plan_items(
    network: str,
    count: int,
    *,
    offset: int,
    profile: dict,
    articles: list[dict],
    proof: list[str],
    homepage: str,
) -> list[dict]:
    """The week's post types, mixed, each with what it is about."""
    kinds = ["tip", "highlight", "question"]
    if articles:
        kinds.insert(1, "article")
    if proof:
        kinds.append("social_proof")
    products = [str(p) for p in profile.get("products_services") or [] if str(p).strip()]
    items: list[dict] = []
    article_queue = list(articles)
    highlights = 0
    for index in range(count):
        kind = kinds[(index + offset) % len(kinds)]
        item: dict = {"type": kind}
        if kind == "article":
            if not article_queue:
                kind = item["type"] = "tip"
            else:
                article = article_queue.pop(0)
                item.update(subject=article["title"], link=article["url"])
        if kind == "highlight":
            if products:
                # Each network starts at a different product, and moves on each time.
                item["subject"] = products[(highlights + offset) % len(products)]
                highlights += 1
            if homepage:
                item["link"] = homepage
        if kind == "social_proof":
            item["subject"] = f'"{proof[index % len(proof)]}"'
        items.append(item)
    return items


def image_prompt(post: dict, item: dict, label: str, business: str) -> str:
    idea = str(post.get("image_prompt") or "").strip() or str(item.get("subject") or post.get("topic") or "")
    return (
        f"{idea}\n\nA natural, high-quality photograph-style image for a {label} post by {business}. "
        "Do not include any text, letters, numbers, logos or watermarks."
    )[:3000]


def as_jpeg(image) -> bool:  # noqa: ANN001
    """Store ``image`` as JPEG (Instagram accepts nothing else). False if it cannot be."""
    if image.mime == "image/jpeg":
        return True
    try:
        from PIL import Image

        with Image.open(io.BytesIO(image.data)) as source:
            buffer = io.BytesIO()
            source.convert("RGB").save(buffer, format="JPEG", quality=90)
    except Exception:  # noqa: BLE001 - reported as a download, with the reason
        return False
    image.data = buffer.getvalue()
    image.mime = "image/jpeg"
    return True


AGENT = SocialMediaManagerAgent()
