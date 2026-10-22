"""The contract every agent implements.

An agent is a class in its own package under ``app/agents/<slug>/`` that:

1. declares a :class:`AgentSpec` — identity, category, cadence, the connectors
   it needs, and what its actions require in the way of approval;
2. implements ``run(ctx) -> AgentResult`` — one idempotent pass over the work
   it owns.

Agents do not touch HTTP, sessions, or the approval queue directly. They read
through the context, propose :class:`AgentAction` objects, and the runner
decides — from the guardrail in force — whether each one is applied now or
queued for a human. That single decision point is why "autonomous" and
"human-in-the-loop" are one switch rather than eleven separate code paths.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from app.connectors.base.connector import Capability

if TYPE_CHECKING:  # pragma: no cover
    from app.agents.base.context import AgentContext


class AgentCategory(StrEnum):
    SEO_AEO = "SEO & AEO"
    OFF_PAGE = "Off-Page"
    ADS = "Ads"
    LOCAL = "Local business"
    CONTENT = "Content & Social"


class Impact(StrEnum):
    """How consequential one action is.

    The ``hybrid`` guardrail — agents draft, high-impact changes need approval —
    is exactly the line between ``LOW`` and ``HIGH`` here.
    """

    LOW = "low"
    HIGH = "high"


@dataclass(slots=True)
class AgentAction:
    """One thing an agent wants to do.

    ``apply`` performs it. The runner calls it immediately under full autonomy,
    or stores ``payload`` on an approval item and calls it later, after a human
    says yes — so an approved action takes the same path as an autonomous one.
    """

    kind: str
    title: str
    impact: Impact = Impact.LOW
    approval_type: str = ""
    target_kind: str = ""
    target_id: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    # Set by the agent; invoked by the runner or by the approvals service.
    apply: Any = None
    # Human-readable line for the audit log.
    audit: str = ""
    # Counters that become true only when this change goes live ("1 page
    # optimised", "1 spam referrer blocked"). Recorded when it is applied —
    # automatically by the runner, or later when a person approves it — so
    # the dashboard never counts proposals that are waiting or were refused.
    metrics: dict[str, int | float] = field(default_factory=dict)
    # Connector slugs this action writes through when applied ("google_ads").
    # Declared by the spending agents so the runner can hold a write through
    # a connector that has not been verified against a live account
    # (app.agents.base.safety). Empty means it reaches no vendor.
    writes_via: tuple[str, ...] = ()


@dataclass(slots=True)
class AgentResult:
    """What one run produced."""

    summary: str = ""
    actions: list[AgentAction] = field(default_factory=list)
    # Overwrites the agent card's metric line, e.g. "142 pages synced".
    metric_label: str = ""
    # Counters folded into today's DailyMetric row.
    metrics: dict[str, int | float] = field(default_factory=dict)
    detail: dict[str, Any] = field(default_factory=dict)
    # True when the agent found nothing to do — recorded as a skipped run.
    skipped: bool = False
    skip_reason: str = ""

    @classmethod
    def skip(cls, reason: str) -> AgentResult:
        return cls(summary=reason, skipped=True, skip_reason=reason)


class UseMode(StrEnum):
    """How an agent uses the connectors that can serve one capability.

    Declared per agent (``AgentSpec.connector_use``) next to the code that
    does the lookup, and read by the setup screen, the configuration
    validator and the runtime — so what the screen says about "which one
    this agent uses" is the same rule the run follows.
    """

    #: Picks a single connector. With two or more connected the operator
    #: chooses which (``AgentContext.with_capability`` / ``candidates``);
    #: unchosen, the automatic rule in ``ConnectorUse.automatic`` applies.
    ONE = "one"
    #: Works across every connected connector with the capability
    #: (``AgentContext.all_with_capability``). There is nothing to choose.
    ALL = "all"
    #: Works on pages already collected (``SeoPage.cms_slug`` says which
    #: website each came from) and writes back to that same website. With
    #: ``multi`` the operator may limit it to some of the connected websites
    #: (``AgentContext.page_sources``); nothing chosen means every one.
    PAGE_SOURCE = "page_source"


@dataclass(frozen=True, slots=True)
class ConnectorUse:
    """One capability an agent looks up, and whether it uses one or all."""

    capability: Capability
    mode: UseMode
    #: ONE: the question the setup screen asks, e.g. "Which website should
    #: this agent publish to?".
    question: str = ""
    #: ALL: the plural noun for "Uses all your connected …", e.g. "ad platforms".
    group: str = ""
    #: ONE: other capabilities served by the same chosen connector (Review
    #: Responder reads and replies through the same profile).
    also: tuple[Capability, ...] = ()
    #: ONE: True when, with nothing chosen, the agent tries each connected
    #: one in turn rather than taking the first (``AgentContext.candidates``).
    tries_in_turn: bool = False
    #: ONE + tries_in_turn: what "works" means, completing "…and uses the
    #: first one that …", e.g. "returns pages it can work on".
    automatic: str = ""
    #: Any mode: a further sentence that is true of this use, shown as is.
    note: str = ""
    #: ONE / PAGE_SOURCE: the operator may choose several (stored as a
    #: list). ONE + multi means "use every chosen one" — e.g. collect pages
    #: from each selected website; without it, at most one may be chosen.
    multi: bool = False
    #: PAGE_SOURCE: the selection is the one made for this other capability
    #: of the same agent, rather than a second question (On-Page SEO Sync
    #: rewrites only pages from the websites it was told to collect from).
    follows: Capability | None = None
    #: With a selection: what the agent does with it, as a clause with
    #: ``{names}``, e.g. "collects pages from {names}". Default "uses {names}".
    selected: str = ""
    #: PAGE_SOURCE with nothing selected: what it does, as a clause, e.g.
    #: "audits every page On-Page SEO Sync has collected".
    everything: str = ""
    #: An agent setting this selection supersedes. It is still read while no
    #: selection is saved (so older configurations keep working), and saving
    #: a selection removes it, leaving one source of truth.
    replaces_setting: str = ""


@dataclass(frozen=True, slots=True)
class AgentSpec:
    """Static declaration of one agent.

    ``slug`` must equal the package directory name; the registry checks it.
    """

    slug: str
    name: str
    category: AgentCategory
    description: str
    # Cadence when the operator has not overridden it in the Configure dialog.
    default_interval: timedelta
    default_schedule: str = "Daily"
    # Capabilities without which the agent cannot do useful work. A run is
    # skipped (not failed) when none is available — that is a setup gap, not
    # a bug.
    #
    # Declared as capabilities rather than vendor names on purpose: the agent
    # needs *a CMS* or *an ad platform*, so a new integration satisfies it
    # without the agent knowing the integration exists.
    required_capabilities: tuple[Capability, ...] = ()
    # Any one of these is enough, where several sources are interchangeable.
    any_of_capabilities: tuple[Capability, ...] = ()
    # Highest impact this agent can produce; drives the hybrid guardrail.
    max_impact: Impact = Impact.HIGH
    default_max_actions_per_day: int = 20
    scope_placeholder: str = ""
    # Continuous agents show "ongoing" rather than a countdown.
    continuous: bool = False
    # When True the Configure dialog asks which connected AI model to use,
    # and a run is skipped until that connector is chosen and healthy.
    requires_llm: bool = False
    # Every capability the agent looks connectors up by (other than the AI
    # model, which ``requires_llm`` covers), each marked as using one
    # connector or all of them. The ONE entries are what the operator may
    # choose between; see :class:`ConnectorUse`.
    connector_use: tuple[ConnectorUse, ...] = ()
    # Runs only when a job asks for it (Creative Studio, for instance), with
    # that job's payload on ``AgentContext.job`` — never on a schedule. Not
    # part of the fleet: ``registry.all_agents()`` leaves it out, so it is not
    # installed in a workspace and has no card in the Agents hub.
    on_demand: bool = False

    def use_for(self, capability: Capability) -> ConnectorUse | None:
        """The declaration covering ``capability`` (directly or via ``also``)."""
        for use in self.connector_use:
            if use.capability is capability:
                return use
        for use in self.connector_use:
            if capability in use.also:
                return use
        return None


class BaseAgent(ABC):
    """Base class for every agent in the fleet."""

    spec: AgentSpec

    def __init__(self) -> None:
        if not getattr(self, "spec", None):
            raise TypeError(f"{type(self).__name__} must declare a class-level `spec`")

    @property
    def slug(self) -> str:
        return self.spec.slug

    @abstractmethod
    def run(self, ctx: AgentContext) -> AgentResult:
        """Do one pass of work.

        Must be safe to call repeatedly: the scheduler retries, an operator can
        trigger a run by hand, and a crash mid-run leaves the previous state.
        """

    # ── Optional hooks ─────────────────────────────────────────────────────
    def validate_scope(self, scope: str) -> str | None:
        """Return a problem with the submitted scope, or ``None`` if it is fine.

        Scope means something different to each agent — path prefixes for the
        CMS agents, competitor domains for the link monitor, topics for
        discovery — so each one checks its own, and only the agent knows what
        good looks like.

        Checked when the dialog is submitted rather than when the agent next
        runs. A scope of ``inventory/*`` instead of ``/inventory/*`` matches
        nothing, and finding that out six hours later from a run that reported
        "0 pages" is the difference between a typo and an afternoon.
        """
        return None

    def on_configure(self, ctx: AgentContext, config: dict[str, Any]) -> None:
        """Called when an operator saves the Configure dialog."""

    def preflight(self, ctx: AgentContext) -> str | None:
        """Return a reason to skip this run, or ``None`` to proceed.

        The default checks the agent's declared capability requirements, and
        reports a missing one in words an operator can act on.
        """
        # The agent's own scope, checked against its own validator, before
        # anything is fetched. Configure refuses a bad scope now, but a value
        # saved before that validator existed is still in the database, and a
        # stored typo silently matched nothing for as long as it sat there:
        # one workspace had "inventory" without the leading slash, so every
        # page was filtered out and the run then blamed the connector. A
        # skip that names the scope is the difference between a minute and an
        # afternoon.
        scope_problem = self.validate_scope((ctx.scope or "").strip())
        if scope_problem:
            return f"The scope saved for this agent is unusable. {scope_problem}"

        missing = [
            capability
            for capability in self.spec.required_capabilities
            if ctx.with_capability(capability) is None
        ]
        if missing:
            needed = ", ".join(_describe(capability) for capability in missing)
            return f"Waiting on a connector that can {needed}"

        if self.spec.any_of_capabilities and not any(
            ctx.with_capability(capability) is not None
            for capability in self.spec.any_of_capabilities
        ):
            needed = " or ".join(
                _describe(capability) for capability in self.spec.any_of_capabilities
            )
            return f"Waiting on a connector that can {needed}"

        # A model named for this one run (a job's choice) was already resolved
        # strictly when the context was built; the saved choice does not apply.
        if self.spec.requires_llm and getattr(ctx, "llm_override", None) is None:
            choice = (getattr(ctx.record, "llm_connector", None) or "").strip()
            if not choice:
                return (
                    "Choose which AI model this agent should use — open "
                    "Configure and pick a connected model."
                )
            if choice not in ctx.connectors:
                name = ctx.connector_names.get(choice, choice)
                return (
                    f"Waiting on {name}. Connect it under Connectors, then "
                    "save this agent's configuration again."
                )
        return None

    def __repr__(self) -> str:  # pragma: no cover
        return f"<{type(self).__name__} {self.spec.slug}>"


# Phrasings that read as English in "Waiting on a connector that can …".
# Only the ones the mechanical de-underscoring gets wrong are listed; the rest
# fall through and stay correct without needing an entry here.
_PHRASING: dict[Capability, str] = {
    Capability.READ_PAGE: "read pages",
    Capability.WRITE_PAGE: "write pages",
    Capability.INJECT_SCHEMA: "inject schema markup",
    Capability.WRITE_AD_BUDGET: "set ad budgets",
    Capability.UPLOAD_CREATIVE: "upload creatives",
    Capability.PUSH_AUDIENCE: "push audiences",
    Capability.BLOCK_PLACEMENT: "block placements",
    Capability.READ_FIRST_PARTY_SIGNALS: "read first-party signals",
    Capability.SEND_NOTIFICATION: "send notifications",
    Capability.READ_LOCAL_RANKINGS: "check your position in Google Maps searches",
    Capability.READ_REVIEWS: "read your Google reviews",
    Capability.REPLY_REVIEW: "reply to your Google reviews",
    Capability.CREATE_PAGE: "publish new pages",
    Capability.PUBLISH_SOCIAL: "post to social media",
    Capability.CREATE_EMAIL_CAMPAIGN: "create email campaigns",
    Capability.CREATE_SEARCH_CAMPAIGN: "create search campaigns",
    Capability.GENERATE_IMAGE: "create images",
}


def _describe(capability: Capability) -> str:
    """A capability in words, for a skip message an operator can act on."""
    return _PHRASING.get(capability, capability.value.replace("_", " "))
