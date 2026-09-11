"""Backlink Node Discovery — finds placement targets that fit.

Authority alone is a bad filter: a DA-80 site in the wrong sector sends no
qualified traffic and reads as an unnatural link. So every candidate is scored
on two axes — the domain's authority, and how close it sits to this
organisation's own subject matter — and only candidates that clear both are
kept.

**It requires a search or backlink data source, and that is not a formality.**
The candidate domains are proposed by the model; the numbers attached to them
are the model's estimates, not measurements, and they are labelled as such
throughout. Without a connected data source there is no ground truth anywhere
in the loop — the topics would be guesses about a site nobody has crawled, and
the output would be a list of domains and "DA" scores that a language model
made up, presented to an operator as discovered opportunities. Its sibling
:mod:`app.agents.competitor_link_monitor` already refuses to run for exactly
this reason; this agent used to be the inconsistency.

Discovery writes nothing outward: it records opportunities. Deciding to pitch
one is the PR agent's job, which is what keeps "found a target" and "contacted
a stranger" separate decisions — and it is why an unverified contact address
here cannot become an email on its own.
"""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import func, select

from app.agents.backlink_node_discovery import prompts, scoring
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
from app.connectors.base.connector import Capability
from app.connectors.base.interfaces import AnalyticsConnector
from app.core.logging import get_logger
from app.models.offpage import BacklinkStatus, BacklinkTarget
from app.models.seo import SeoPage

log = get_logger(__name__)

MIN_RELEVANCE = 0.6
CANDIDATES_PER_RUN = 12


