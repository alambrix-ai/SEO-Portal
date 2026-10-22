"""Dynamic Creative Optimizer — builds cross-channel variants from product data.

Creative is generated from what the site actually says, not from a brief: the
product metadata already synced by the SEO pipeline is the source, so a price
or trim change on the page propagates into the ads instead of quietly going
stale.

Variants are tailored per platform — a LinkedIn feed unit and a TikTok
full-screen do not share copy length, tone, or aspect ratio — and every variant
is tied to the audience segment it was written for. Publishing creative under
the customer's brand is high impact, so it queues under the hybrid guardrail.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.agents.base.context import AgentContext
from app.agents.base import scope as scope_rules
from app.agents.base.contracts import (
    AgentAction,
    AgentCategory,
    AgentResult,
    AgentSpec,
    BaseAgent,
    Impact,
)
from app.agents.base.contracts import ConnectorUse, UseMode
from app.agents.dynamic_creative_optimizer import prompts, specs
from app.connectors.base.connector import Capability
from app.connectors.base.interfaces import AdsConnector
from app.core.logging import get_logger
from app.db.base import utcnow
from app.models.ads import AdCreative, AudienceCluster
from app.models.approval import ApprovalItem, ApprovalType
from app.models.seo import SeoPage
from app.models.workspace import Organization
from app.services.approvals import register_applier, register_rejecter
from app.services.encryption import Ctx

log = get_logger(__name__)

# Source pages per run: at least this many (the old fixed number), more when
# the daily allowance has room — see pages_per_run(). Each is one model call.
PAGES_PER_RUN = 2
MAX_PAGES_PER_RUN = 5
# Pages considered per run, most recently updated first.
CANDIDATE_PAGES = 200
# Variants kept per page, to stop one product flooding the account. Room for
# a Google responsive search ad's three headlines alongside other platforms.
MAX_VARIANTS_PER_PAGE = 8


class DynamicCreativeOptimizerAgent(BaseAgent):
    spec = AgentSpec(
        slug="dynamic_creative_optimizer",
        name="Dynamic Creative Optimizer",
        category=AgentCategory.ADS,
        description="Builds cross-channel banner and copy variants from product data.",
        default_interval=timedelta(hours=6),
        default_schedule="Every 6 hours",
        required_capabilities=(Capability.UPLOAD_CREATIVE,),
        max_impact=Impact.HIGH,
        default_max_actions_per_day=25,
        scope_placeholder="/products/* — pages to build creative from",
        requires_llm=True,
        connector_use=(
            ConnectorUse(
                Capability.UPLOAD_CREATIVE,
                UseMode.ALL,
                question="Which ad platforms should this agent write ads for?",
                group="ad platforms",
            ),
        ),
    )

    def validate_scope(self, scope: str) -> str | None:
        """Page paths to build creative from, e.g. ``/models/*``."""
        return scope_rules.paths(scope)

    def run(self, ctx: AgentContext) -> AgentResult:
        platforms = self._live_platforms(ctx)
        if not platforms:
            return AgentResult.skip("Waiting on an ad platform connector")

        limit = pages_per_run(ctx.remaining_actions, len(platforms))
        candidates = self._candidate_pages(ctx)
        if not candidates:
            return AgentResult.skip("No product pages synced yet")
        pages = self._pick_pages(ctx, candidates, limit=limit)
        if not pages:
            return AgentResult.skip(
                "Every product page already has creative written since it last changed"
            )

        segments = self._segments(ctx)
        known_segments = {s.lower(): s for s in segments}
        allowed = {p.name for p in platforms}
        actions: list[AgentAction] = []
        generated = 0

        for page in pages:
            body = ctx.cipher.decrypt(page.body_encrypted, context=Ctx.PAGE_BODY)
            product = specs.extract_product(title=page.title, url=page.url, body=body)

            try:
                data = ctx.ask_json(
                    prompts.creative_variants(
                        product=product,
                        platforms=[p.name for p in platforms],
                        brand=ctx.org.name,
                        segments=segments,
                        page_url=page.url,
                    ),
                    system=prompts.SYSTEM,
                    schema_hint=prompts.SCHEMA_HINT,
                )
            except Exception as exc:  # noqa: BLE001
                ctx.log_warning("Creative generation failed for %s: %s", page.url, exc)
                continue

            # Variants written together for one page share a batch: that is
            # what a combining platform (Google) assembles into one ad, and it
            # keeps copy written before a price change apart from copy
            # written after it.
            batch = str(uuid.uuid4())
            kept = 0
            for entry in data.get("variants") or []:
                if kept >= MAX_VARIANTS_PER_PAGE:
                    break
                variant = specs.normalise_variant(entry, allowed=allowed)
                if variant is None:
                    continue

                problem = specs.check_copy(
                    headline=variant.headline,
                    body_copy=variant.body_copy,
                    platform=variant.platform,
                )
                if problem:
                    # Copy that exceeds a platform's own limits is rejected by
                    # that platform anyway; catching it here saves a round trip
                    # and keeps the queue clean.
                    ctx.log_warning("Rejected %s variant: %s", variant.platform, problem)
                    continue

                # Only a segment that actually exists is recorded; anything
                # else the model wrote is a label nobody can target.
                segment = known_segments.get(variant.audience_segment.lower(), "")

                creative = AdCreative(
                    tenant_id=ctx.tenant_id,
                    platform=variant.platform,
                    dimensions=variant.dimensions,
                    headline_encrypted=ctx.cipher.encrypt(
                        variant.headline, context=Ctx.CREATIVE_HEADLINE
                    ),
                    body_copy_encrypted=ctx.cipher.encrypt(
                        variant.body_copy, context=Ctx.CREATIVE_BODY
                    ),
                    call_to_action=variant.call_to_action,
                    source_page_url=page.url,
                    source_product=product.name,
                    audience_segment=segment,
                    status="generated",
                    detail={"aspect": specs.aspect_of(variant.dimensions), "batch": batch},
                )
                ctx.db.add(creative)
                ctx.db.flush()
                generated += 1
                kept += 1

                actions.append(
                    AgentAction(
                        kind="publish_creative",
                        title=(
                            f"DCO variant — {product.name}, {variant.platform} "
                            f"{variant.dimensions}"
                        ),
                        impact=Impact.HIGH,
                        approval_type=ApprovalType.AD_CREATIVE.value,
                        target_kind="ad_creative",
                        target_id=creative.id,
                        payload={
                            "creative_id": creative.id,
                            "platform": variant.platform,
                            "dimensions": variant.dimensions,
                            "headline": variant.headline,
                            "body_copy": variant.body_copy,
                            "call_to_action": variant.call_to_action,
                            "audience_segment": segment,
                            "source_page_url": page.url,
                        },
                        apply=_make_publisher(creative.id),
                        audit=(
                            f"published a {variant.platform} {variant.dimensions} "
                            f"variant for {product.name}"
                        ),
                        writes_via=_writer_for(ctx, variant.platform),
                    )
                )

        total = ctx.db.execute(
            select(func.count())
            .select_from(AdCreative)
            .where(AdCreative.tenant_id == ctx.tenant_id)
        ).scalar_one()

        return AgentResult(
            summary=(
                f"generated {generated} variants for {len(pages)} pages "
                f"across {len(platforms)} platforms"
            ),
            actions=actions,
            metric_label=f"{total} variants generated",
            detail={
                "generated": generated,
                "pages": [p.url for p in pages],
                "page_limit": limit,
                "platforms": [p.name for p in platforms],
                "segments": segments,
            },
        )

    # ── Inputs ─────────────────────────────────────────────────────────────
    def _live_platforms(self, ctx: AgentContext) -> list[AdsConnector]:
        return ctx.all_with_capability(  # type: ignore[return-value]
            Capability.UPLOAD_CREATIVE
        )

    def _candidate_pages(self, ctx: AgentContext) -> list[SeoPage]:
        # Same reason as the AEO injector: SeoPage.status is another
        # agent's rewrite lifecycle, not a statement about whether the page
        # can be advertised. Accepting only live and rewritten meant a page
        # with a pending copy rewrite could not be used to build creative,
        # which has nothing to do with it.
        stmt = select(SeoPage).where(SeoPage.tenant_id == ctx.tenant_id)
        in_scope = scope_rules.sql_filter(ctx.scope or "", SeoPage.url)
        if in_scope is not None:
            stmt = stmt.where(in_scope)
        return list(
            ctx.db.execute(
                stmt.order_by(SeoPage.updated_at.desc()).limit(CANDIDATE_PAGES)
            ).scalars()
        )

    def _last_generated(self, ctx: AgentContext) -> dict[str, datetime]:
        """When creative was last written for each page, whatever became of it.

        Rejected variants count too: a person turned that copy down, and
        writing for the same page again before it changes would only offer
        them the same thing.
        """
        rows = ctx.db.execute(
            select(AdCreative.source_page_url, func.max(AdCreative.created_at))
            .where(AdCreative.tenant_id == ctx.tenant_id)
            .group_by(AdCreative.source_page_url)
        )
        return {url: at for url, at in rows if url and at is not None}

    def _pick_pages(
        self, ctx: AgentContext, pages: list[SeoPage], *, limit: int
    ) -> list[SeoPage]:
        """Pages whose creative is missing or older than the page itself.

        Every run used to take the same two most recently updated pages and
        write a fresh set of variants for them, over and over, while other
        pages never got any. Now a page is written for once, and again only
        when it changes (its updated_at moves past its newest creative — a
        price change, say, so the ads stop quoting the old price). Pages
        already covered drop out, so successive runs rotate through the rest.
        """
        last = self._last_generated(ctx)
        due = [
            page
            for page in pages
            if (written := last.get(page.url)) is None
            or (page.updated_at is not None and _aware(page.updated_at) > _aware(written))
        ]
        # Pages that describe something purchasable first, then pages never
        # covered, then the most recently changed.
        due.sort(
            key=lambda p: (
                -specs.commercial_score(p.title, p.url),
                p.url in last,
                -(_aware(p.updated_at).timestamp() if p.updated_at else 0.0),
            )
        )
        return due[:limit]

    def _source_pages(self, ctx: AgentContext) -> list[SeoPage]:
        """The pages this run would write creative for."""
        platforms = self._live_platforms(ctx)
        limit = pages_per_run(ctx.remaining_actions, len(platforms))
        return self._pick_pages(ctx, self._candidate_pages(ctx), limit=limit)

    def _segments(self, ctx: AgentContext) -> list[str]:
        """Live audience segments, so copy is written for a real reader.

        Empty when there are none. It used to fall back to an invented
        "in-market shoppers" segment, which then appeared on creatives as if
        it were an audience someone could target.
        """
        clusters = ctx.db.execute(
            select(AudienceCluster)
            .where(
                AudienceCluster.tenant_id == ctx.tenant_id,
                AudienceCluster.is_live.is_(True),
            )
            .order_by(AudienceCluster.size.desc())
            .limit(4)
        ).scalars()
        return [c.label for c in clusters if c.label]


def pages_per_run(remaining_actions: int, platform_count: int) -> int:
    """Source pages for one run. Each is one LLM call.

    Follows the daily actions still available: a page yields roughly two
    variants per platform (capped at MAX_VARIANTS_PER_PAGE), one action each.
    Never fewer than PAGES_PER_RUN (the old fixed number; proposals queued
    for review do not use up the allowance) and never more than
    MAX_PAGES_PER_RUN, which bounds a run's model cost.
    """
    per_page = max(1, min(MAX_VARIANTS_PER_PAGE, 2 * max(1, platform_count)))
    try:
        affordable = int(remaining_actions) // per_page
    except (TypeError, ValueError):
        affordable = 0
    return max(PAGES_PER_RUN, min(MAX_PAGES_PER_RUN, affordable))


def _writer_for(ctx: AgentContext, platform: str) -> tuple[str, ...]:
    """The connector a creative for ``platform`` is uploaded through."""
    connector = specs.connector_for_platform(ctx, platform)
    slug = getattr(connector, "slug", "") if connector is not None else ""
    if not slug:
        spec = specs.PLATFORMS.get(platform)
        slug = spec.connector_slug if spec is not None else ""
    return (slug,) if slug else ()


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def landing_url(ctx: AgentContext, source_page_url: str) -> str:
    """The absolute URL an ad should send people to.

    Synced pages are often stored as paths ("/models/x"); ad platforms need a
    full URL, built here from the workspace's primary domain. Empty when that
    is not possible, so the connector can fall back to its own default.
    """
    url = (source_page_url or "").strip()
    if url.startswith(("http://", "https://")):
        return url
    domain = (getattr(ctx.org, "primary_domain", "") or "").strip().rstrip("/")
    if not url or not domain:
        return ""
    if not domain.startswith(("http://", "https://")):
        domain = f"https://{domain}"
    return domain + (url if url.startswith("/") else "/" + url)


# ── Publishing ─────────────────────────────────────────────────────────────
def _siblings(ctx: AgentContext, creative: AdCreative) -> list[AdCreative]:
    """Unpublished variants from the same batch, page and platform."""
    batch = (creative.detail or {}).get("batch")
    rows = ctx.db.execute(
        select(AdCreative).where(
            AdCreative.tenant_id == creative.tenant_id,
            AdCreative.platform == creative.platform,
            AdCreative.source_page_url == creative.source_page_url,
            AdCreative.id != creative.id,
        )
    ).scalars()
    return [
        row
        for row in rows
        if row.id != creative.id
        and row.platform == creative.platform
        and row.source_page_url == creative.source_page_url
        and row.status in ("generated", "queued")
        and (row.detail or {}).get("batch") == batch
    ]


def _make_publisher(creative_id: str):  # noqa: ANN202
    def publish(ctx: AgentContext) -> None:
        creative = ctx.db.get(AdCreative, creative_id)
        if creative is None or creative.status == "live":
            # Already live, including as part of a sibling's combined ad.
            return

        connector = specs.connector_for_platform(ctx, creative.platform)
        if connector is None:
            from app.core.exceptions import ConnectorError

            raise ConnectorError(
                f"{creative.platform} is not connected — cannot publish this creative"
            )

        headline = ctx.cipher.decrypt(
            creative.headline_encrypted, context=Ctx.CREATIVE_HEADLINE
        )
        body_copy = ctx.cipher.decrypt(
            creative.body_copy_encrypted, context=Ctx.CREATIVE_BODY
        )

        spec = specs.PLATFORMS.get(creative.platform)
        siblings = _siblings(ctx, creative) if spec and spec.combines_variants else []
        extra_headlines = tuple(
            ctx.cipher.decrypt(row.headline_encrypted, context=Ctx.CREATIVE_HEADLINE)
            for row in siblings
        )
        extra_descriptions = tuple(
            ctx.cipher.decrypt(row.body_copy_encrypted, context=Ctx.CREATIVE_BODY)
            for row in siblings
        )

        remote_id = connector.upload_creative(
            headline=headline,
            body_copy=body_copy,
            call_to_action=creative.call_to_action,
            dimensions=creative.dimensions,
            audience_segment=creative.audience_segment,
            final_url=landing_url(ctx, creative.source_page_url),
            extra_headlines=extra_headlines,
            extra_descriptions=extra_descriptions,
        )
        now = utcnow()
        creative.status = "live"
        creative.detail = {
            **(creative.detail or {}),
            "remote_id": remote_id,
            **({"combined_with": [row.id for row in siblings]} if siblings else {}),
        }
        creative.updated_at = now
        # The siblings' copy is now part of that one ad. Marking them live
        # with the same remote id stops each being uploaded again as an ad
        # of its own when its approval (or the next autonomous apply) runs.
        for row in siblings:
            row.status = "live"
            row.detail = {
                **(row.detail or {}),
                "remote_id": remote_id,
                "combined_into": creative.id,
            }
            row.updated_at = now
        if siblings:
            _close_sibling_approvals(ctx, [row.id for row in siblings], creative.id)
        ctx.db.flush()

    return publish


def _close_sibling_approvals(ctx: AgentContext, sibling_ids: list[str], combined_id: str) -> None:
    """Close the siblings' own approval cards: their copy is already live.

    Left open, approving one would do nothing and rejecting one would mark
    copy rejected that is running in the combined ad.
    """
    from app.models.approval import ApprovalStatus

    pending = ctx.db.execute(
        select(ApprovalItem).where(
            ApprovalItem.tenant_id == ctx.tenant_id,
            ApprovalItem.target_kind == "ad_creative",
            ApprovalItem.target_id.in_(sibling_ids),
            ApprovalItem.status == ApprovalStatus.PENDING.value,
        )
    ).scalars()
    for item in pending:
        item.status = ApprovalStatus.APPROVED.value
        item.decided_at = utcnow()
        item.decided_by = ctx.record.name
        item.decision_note = (
            "Published as part of one combined Google search ad, together with the "
            "variant that was approved."
        )


@register_applier("ad_creative")
def apply_creative(db: Session, org: Organization, item: ApprovalItem, payload: dict) -> None:
    from app.models.agent import AgentRecord
    from app.orchestration.runner import build_context

    record = db.execute(
        select(AgentRecord).where(
            AgentRecord.tenant_id == org.id,
            AgentRecord.slug == "dynamic_creative_optimizer",
        )
    ).scalar_one_or_none()
    if record is None:
        raise LookupError("Dynamic Creative Optimizer is not installed in this workspace")

    ctx = build_context(db, record, org, trigger="approval")
    _make_publisher(payload.get("creative_id") or item.target_id or "")(ctx)


@register_rejecter("ad_creative")
def reject_creative(db: Session, org: Organization, item: ApprovalItem, payload: dict) -> None:
    creative = db.get(AdCreative, payload.get("creative_id") or item.target_id or "")
    if creative is None:
        return
    creative.status = "rejected"
    db.flush()


AGENT = DynamicCreativeOptimizerAgent()
