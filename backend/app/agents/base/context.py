"""Everything an agent is handed for one run.

The context is the agent's only door to the outside: the database session, its
own configuration row, the organisation's cipher, the LLM, and the connectors
it declared. Keeping it narrow is what makes agents testable — a fake context
with two connectors and the mock provider exercises a real agent end to end.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import TYPE_CHECKING, Any

from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.connectors.base.connector import Capability
from app.llm.base import LLMProvider, LLMUsage
from app.models.agent import AgentRecord
from app.models.workspace import Organization
from app.services.encryption import OrgCipher

if TYPE_CHECKING:  # pragma: no cover
    from app.connectors.base.connector import BaseConnector

log = get_logger(__name__)


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

    def with_capability(self, capability: Capability) -> BaseConnector | None:
        """The first connected connector that can do this, in catalogue order.

        This is how agents should find what they need. An agent wants *a CMS*
        or *an ad platform*, not WordPress or Google Ads — so a new
        integration becomes available to every agent that needs its
        capability without any agent changing.
        """
        from app.connectors.base import registry

        for slug in registry.connector_slugs():
            connector = self.connectors.get(slug)
            if connector is not None and connector.supports(capability):
                return connector
        return None

    def all_with_capability(self, capability: Capability) -> list[BaseConnector]:
        """Every connected connector that can do this, in catalogue order."""
        from app.connectors.base import registry

        found = []
        for slug in registry.connector_slugs():
            connector = self.connectors.get(slug)
            if connector is not None and connector.supports(capability):
                found.append(connector)
        return found

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
            from sqlalchemy import update

            from app.db.session import session_scope
            from app.models.agent import AgentRun

            with session_scope(self.tenant_id) as db:
                db.execute(
                    update(AgentRun)
                    .where(AgentRun.id == self.run_id)
                    .values(step=step[:120], step_done=done, step_total=total)
                )
        except Exception as exc:  # noqa: BLE001 - reporting must not fail work
            log.debug("Could not record progress for %s: %s", self.record.slug, exc)

    # ── LLM ────────────────────────────────────────────────────────────────
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
        self.usage = self.usage + usage
        return parsed

    def ask(self, prompt: str, *, system: str = "", max_tokens: int | None = None) -> str:
        response = self.llm.complete(prompt, system=system, max_tokens=max_tokens)
        self.usage = self.usage + response.usage
        return response.text

    # ── Logging ────────────────────────────────────────────────────────────
    def log_info(self, message: str, *args: Any) -> None:  # noqa: ANN401
        log.info(f"[{self.record.slug}] {message}", *args)

    def log_warning(self, message: str, *args: Any) -> None:  # noqa: ANN401
        log.warning(f"[{self.record.slug}] {message}", *args)
