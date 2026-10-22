"""Backlink Node Discovery — finds placement targets that fit.

Authority alone is a bad filter: a DA-80 site in the wrong sector sends no
qualified traffic and reads as an unnatural link. So every candidate is scored
on two axes — the domain's authority, and how close it sits to this
organisation's own subject matter — and only candidates that clear the
relevance floor are kept.

**It requires a search or backlink data source, and that is not a formality.**
The candidate domains are proposed by the model, from topics taken from real
search queries. Without a connected data source there is no ground truth
anywhere in the loop — the topics would be guesses about a site nobody has
crawled, and the output would be a list of domains a language model made up,
presented to an operator as discovered opportunities.

When a backlink index is connected (``READ_BACKLINKS`` — DataForSEO), every
suggestion is looked up in it: a domain the index has never seen is dropped as
probably not existing, and the authority shown is the index's measured figure.
Without one, authority stays 0, which means *unmeasured* and is shown as such.

Discovery writes nothing outward: it records opportunities. Deciding to pitch
one is the PR agent's job, which is what keeps "found a target" and "contacted
a stranger" separate decisions — and it is why no contact address is recorded
here. A person adds it on the Off-Page screen.
"""
from __future__ import annotations

import uuid
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

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
from app.agents.base.contracts import ConnectorUse, UseMode
from app.connectors.base.connector import Capability
from app.core.logging import get_logger
from app.models.approval import ApprovalItem, ApprovalStatus
from app.models.offpage import BacklinkStatus, BacklinkTarget
from app.models.seo import SeoPage
from app.models.workspace import Organization
from app.services.approvals import register_applier
from app.services.encryption import Ctx

log = get_logger(__name__)

SLUG = "backlink_node_discovery"
TARGET_KIND = "backlink_target"
MIN_RELEVANCE = 0.6
CANDIDATES_PER_RUN = 12
SUGGESTED = "suggested by the model"

#: Namespace for the approval ``target_id`` of a suggested domain. The target
#: row does not exist until the suggestion is approved, so the id is derived
#: from the domain: stable across runs (one pending card per domain, not one
#: card for every suggestion ever made) and a valid UUID for the column.
_TARGET_NS = uuid.UUID("7d0f3c1e-5b8a-4c1f-9a51-3f0e2b6d8c42")


def target_key(domain: str) -> str:
    """The approval ``target_id`` for a suggested domain."""
    return str(uuid.uuid5(_TARGET_NS, scoring.normalise_domain(domain)))


