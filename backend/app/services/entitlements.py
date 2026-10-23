"""What a workspace's plan lets each agent use. The single place to decide it.

Every "which integrations may this agent use, and how many" question goes
through :func:`connector_policy`. That covers:

* the setup screen's options (a disallowed one is sent ``locked`` with the
  reason, so the screen can say "Available on Pro" without a frontend change);
* validation of a saved choice (``agent_requirements.clean_choices``);
* the run itself (``AgentContext`` treats a disallowed connector as absent).

Today nothing is restricted. Plans will be applied here, on top of the
portal feature flags (``portal_features``), which still decide whether an
integration exists in this portal at all.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from app.connectors.base.connector import Capability


@dataclass(frozen=True, slots=True)
class ConnectorPolicy:
    """Limits on the connectors one agent may use for one capability."""

    #: Connector slugs the plan allows; ``None`` means no restriction.
    allowed: frozenset[str] | None = None
    #: The most that may be selected (or used at once); ``None`` means no limit.
    max_selected: int | None = None
    #: Shown next to a locked option or a refused save, e.g. "Available on Pro".
    reason: str = ""

    def permits(self, slug: str) -> bool:
        return self.allowed is None or slug in self.allowed

    @property
    def locked_reason(self) -> str:
        return self.reason or "Not included in your plan."


UNRESTRICTED = ConnectorPolicy()

#: ``(agent slug, capability) -> policy`` for one workspace.
PolicyLookup = Callable[[str, Capability], ConnectorPolicy]


def connector_policy(
    db: Any,  # noqa: ANN401 - a Session; unused until plans exist
    org: Any,  # noqa: ANN401 - an Organization
    agent_slug: str,
    capability: Capability,
) -> ConnectorPolicy:
    """The plan's limits for this agent and capability. Unrestricted today."""
    return UNRESTRICTED


def lookup(db: Any, org: Any) -> PolicyLookup:  # noqa: ANN401
    """A cached ``(agent, capability) -> policy`` for one workspace and request."""
    cache: dict[tuple[str, Capability], ConnectorPolicy] = {}

    def policy(agent_slug: str, capability: Capability) -> ConnectorPolicy:
        key = (agent_slug, capability)
        if key not in cache:
            # Looked up through the module so a test (or a plan) can replace it.
            from app.services import entitlements

            cache[key] = entitlements.connector_policy(db, org, agent_slug, capability)
        return cache[key]

    return policy


def unrestricted(agent_slug: str, capability: Capability) -> ConnectorPolicy:  # noqa: ARG001
    return UNRESTRICTED


@dataclass(frozen=True, slots=True)
class FeaturePolicy:
    """Whether a plan includes a feature, and how often it may be used."""

    allowed: bool = True
    #: Uses per day; ``None`` means the platform default applies.
    max_per_day: int | None = None
    #: For history features (``"audit_history"``): how many past snapshots
    #: are shown; ``None`` means all that are kept.
    max_history: int | None = None
    reason: str = ""


def feature_policy(db: Any, org: Any, feature: str) -> FeaturePolicy:  # noqa: ANN401
    """The plan's limits on a feature such as ``"site_analysis"``.

    Reads synced ``plan_entitlements`` (from the billing catalog in config).
    When no billing row exists yet, remains unrestricted so existing workspaces
    are not locked out mid-rollout.
    """
    if db is None or org is None:
        return FeaturePolicy()
    try:
        from app.services import billing

        spec = billing.resolved_feature(db, org, feature)
    except Exception:
        return FeaturePolicy()
    if not spec:
        return FeaturePolicy()
    allowed = bool(spec.get("allowed", True))
    max_per_day = spec.get("max_per_day")
    max_history = spec.get("max_history")
    reason = "" if allowed else "Not included in your plan."
    return FeaturePolicy(
        allowed=allowed,
        max_per_day=int(max_per_day) if max_per_day is not None else None,
        max_history=int(max_history) if max_history is not None else None,
        reason=reason,
    )
