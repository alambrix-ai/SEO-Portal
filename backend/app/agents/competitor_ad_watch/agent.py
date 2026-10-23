"""Watch public Meta / Google ad libraries for competitor creatives."""
from __future__ import annotations

from datetime import timedelta
from typing import Callable

from sqlalchemy import select

from app.agents.base.context import AgentContext
from app.agents.base.contracts import (
    AgentCategory,
    AgentResult,
    AgentSpec,
    BaseAgent,
    Impact,
)
from app.db.base import utcnow
from app.models.competitor_ads import CompetitorAd
from app.services import competitor_ads as ads_svc
from app.services.encryption import Ctx

SLUG = "competitor_ad_watch"
MAX_ADVERTISERS = 5
#: Injected in tests to avoid live egress.
_FETCH_OVERRIDE: Callable[[str], bytes] | None = None


class CompetitorAdWatchAgent(BaseAgent):
    spec = AgentSpec(
        slug=SLUG,
        name="Competitor Ad Watch",
        category=AgentCategory.ADS,
        description=(
            "Fetches public Meta Ad Library and Google Ads Transparency creatives "
            "for configured competitors. All copy is stored encrypted and treated "
            "as untrusted."
        ),
        default_interval=timedelta(days=1),
        default_schedule="Daily",
        required_capabilities=(),
        max_impact=Impact.LOW,
        default_max_actions_per_day=0,
        scope_placeholder="Competitor advertiser names, comma separated",
        requires_llm=False,
    )

    def run(self, ctx: AgentContext) -> AgentResult:
        advertisers = [
            part.strip()[:200]
            for part in (ctx.scope or "").split(",")
            if part.strip()
        ][:MAX_ADVERTISERS]
        if not advertisers:
            return AgentResult.skip(
                "Configure competitor advertiser names in the agent scope"
            )

        new_count = 0
        seen_count = 0
        errors = 0
        for advertiser in advertisers:
            for platform in ("meta", "google"):
                try:
                    snapshots = ads_svc.fetch_snapshots(
                        platform,
                        advertiser,
                        fetch=_FETCH_OVERRIDE,
                    )
                except Exception as exc:  # noqa: BLE001 — one bad host must not stop the run
                    errors += 1
                    ctx.log_warning(
                        "Competitor ad fetch failed for %s/%s: %s",
                        platform,
                        advertiser[:40],
                        exc,
                    )
                    continue
                for snap in snapshots:
                    # Fence before any future LLM use; never log the copy.
                    _ = ads_svc.as_untrusted(snap)
                    existing = ctx.db.execute(
                        select(CompetitorAd).where(
                            CompetitorAd.tenant_id == ctx.tenant_id,
                            CompetitorAd.platform == snap.platform,
                            CompetitorAd.external_id == snap.external_id,
                        )
                    ).scalar_one_or_none()
                    now = utcnow()
                    if existing is not None:
                        existing.last_seen_at = now
                        existing.content_hash = snap.content_hash
                        if snap.copy_text:
                            existing.copy_encrypted = ctx.cipher.encrypt(
                                snap.copy_text, context=Ctx.COMPETITOR_AD_COPY
                            )
                        seen_count += 1
                        continue
                    row = CompetitorAd(
                        tenant_id=ctx.tenant_id,
                        platform=snap.platform,
                        advertiser=snap.advertiser,
                        external_id=snap.external_id,
                        copy_encrypted=ctx.cipher.encrypt(
                            snap.copy_text, context=Ctx.COMPETITOR_AD_COPY
                        )
                        if snap.copy_text
                        else "",
                        source_url=snap.source_url[:1000],
                        content_hash=snap.content_hash,
                        first_seen_at=now,
                        last_seen_at=now,
                        detail={"platform": snap.platform},
                    )
                    ctx.db.add(row)
                    new_count += 1
        ctx.db.flush()
        if new_count == 0 and seen_count == 0:
            return AgentResult.skip(
                f"No public ads found ({errors} fetch error{'s' if errors != 1 else ''})"
                if errors
                else "No public ads found for the configured advertisers"
            )
        return AgentResult(
            summary=(
                f"Recorded {new_count} new competitor ad{'s' if new_count != 1 else ''}"
                + (f"; refreshed {seen_count}" if seen_count else "")
            ),
            metric_label=f"{new_count} new ad",
            metrics={
                "competitor_ads_new": new_count,
                "competitor_ads_seen": seen_count,
                "competitor_ads_errors": errors,
            },
        )


AGENT = CompetitorAdWatchAgent()