class BacklinkNodeDiscoveryAgent(BaseAgent):
    spec = AgentSpec(
        slug=SLUG,
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
        connector_use=(
            ConnectorUse(
                Capability.READ_SEARCH_PERFORMANCE,
                UseMode.ONE,
                question="Which search data should this agent take topics from?",
            ),
            ConnectorUse(
                Capability.READ_BACKLINKS,
                UseMode.ONE,
                question=(
                    "Which backlink index should this agent check suggestions against?"
                ),
            ),
        ),
    )

    def validate_scope(self, scope: str) -> str | None:
        """Topics to hunt for placements."""
        return scope_rules.free_text(scope, max_items=8, label="topics")

    def run(self, ctx: AgentContext) -> AgentResult:
        topics = self._topics(ctx)
        if not topics:
            return AgentResult.skip("No topics yet — sync some pages first")

        discovered, stats = self._discover(ctx, topics)
        on_file = self._target_count(ctx)

        summary = f"{len(discovered)} new targets kept of {stats['suggested']} suggested"
        dropped = [
            f"{stats[key]} {label}"
            for key, label in (
                ("duplicates", "already known or awaiting review"),
                ("low_relevance", "below the relevance floor"),
                ("link_farms", "likely link sellers"),
                ("not_indexed", "not found in the backlink index"),
            )
            if stats[key]
        ]
        if dropped:
            summary += " (dropped: " + ", ".join(dropped) + ")"
        if stats["measured"]:
            summary += f"; authority measured for {stats['measured']}"

        return AgentResult(
            summary=summary,
            actions=discovered,
            metric_label=f"{on_file} targets on file",
            detail={"topics": topics, "kept": len(discovered), **stats},
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

    # ── What not to suggest again ──────────────────────────────────────────
    @staticmethod
    def _known_domains(ctx: AgentContext) -> set[str]:
        """Targets already on file."""
        return {
            domain
            for (domain,) in ctx.db.execute(
                select(BacklinkTarget.domain).where(BacklinkTarget.tenant_id == ctx.tenant_id)
            )
        }

    @staticmethod
    def _proposed_domains(ctx: AgentContext) -> set[str]:
        """Suggestions awaiting review, or already turned down by a person.

        Read from the approval payloads, since the target row only exists once
        a suggestion is approved. Without this the same domains were suggested
        on every run.
        """
        domains: set[str] = set()
        for (token,) in ctx.db.execute(
            select(ApprovalItem.payload_encrypted).where(
                ApprovalItem.tenant_id == ctx.tenant_id,
                ApprovalItem.agent_slug == SLUG,
                ApprovalItem.target_kind == TARGET_KIND,
                ApprovalItem.status.in_(
                    (ApprovalStatus.PENDING.value, ApprovalStatus.REJECTED.value)
                ),
            )
        ):
            payload = ctx.cipher.decrypt_json(token, context=Ctx.APPROVAL_PAYLOAD, default={})
            domain = scoring.normalise_domain(str((payload or {}).get("domain") or ""))
            if domain:
                domains.add(domain)
        return domains

    @staticmethod
    def _target_count(ctx: AgentContext) -> int:
        return ctx.db.execute(
            select(func.count())
            .select_from(BacklinkTarget)
            .where(BacklinkTarget.tenant_id == ctx.tenant_id)
        ).scalar_one()

    # ── Discovery ──────────────────────────────────────────────────────────
    def _discover(
        self, ctx: AgentContext, topics: list[str]
    ) -> tuple[list[AgentAction], dict[str, int]]:
        stats = {
            "suggested": 0,
            "duplicates": 0,
            "low_relevance": 0,
            "link_farms": 0,
            "not_indexed": 0,
            "measured": 0,
        }
        known = self._known_domains(ctx) | self._proposed_domains(ctx)

        try:
            data = ctx.ask_json(
                prompts.find_targets(
                    industry=ctx.industry,
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
            return [], stats

        index = ctx.with_capability(Capability.READ_BACKLINKS)
        lookup = getattr(index, "read_domain_summary", None) if index is not None else None
        floor = float(ctx.setting("min_relevance", MIN_RELEVANCE))

        actions: list[AgentAction] = []
        entries = [e for e in (data.get("targets") or []) if isinstance(e, dict)]
        stats["suggested"] = len(entries)
        for entry in entries:
            domain = scoring.normalise_domain(str(entry.get("domain") or ""))
            if not domain or domain in known:
                stats["duplicates"] += 1
                continue
            known.add(domain)

            try:
                relevance = float(entry.get("relevance") or 0.0)
            except (TypeError, ValueError):
                relevance = 0.0
            placement = str(entry.get("placement_type") or "Resource Page")

            # Relevance only. Subject-matter overlap is a judgement a model
            # can genuinely make; authority is a measurement, and gating on
            # an invented one hid real opportunities and admitted invented
            # ones according to a number nobody checked.
            if relevance < floor:
                stats["low_relevance"] += 1
                continue
            if scoring.looks_like_link_farm(domain):
                # Pursuing a paid-link network is a penalty risk, so the
                # cheap name check is worth running.
                ctx.log_warning("Skipping likely link farm: %s", domain)
                stats["link_farms"] += 1
                continue

            # Authority is never asked of the model: it is a proprietary
            # crawl metric a language model cannot measure. 0 means
            # unmeasured, and stays that way unless the backlink index
            # supplies it.
            authority = 0
            referring = 0
            measured = False
            via = SUGGESTED
            if callable(lookup):
                try:
                    profile = lookup(domain=domain)
                except Exception as exc:  # noqa: BLE001 - enrichment is optional
                    ctx.log_warning("Backlink index lookup failed for %s: %s", domain, exc)
                    profile = None
                if profile is not None:
                    if not profile.get("found"):
                        # The index has never seen a link to it. For a
                        # domain a model proposed, that usually means the
                        # publication does not exist.
                        ctx.log_info("Dropping %s: not in the backlink index", domain)
                        stats["not_indexed"] += 1
                        continue
                    authority = int(profile.get("authority") or 0)
                    referring = int(profile.get("referring_domains") or 0)
                    measured = authority > 0
                    via = f"{SUGGESTED}; checked in {index.name}"[:64]  # type: ignore[union-attr]
                    if measured:
                        stats["measured"] += 1

            why = str(entry.get("why") or "")
            route = str(entry.get("contact_route") or "")
            title = f"Found {placement.lower()} opportunity at {domain}"
            if measured:
                title += f" (authority {authority}, measured)"

            actions.append(
                AgentAction(
                    kind="record_target",
                    # No unmeasured authority figure in the title. It used to
                    # read "est. DA 62" from a number the model invented,
                    # which is the kind of precision that gets believed.
                    title=title,
                    impact=Impact.LOW,
                    approval_type="Backlink Target",
                    target_kind=TARGET_KIND,
                    # One pending card per domain. With no target_id every
                    # suggestion matched the same "target_id IS NULL" pending
                    # item, so a run's proposals collapsed into one card.
                    target_id=target_key(domain),
                    payload={
                        "domain": domain,
                        "authority": authority,
                        "authority_measured": measured,
                        "referring_domains": referring,
                        "relevance": relevance,
                        # Carried into the stored payload so the approvals
                        # screen and the PR agent can both see which figures
                        # were measured. Relevance is always the model's view.
                        "estimates_unverified": not measured,
                        "placement_type": placement,
                        "why": why,
                        # How they take submissions, in words. An email
                        # address is not requested: the model produced
                        # plausible ones, and the outreach agent sends to
                        # whatever is recorded. A person adds the address.
                        "contact_route": route,
                        "discovered_via": via,
                    },
                    apply=_make_recorder(
                        domain=domain,
                        authority=authority,
                        relevance=relevance,
                        placement=placement,
                        via=via,
                        notes=_notes(why, route),
                    ),
                    audit=f"discovered {domain} (relevance {relevance:.2f})",
                )
            )

        return actions, stats


def _notes(why: str, route: str) -> str:
    parts = [why.strip(), f"How to submit: {route.strip()}" if route.strip() else ""]
    return "\n".join(p for p in parts if p)


def record_target(
    db: Session,
    *,
    tenant_id: str,
    domain: str,
    authority: int,
    relevance: float,
    placement: str,
    via: str,
    notes: str = "",
) -> BacklinkTarget | None:
    """Store a suggested target. The one path, autonomous or approved.

    No contact address: a person adds it, or nothing is ever emailed.
    """
    # Guard against a concurrent run having inserted the same domain: the
    # unique constraint would otherwise abort the whole transaction.
    exists = db.execute(
        select(BacklinkTarget).where(
            BacklinkTarget.tenant_id == tenant_id, BacklinkTarget.domain == domain
        )
    ).scalar_one_or_none()
    if exists is not None:
        return None

    target = BacklinkTarget(
        tenant_id=tenant_id,
        domain=domain,
        authority=authority,
        placement_type=placement[:40],
        status=BacklinkStatus.DISCOVERED.value,
        relevance=relevance,
        contact_email=None,
        discovered_via=via[:64],
        notes=notes,
    )
    db.add(target)
    db.flush()
    return target


def _make_recorder(
    *, domain: str, authority: int, relevance: float, placement: str, via: str, notes: str = ""
):  # noqa: ANN202
    def record(ctx: AgentContext) -> None:
        record_target(
            ctx.db,
            tenant_id=ctx.tenant_id,
            domain=domain,
            authority=authority,
            relevance=relevance,
            placement=placement,
            via=via,
            notes=notes,
        )

    return record


@register_applier(TARGET_KIND)
def apply_backlink_target(
    db: Session, org: Organization, item: ApprovalItem, payload: dict
) -> None:
    """Approving a suggestion records it — exactly what the autonomous path does.

    There was no applier, so approving one of these failed with "its handler
    is missing" and no suggestion could ever be accepted.
    """
    domain = scoring.normalise_domain(str(payload.get("domain") or ""))
    if not domain:
        raise ValueError("This suggestion has no domain to record")
    try:
        relevance = float(payload.get("relevance") or 0.0)
    except (TypeError, ValueError):
        relevance = 0.0
    record_target(
        db,
        tenant_id=org.id,
        domain=domain,
        authority=int(payload.get("authority") or 0),
        relevance=relevance,
        placement=str(payload.get("placement_type") or "Resource Page"),
        via=str(payload.get("discovered_via") or SUGGESTED),
        notes=_notes(str(payload.get("why") or ""), str(payload.get("contact_route") or "")),
    )


AGENT = BacklinkNodeDiscoveryAgent()
