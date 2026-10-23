"""Run local rank checks across every active location in the workspace."""
from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

from sqlalchemy import select

from app.agents.base.context import AgentContext
from app.agents.base.contracts import (
    AgentCategory,
    AgentResult,
    AgentSpec,
    BaseAgent,
    ConnectorUse,
    Impact,
    UseMode,
)
from app.connectors.base.connector import Capability
from app.core.exceptions import ConnectorError
from app.db.base import utcnow
from app.models.local import LocalRankCheck
from app.models.locations import Location
from app.services import locations as loc_svc

SLUG = "multi_location_local_seo"
MAX_LOCATIONS = 25
MAX_PHRASES = 5
TOP_KEPT = 10


class MultiLocationLocalSeoAgent(BaseAgent):
    spec = AgentSpec(
        slug=SLUG,
        name="Multi-Location Local SEO",
        category=AgentCategory.LOCAL,
        description=(
            "Tracks Maps rankings for every active location (brand/region scoped), "
            "reusing the local rank check pipeline."
        ),
        default_interval=timedelta(days=1),
        default_schedule="Daily",
        required_capabilities=(Capability.READ_LOCAL_RANKINGS,),
        max_impact=Impact.LOW,
        default_max_actions_per_day=1,
        scope_placeholder="Optional region or brand filter",
        connector_use=(
            ConnectorUse(
                Capability.READ_LOCAL_RANKINGS,
                UseMode.ONE,
                question="Which Google Maps search service should multi-location tracking use?",
            ),
        ),
    )

    def run(self, ctx: AgentContext) -> AgentResult:
        places = ctx.with_capability(Capability.READ_LOCAL_RANKINGS)
        if places is None:
            return AgentResult.skip("Waiting on the Google Maps search integration")

        locations = list(
            ctx.db.execute(
                select(Location).where(
                    Location.tenant_id == ctx.tenant_id,
                    Location.status == "active",
                ).limit(MAX_LOCATIONS)
            ).scalars()
        )
        scope_filter = (ctx.scope or "").strip().lower()
        if scope_filter:
            locations = [
                loc
                for loc in locations
                if scope_filter in {
                    (loc.region or "").lower(),
                    (loc.brand or "").lower(),
                    (loc.slug or "").lower(),
                    (loc.city or "").lower(),
                }
                or scope_filter in (loc.name or "").lower()
            ]
        if not locations:
            return AgentResult.skip("No active locations match this agent's scope")

        now = utcnow()
        checks = 0
        found = 0
        for location in locations:
            business = SimpleNamespace(
                place_id=location.place_id or location.gbp_location_id or location.slug,
                name=location.name,
                rating=None,
                review_count=None,
                category="",
                lat=location.lat,
                lng=location.lng,
            )
            phrases = loc_svc.default_phrases_for(location.city, location.name)[:MAX_PHRASES]
            if not phrases:
                phrases = [f"{location.name} near me"]
            for phrase in phrases:
                try:
                    search = places.search_around(phrase, business)  # type: ignore[union-attr]
                except ConnectorError as exc:
                    ctx.log_warning(
                        "Location search failed for %s / %s: %s",
                        location.slug,
                        phrase[:40],
                        exc,
                    )
                    continue
                except Exception as exc:  # noqa: BLE001
                    ctx.log_warning(
                        "Location search failed for %s: %s", location.slug, exc
                    )
                    continue
                position = None
                results = getattr(search, "results", []) or []
                position_of = getattr(search, "position_of", None)
                if callable(position_of):
                    position = position_of(business.place_id)
                ctx.db.add(
                    LocalRankCheck(
                        tenant_id=ctx.tenant_id,
                        location_id=location.id,
                        checked_at=now,
                        run_id=ctx.run_id or "",
                        phrase=phrase[:160],
                        position=position,
                        results_seen=len(results),
                        radius_m=int(getattr(places, "radius_m", 0) or 0),
                        place_id=str(business.place_id)[:255],
                        business_name=location.name[:255],
                        rating=None,
                        review_count=None,
                        top_results=[
                            {
                                "position": rank,
                                "place_id": getattr(place, "place_id", ""),
                                "name": getattr(place, "name", ""),
                                "rating": getattr(place, "rating", None),
                                "review_count": getattr(place, "review_count", None),
                            }
                            for rank, place in enumerate(results[:TOP_KEPT], start=1)
                        ],
                    )
                )
                checks += 1
                if position is not None:
                    found += 1
        ctx.db.flush()
        if checks == 0:
            return AgentResult.skip("No rank checks could be recorded this run")
        return AgentResult(
            summary=f"Checked {len(locations)} location{'s' if len(locations) != 1 else ''} ({checks} searches)",
            metric_label=f"{found} ranked" if found else "Not in results yet",
            metrics={
                "locations_checked": len(locations),
                "local_rank_checks": checks,
                "local_rank_found": found,
            },
        )


AGENT = MultiLocationLocalSeoAgent()
