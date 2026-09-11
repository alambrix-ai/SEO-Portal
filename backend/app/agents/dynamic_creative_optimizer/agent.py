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

from datetime import timedelta

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

# Source pages used per run; each is one model call producing several variants.
PAGES_PER_RUN = 2
# Variants kept per page, to stop one product flooding the account.
MAX_VARIANTS_PER_PAGE = 6


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
    )

    def validate_scope(self, scope: str) -> str | None:
        """Page paths to build creative from, e.g. ``/models/*``."""
        return scope_rules.paths(scope)

    def run(self, ctx: AgentContext) -> AgentResult:
        platforms = self._live_platforms(ctx)
        if not platforms:
            return AgentResult.skip("Waiting on an ad platform connector")

        pages = self._source_pages(ctx)
        if not pages:
            return AgentResult.skip("No product pages synced yet")

        segments = self._segments(ctx)
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

            kept = 0
            for entry in data.get("variants") or []:
                if kept >= MAX_VARIANTS_PER_PAGE:
                    break
                variant = specs.normalise_variant(entry, allowed={p.name for p in platforms})
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
                    audience_segment=variant.audience_segment,
                    status="generated",
                    detail={"aspect": specs.aspect_of(variant.dimensions)},
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
                            "audience_segment": variant.audience_segment,
                            "source_page_url": page.url,
                        },
                        apply=_make_publisher(creative.id),
                        audit=(
                            f"published a {variant.platform} {variant.dimensions} "
                            f"variant for {product.name}"
                        ),
                    )
                )

        total = ctx.db.execute(
            select(func.count())
            .select_from(AdCreative)
            .where(AdCreative.tenant_id == ctx.tenant_id)
        ).scalar_one()

        return AgentResult(
            summary=f"generated {generated} variants across {len(platforms)} platforms",
            actions=actions,
            metric_label=f"{total} variants generated",
            detail={"generated": generated, "platforms": [p.name for p in platforms]},
        )

    # ── Inputs ─────────────────────────────────────────────────────────────
    def _live_platforms(self, ctx: AgentContext) -> list[AdsConnector]:
        return ctx.all_with_capability(  # type: ignore[return-value]
            Capability.UPLOAD_CREATIVE
        )

    def _source_pages(self, ctx: AgentContext) -> list[SeoPage]:
        """Pages worth advertising: live product-shaped content, freshest first."""
        # Same reason as the AEO injector: SeoPage.status is another
        # agent's rewrite lifecycle, not a statement about whether the page
        # can be advertised. Accepting only live and rewritten meant a page
        # with a pending copy rewrite could not be used to build creative,
        # which has nothing to do with it.
        stmt = select(SeoPage).where(SeoPage.tenant_id == ctx.tenant_id)
        in_scope = scope_rules.sql_filter(ctx.scope or "", SeoPage.url)
        if in_scope is not None:
            stmt = stmt.where(in_scope)
        pages = list(
            ctx.db.execute(stmt.order_by(SeoPage.updated_at.desc()).limit(PAGES_PER_RUN * 4)).scalars()
        )
        # Prefer pages that actually describe something purchasable.
        pages.sort(key=lambda p: specs.commercial_score(p.title, p.url), reverse=True)
        return pages[:PAGES_PER_RUN]

    def _segments(self, ctx: AgentContext) -> list[str]:
        """Live audience segments, so copy is written for a real reader."""
        clusters = ctx.db.execute(
            select(AudienceCluster)
            .where(
                AudienceCluster.tenant_id == ctx.tenant_id,
                AudienceCluster.is_live.is_(True),
            )
            .order_by(AudienceCluster.size.desc())
            .limit(4)
        ).scalars()
        labels = [c.label for c in clusters]
        return labels or ["in-market shoppers"]


# ── Publishing ─────────────────────────────────────────────────────────────
def _make_publisher(creative_id: str):  # noqa: ANN202
    def publish(ctx: AgentContext) -> None:
        creative = ctx.db.get(AdCreative, creative_id)
        if creative is None or creative.status == "live":
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

        remote_id = connector.upload_creative(
            headline=headline,
            body_copy=body_copy,
            call_to_action=creative.call_to_action,
            dimensions=creative.dimensions,
            audience_segment=creative.audience_segment,
        )
        creative.status = "live"
        creative.detail = {**(creative.detail or {}), "remote_id": remote_id}
        creative.updated_at = utcnow()
        ctx.db.flush()

    return publish


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
