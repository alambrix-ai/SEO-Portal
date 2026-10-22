"""Everything an agent is handed for one run.

The context is the agent's only door to the outside: the database session, its
own configuration row, the organisation's cipher, the LLM, and the connectors
it declared. Keeping it narrow is what makes agents testable — a fake context
with two connectors and the mock provider exercises a real agent end to end.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from typing import TYPE_CHECKING, Any

from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.connectors.base.connector import Capability
from app.llm.base import LLMProvider, LLMResponse, LLMUsage
from app.models.agent import AgentRecord
from app.models.workspace import Organization
from app.services.encryption import OrgCipher

if TYPE_CHECKING:  # pragma: no cover
    from app.connectors.base.connector import BaseConnector

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class LLMOverride:
    """The AI model a job named for one run, used exactly or not at all.

    ``connector`` is the model connector's slug; empty means the agent's saved
    choice. ``model`` is a model id for that connection; ``None`` means the
    connection's saved model. Either way the choice is strict: when it cannot
    be used the run stops rather than writing with a different model. The
    caller is responsible for checking ``model`` against any allow-list.
    """

    connector: str = ""
    model: str | None = None


@dataclass(slots=True)
class AgentContext:
    db: Session
    org: Organization
    record: AgentRecord
    cipher: OrgCipher
    llm: LLMProvider
    # Connector instances, keyed by slug, already carrying decrypted credentials.
    connectors: dict[str, BaseConnector] = field(default_factory=dict)
    # Names of every connector the platform knows, for readable skip messages.
    connector_names: dict[str, str] = field(default_factory=dict)
    # Slugs the organisation has actually connected.
    connected_slugs: frozenset[str] = frozenset()
    # Accumulated token spend for this run.
    usage: LLMUsage = field(default_factory=LLMUsage)
    trigger: str = "schedule"
    #: The run row this pass is writing to, so it can report progress. Set by
    #: the runner; None for a context built outside a run (an approval being
    #: applied, for instance) where there is nothing to report to.
    run_id: str | None = None
    #: ``(agent slug, capability) -> ConnectorPolicy`` — what the plan lets
    #: this agent use (app.services.entitlements). Built on first use.
    policies: Any = None
    #: The job an on-demand run was started for (read-only), or None for a
    #: scheduled or "Run now" pass. Untrusted input: an agent validates what
    #: it reads from it and never logs it.
    job: Mapping[str, Any] | None = None
    #: The model a job named for this run, resolved strictly; None otherwise.
    llm_override: LLMOverride | None = None
    #: True when this run may only use its chosen model (a job's choice, or
    #: the agent's ``strict_model`` setting) — never a fallback.
    strict_model: bool = False
    #: The model connector and model id this run writes with; empty when the
    #: run has no model. Recorded on the run row.
    llm_connector: str = ""
    llm_model: str = ""
    #: True once any model call in this run came back without token counts
    #: (a self-hosted server may leave ``usage`` out). ``usage`` then holds
    #: placeholders, so the runner records the run's tokens as unknown.
    tokens_unknown: bool = False

    # ── Identity ───────────────────────────────────────────────────────────
    @property
    def tenant_id(self) -> str:
        return self.org.id

    @property
    def agent_name(self) -> str:
        return self.record.name

    @property
    def today(self) -> date:
        from app.db.base import utcnow

        return utcnow().date()

    # ── Configuration ──────────────────────────────────────────────────────
    @property
    def scope(self) -> str:
        """The operator's ``Scope / target`` value, empty when unset."""
        return (self.record.scope or "").strip()

    @property
    def autonomous(self) -> bool:
        return bool(self.record.autonomy)

    def setting(self, key: str, default: Any = None) -> Any:  # noqa: ANN401
        return (self.record.settings or {}).get(key, default)

    @property
    def industry(self) -> str:
        """What this customer does, or empty if nobody has said.

        Empty is a legitimate and common answer, and it must stay empty. This
        used to be ``ctx.setting("industry", "automotive retail")`` in three
        agents — reading the *agent's* settings, which nothing ever wrote —
        so every customer's copy, Q&A and outreach was generated for a car
        dealership. A prompt that omits the sector and asks the model to
        infer it from the page in front of it is right far more often than a
        constant is.
        """
        return str((self.org.settings or {}).get("industry") or "").strip()

    @property
    def brand_brief(self) -> str:
        """The workspace's brand profile as a prompt block, or "" when there is none.

        Content agents put this in every prompt so copy sounds like the business.
        """
        from app.services.brand_profile import brief

        return brief(self.org)

    @property
    def brand_profile(self) -> dict:
        from app.services.brand_profile import get

        return get(self.org) or {}

    def set_setting(self, key: str, value: Any) -> None:  # noqa: ANN401
        settings = dict(self.record.settings or {})
        settings[key] = value
        self.record.settings = settings

    @property
    def remaining_actions(self) -> int:
        """How many more actions the daily cap allows."""
        if self.record.actions_today_date != self.today.isoformat():
            return self.record.max_actions_per_day
        return max(0, self.record.max_actions_per_day - self.record.actions_today)

    # ── Connectors ─────────────────────────────────────────────────────────
    def has_connector(self, slug: str) -> bool:
        return slug in self.connected_slugs

    def connector_name(self, slug: str) -> str:
        return self.connector_names.get(slug, slug)

    def connector(self, slug: str) -> BaseConnector:
        """Fetch a connected connector, or raise if it is not available."""
        try:
            return self.connectors[slug]
        except KeyError as exc:
            from app.core.exceptions import ConnectorError

            raise ConnectorError(
                f"{self.connector_name(slug)} is not connected for this workspace"
            ) from exc

    def optional_connector(self, slug: str) -> BaseConnector | None:
        return self.connectors.get(slug)

    def first_connector(self, *slugs: str) -> BaseConnector | None:
        """First of several named connectors that is connected.

        Only for the rare case where the vendor itself is the subject — an ad
        *channel*, for instance, is a specific platform. Everywhere else, ask
        for a capability instead.
        """
        for slug in slugs:
            found = self.connectors.get(slug)
            if found is not None:
                return found
        return None

    # ── Which connectors this agent uses (see app.agents.base.choice) ─────
    def _spec(self):  # noqa: ANN202 - an AgentSpec or None
        from app.agents.base.registry import get_agent

        agent = get_agent(getattr(self.record, "slug", ""))
        return agent.spec if agent else None

    def policy(self, capability: Capability):  # noqa: ANN201 - a ConnectorPolicy
        """What the plan lets this agent use for a capability."""
        from app.services import entitlements

        if self.policies is None:
            self.policies = entitlements.lookup(self.db, self.org)
        return self.policies(getattr(self.record, "slug", ""), capability)

    def _resolution(self, capability: Capability):  # noqa: ANN202
        """``(declaration, Resolution)`` for a capability, or ``(None, None)``."""
        from app.agents.base import choice

        spec = self._spec()
        key, chosen = choice.selection_for(spec, getattr(self.record, "settings", None), capability)
        if key is None:
            return None, None
        resolved = choice.resolve(
            key,
            usable=self.connectors.keys(),
            chosen=chosen,
            policy=self.policy(key.capability),
        )
        return key, resolved

    def _capable(self, capability: Capability) -> list[BaseConnector]:
        """Connected, plan-allowed connectors that can do this, in catalogue order."""
        from app.connectors.base import registry

        policy = self.policy(capability)
        found = []
        for slug in registry.connector_slugs():
            connector = self.connectors.get(slug)
            if connector is not None and connector.supports(capability) and policy.permits(slug):
                found.append(connector)
        return found

    def chosen_connectors(self, capability: Capability) -> list[BaseConnector]:
        """The connectors chosen for a one-connector capability, still usable.

        Empty when nothing is chosen, when every chosen one has been
        disconnected or is not allowed, or when the capability is not one the
        agent picks a single source for — then the automatic rule applies.
        At most one unless the declaration is ``multi``.
        """
        from app.agents.base.contracts import UseMode

        key, resolved = self._resolution(capability)
        if key is None or key.mode is not UseMode.ONE or resolved.automatic:
            return []
        return [
            self.connectors[slug]
            for slug in resolved.order
            if slug in self.connectors and self.connectors[slug].supports(capability)
        ]

    def chosen_connector(self, capability: Capability) -> BaseConnector | None:
        """The (first) chosen connector for this capability, if still usable."""
        chosen = self.chosen_connectors(capability)
        return chosen[0] if chosen else None

    def with_capability(self, capability: Capability) -> BaseConnector | None:
        """The connector this agent uses for a capability.

        The one chosen in the agent's settings when it is still connected;
        otherwise the first connected connector that can do this, in
        catalogue order. Connectors the plan does not allow are skipped.

        This is how agents should find what they need. An agent wants *a CMS*
        or *an ad platform*, not WordPress or Google Ads — so a new
        integration becomes available to every agent that needs its
        capability without any agent changing.
        """
        chosen = self.chosen_connector(capability)
        if chosen is not None:
            return chosen
        capable = self._capable(capability)
        return capable[0] if capable else None

    def candidates(self, capability: Capability) -> list[BaseConnector]:
        """Connectors to try, or use, for a capability the agent uses one of.

        The chosen ones when a choice is in force — the operator said which
        website, so another is never substituted — otherwise every connected
        one in catalogue order, for the agent to take the first that works.
        """
        chosen = self.chosen_connectors(capability)
        return chosen or self._capable(capability)

    def all_with_capability(self, capability: Capability) -> list[BaseConnector]:
        """Every connector this agent uses for a capability, in catalogue order.

        For a capability the agent uses all of (``UseMode.ALL``), only the
        ones selected in its settings when a selection is saved — a platform
        left out is never touched — and every connected one otherwise.
        Elsewhere, every connected one the plan allows.
        """
        from app.agents.base.contracts import UseMode

        key, resolved = self._resolution(capability)
        capable = self._capable(capability)
        if key is None or key.mode is not UseMode.ALL:
            return capable
        keep = set(resolved.order)
        return [c for c in capable if getattr(c, "slug", None) in keep]

    def selected_slugs(self, capability: Capability) -> list[str]:
        """The selection saved for this capability, as saved ([] = none)."""
        _key, resolved = self._resolution(capability)
        return list(resolved.chosen) if resolved is not None else []

    def uses_connector(self, slug: str, capability: Capability) -> bool:
        """Whether this agent works with ``slug`` for ``capability`` on this run."""
        return any(getattr(c, "slug", None) == slug for c in self.all_with_capability(capability))

    def page_sources(self, capability: Capability) -> list[str] | None:
        """Websites whose collected pages this agent works on; ``None`` = all.

        For page agents (``UseMode.PAGE_SOURCE``): the selection is applied
        to ``SeoPage.cms_slug`` as saved, so pages from a website that was
        not chosen are never touched.
        """
        from app.agents.base.contracts import UseMode

        key, resolved = self._resolution(capability)
        if key is None:
            return None
        if key.mode is UseMode.PAGE_SOURCE:
            return None if resolved.every else list(resolved.order)
        if key.mode is UseMode.ONE and key.multi and resolved.chosen:
            # Following a "collect from these websites" choice: as saved.
            return [s for s in resolved.chosen if self.policy(key.capability).permits(s)]
        return None

    # ── Progress ───────────────────────────────────────────────────────────
    def progress(self, step: str, *, done: int = 0, total: int = 0) -> None:
        """Say what this pass is doing now, and how far through it is.

        Deliberately on its own connection. The run's session is inside a
        transaction that is not committed until the pass finishes — which is
        the right behaviour, because a failed pass must not leave half its
        work behind — so anything written there is invisible to the console
        until the pass ends. This writes and commits independently.

        Never allowed to break a run. A progress update failing is worth a
        log line and nothing more; the work itself is what matters.
        """
        if not self.run_id:
            return
        try:
            from app.db.session import session_scope
            from app.models.agent import AgentRun, record_timeline_step

            with session_scope(self.tenant_id) as db:
                run = db.get(AgentRun, self.run_id)
                if run is None:
                    return
                run.step = step[:120]
                run.step_done = done
                run.step_total = total
                # The step on its own is overwritten by the next one; the
                # timeline is what lets somebody see what the pass has done.
                run.detail = record_timeline_step(run.detail, step, done=done, total=total)
        except Exception as exc:  # noqa: BLE001 - reporting must not fail work
            log.debug("Could not record progress for %s: %s", self.record.slug, exc)

    # ── LLM ────────────────────────────────────────────────────────────────
    def account(self, usage: LLMUsage) -> None:
        """Add one call's usage to the run's total.

        Adding usages drops the "not reported" marker (``providers
        .UnreportedUsage``), so it is read here, per call, and kept on the
        context for the runner.
        """
        from app.llm.providers import usage_reported

        if not usage_reported(usage):
            self.tokens_unknown = True
        self.usage = self.usage + usage

    def ask_json(
        self,
        prompt: str,
        *,
        system: str = "",
        schema_hint: str = "",
        max_tokens: int | None = None,
    ) -> Any:  # noqa: ANN401
        """Structured completion, with the run's token spend accumulated."""
        parsed, usage = self.llm.complete_json(
            prompt, system=system, schema_hint=schema_hint, max_tokens=max_tokens
        )
        self.account(usage)
        return parsed

    def ask(self, prompt: str, *, system: str = "", max_tokens: int | None = None) -> str:
        return self.ask_response(prompt, system=system, max_tokens=max_tokens).text

    def ask_response(
        self, prompt: str, *, system: str = "", max_tokens: int | None = None
    ) -> LLMResponse:
        """A completion with its usage, accounted to the run.

        For callers that settle a per-call reservation and parse the text
        themselves, so a reply that fails to parse is still counted.
        """
        response = self.llm.complete(prompt, system=system, max_tokens=max_tokens)
        self.account(response.usage)
        return response

    # ── Logging ────────────────────────────────────────────────────────────
    def log_info(self, message: str, *args: Any) -> None:  # noqa: ANN401
        log.info(f"[{self.record.slug}] {message}", *args)

    def log_warning(self, message: str, *args: Any) -> None:  # noqa: ANN401
        log.warning(f"[{self.record.slug}] {message}", *args)