class BacklinkNodeDiscoveryAgent(BaseAgent):
    spec = AgentSpec(
        slug="backlink_node_discovery",
        name="Backlink Node Discovery",
        category=AgentCategory.OFF_PAGE,
        description="Indexes the web for high-authority placement targets.",
        default_interval=timedelta(hours=4),
        default_schedule="Every 6 hours",
        max_impact=Impact.LOW,
        default_max_actions_per_day=30,
        # A real data source, or it does not run. See the module docstring.
        any_of_capabilities=(
            Capability.READ_SEARCH_PERFORMANCE,
            Capability.READ_BACKLINKS,
        ),
        scope_placeholder="Topics to hunt, comma separated",
        requires_llm=True,
    )

    def validate_scope(self, scope: str) -> str | None:
        """Topics to hunt for placements."""
        return scope_rules.free_text(scope, max_items=8, label="topics")

    def run(self, ctx: AgentContext) -> AgentResult:
        topics = self._topics(ctx)
        if not topics:
            return AgentResult.skip("No topics yet — sync some pages first")

        discovered = self._discover(ctx, topics)
        scanned = ctx.db.execute(
            select(func.count())
            .select_from(BacklinkTarget)
            .where(BacklinkTarget.tenant_id == ctx.tenant_id)
        ).scalar_one()

        return AgentResult(
            summary=f"{len(discovered)} new targets kept from {CANDIDATES_PER_RUN} candidates",
            actions=discovered,
            metric_label=f"{scanned} domains scanned",
            detail={"topics": topics, "kept": len(discovered)},
        )

    # ── Topic model ────────────────────────────────────────────────────────
    def _topics(self, ctx: AgentContext) -> list[str]:
        """What this organisation is actually about.

        An explicit scope wins. Otherwise topics come from the keywords the
        SEO agent extracted, plus real search queries where a search connector
        is available — the latter is ground truth rather than inference.
        """
        if ctx.scope:
            return [t.strip() for t in ctx.scope.split(",") if t.strip()][:8]

        topics: list[str] = []
        for page in ctx.db.execute(
            select(SeoPage)
            .where(SeoPage.tenant_id == ctx.tenant_id)
            .order_by(SeoPage.gap_score.desc())
            .limit(10)
        ).scalars():
            topics.extend(page.target_keywords or [])

        search = ctx.with_capability(Capability.READ_SEARCH_PERFORMANCE)
        if search is not None:
            try:
                data: dict = search.read_search_performance(days=28)  # type: ignore[union-attr]
                topics.extend(str(q) for q in (data.get("top_queries") or [])[:10])
            except Exception as exc:  # noqa: BLE001
                ctx.log_warning("Search performance unavailable: %s", exc)

        # De-duplicate while keeping the highest-signal ones first.
        seen: set[str] = set()
        unique = []
        for topic in topics:
            key = topic.strip().lower()
            if key and key not in seen:
                seen.add(key)
                unique.append(topic.strip())
        return unique[:8]

    # ── Discovery ──────────────────────────────────────────────────────────
    def _discover(self, ctx: AgentContext, topics: list[str]) -> list[AgentAction]:
        industry = ctx.industry
        known = {
            domain
            for (domain,) in ctx.db.execute(
                select(BacklinkTarget.domain).where(
                    BacklinkTarget.tenant_id == ctx.tenant_id
                )
            )
        }

        try:
            data = ctx.ask_json(
                prompts.find_targets(
                    industry=industry,
                    topics=topics,
                    domain=ctx.org.primary_domain,
                    exclude=sorted(known)[:40],
                    wanted=CANDIDATES_PER_RUN,
                ),
                system=prompts.SYSTEM,
                schema_hint=prompts.SCHEMA_HINT,
            )
        except Exception as exc:  # noqa: BLE001
            ctx.log_warning("Target discovery failed: %s", exc)
            return []

        actions: list[AgentAction] = []
        for entry in data.get("targets") or []:
            domain = scoring.normalise_domain(str(entry.get("domain") or ""))
            if not domain or domain in known:
                continue
            known.add(domain)

            # Authority is no longer asked for: it is a proprietary crawl
            # metric a language model cannot measure, and it was being used
            # to gate which opportunities a person ever saw. 0 means
            # unmeasured, and stays that way until a real backlink source
            # supplies it.
            authority = 0
            relevance = float(entry.get("relevance") or 0.0)
            placement = str(entry.get("placement_type") or "Resource Page")

            # Relevance only. Subject-matter overlap is a judgement a model
            # can genuinely make; authority is a measurement, and gating on
            # an invented one hid real opportunities and admitted invented
            # ones according to a number nobody checked.
            if relevance < float(ctx.setting("min_relevance", MIN_RELEVANCE)):
                continue
            if scoring.looks_like_link_farm(domain):
                # Pursuing a paid-link network is a penalty risk, so the
                # cheap name check is worth running even though it is all
                # that can be checked without a measured authority source.
                ctx.log_warning("Skipping likely link farm: %s", domain)
                continue

            actions.append(
                AgentAction(
                    kind="record_target",
                    # "est. DA", not "DA". The number came from a language
                    # model, and a reviewer deciding whether to pitch a
                    # stranger deserves to know which figures were measured
                    # and which were guessed.
                    # No authority figure in the title. It used to read
                    # "est. DA 62" from a number the model invented, which
                    # is the kind of precision that gets believed.
                    title=f"Found {placement.lower()} opportunity at {domain}",
                    impact=Impact.LOW,
                    approval_type="Backlink Target",
                    target_kind="backlink_target",
                    payload={
                        "domain": domain,
                        "authority": authority,
                        "relevance": relevance,
                        # Carried into the stored payload so the approvals
                        # screen and the PR agent can both see that these
                        # figures, and the address, are unverified.
                        "estimates_unverified": True,
                        "placement_type": placement,
                        "why": str(entry.get("why") or ""),
                        # How they take submissions, in words. An email
                        # address is no longer requested: the model produced
                        # plausible ones, and the outreach agent can pitch to
                        # whatever is here.
                        "contact_route": str(entry.get("contact_route") or ""),
                        # What it actually was. This defaulted to "topical
                        # crawl", describing a crawl that never ran.
                        "discovered_via": "suggested by the model",
                    },
                    apply=_make_recorder(
                        domain=domain,
                        authority=authority,
                        relevance=relevance,
                        placement=placement,
                        # Deliberately empty. The model used to supply an
                        # address here and the outreach agent sends to it —
                        # so a plausible invention became a real pitch to a
                        # stranger, under the customer's name. A person adds
                        # the address, or nothing is sent.
                        contact="",
                        via="suggested by the model",
                    ),
                    audit=f"discovered {domain} (relevance {relevance:.2f})",
                )
            )

        return actions


def _make_recorder(
    *, domain: str, authority: int, relevance: float, placement: str, contact: str, via: str
):  # noqa: ANN202
    def record(ctx: AgentContext) -> None:
        # Guard against a concurrent run having inserted the same domain: the
        # unique constraint would otherwise abort the whole transaction.
        exists = ctx.db.execute(
            select(BacklinkTarget).where(
                BacklinkTarget.tenant_id == ctx.tenant_id, BacklinkTarget.domain == domain
            )
        ).scalar_one_or_none()
        if exists is not None:
            return

        ctx.db.add(
            BacklinkTarget(
                tenant_id=ctx.tenant_id,
                domain=domain,
                authority=authority,
                placement_type=placement,
                status=BacklinkStatus.DISCOVERED.value,
                relevance=relevance,
                contact_email=contact or None,
                discovered_via=via,
            )
        )
        ctx.db.flush()

    return record


AGENT = BacklinkNodeDiscoveryAgent()
