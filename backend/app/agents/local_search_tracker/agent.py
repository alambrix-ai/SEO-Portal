"""Local Search Tracker — where the business appears in Google Maps searches.

For each phrase a customer might type ("salon near me", "unisex salon"), it
asks Google Places for the results around the business's own map pin, and
records the business's position and the places listed above it.

It changes nothing anywhere. Its value is the measurement: the same searches,
made the same way every day, so a move from 7th to 3rd is visible — along
with the ratings and review counts of the businesses it is competing with,
which are usually the reason it is not first.
"""
from __future__ import annotations

from datetime import timedelta

from app.agents.base import scope as scope_rules
from app.agents.base.context import AgentContext
from app.agents.base.contracts import AgentCategory, AgentResult, AgentSpec, BaseAgent, Impact
from app.agents.base.contracts import ConnectorUse, UseMode
from app.connectors.base.connector import Capability
from app.core.exceptions import ConnectorError
from app.db.base import utcnow
from app.models.local import LocalRankCheck

#: Phrases checked per run — each one is a billed Places search.
MAX_PHRASES = 5
#: Competitors kept with each check.
TOP_KEPT = 10


def default_phrases(category: str) -> list[str]:
    """Phrases to check when none are set, from the listing's own category."""
    category = (category or "").strip().lower()
    if not category:
        return []
    return [f"{category} near me", category]


class LocalSearchTrackerAgent(BaseAgent):
    spec = AgentSpec(
        slug="local_search_tracker",
        name="Local Search Tracker",
        category=AgentCategory.LOCAL,
        description=(
            "Tracks where your business appears in Google Maps searches such as "
            "“salon near me”, and who ranks above you."
        ),
        default_interval=timedelta(days=1),
        default_schedule="Daily",
        required_capabilities=(Capability.READ_LOCAL_RANKINGS,),
        max_impact=Impact.LOW,
        default_max_actions_per_day=1,
        scope_placeholder="salon near me, unisex salon, hair salon",
        connector_use=(
            ConnectorUse(
                Capability.READ_LOCAL_RANKINGS,
                UseMode.ONE,
                question="Which Google Maps search service should this agent use?",
            ),
        ),
    )

    def validate_scope(self, scope: str) -> str | None:
        return scope_rules.free_text(scope, max_items=MAX_PHRASES, label="search phrases")

    def run(self, ctx: AgentContext) -> AgentResult:
        places = ctx.with_capability(Capability.READ_LOCAL_RANKINGS)
        if places is None:
            return AgentResult.skip("Waiting on the Google Maps search integration")

        ctx.progress("Finding your business on Google Maps")
        try:
            business = places.find_business()  # type: ignore[union-attr]
        except ConnectorError as exc:
            return AgentResult.skip(str(exc))

        phrases = scope_rules.items(ctx.scope)[:MAX_PHRASES] or default_phrases(business.category)
        if not phrases:
            return AgentResult.skip(
                "Enter the searches to track in this agent's settings — for example "
                "“salon near me, unisex salon”."
            )

        now = utcnow()
        lines: list[str] = []
        positions: list[int] = []
        for index, phrase in enumerate(phrases, start=1):
            ctx.progress(f"Searching “{phrase}” ({index} of {len(phrases)})", done=index - 1, total=len(phrases))
            try:
                search = places.search_around(phrase, business)  # type: ignore[union-attr]
            except ConnectorError as exc:
                ctx.log_warning("Search for %s failed: %s", phrase, exc)
                lines.append(f"“{phrase}” could not be checked")
                continue
            position = search.position_of(business.place_id)
            ctx.db.add(
                LocalRankCheck(
                    tenant_id=ctx.tenant_id,
                    checked_at=now,
                    run_id=ctx.run_id or "",
                    phrase=phrase[:160],
                    position=position,
                    results_seen=len(search.results),
                    radius_m=places.radius_m,  # type: ignore[union-attr]
                    place_id=business.place_id,
                    business_name=business.name,
                    rating=business.rating,
                    review_count=business.review_count,
                    top_results=[
                        {
                            "position": rank,
                            "place_id": place.place_id,
                            "name": place.name,
                            "rating": place.rating,
                            "review_count": place.review_count,
                        }
                        for rank, place in enumerate(search.results[:TOP_KEPT], start=1)
                    ],
                )
            )
            if position is None:
                lines.append(f"“{phrase}”: not in the top {len(search.results)}")
            else:
                positions.append(position)
                lines.append(f"“{phrase}”: #{position} of {len(search.results)}")
        ctx.db.flush()

        best = min(positions) if positions else None
        return AgentResult(
            summary="; ".join(lines),
            metric_label=f"Best position #{best}" if best else "Not in the results yet",
            detail={"phrases": len(phrases), "found_in": len(positions)},
        )


AGENT = LocalSearchTrackerAgent()
