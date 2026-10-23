"""Agent registry.

Agents are discovered by importing each package under ``app/agents/`` and
reading its ``AGENT`` export. Nothing has to be listed twice: adding a folder
with an ``agent.py`` that exports ``AGENT`` adds an agent to the fleet, and the
provisioning service picks it up for every new organisation.
"""
from __future__ import annotations

import importlib
import pkgutil
from pathlib import Path

from app.agents.base.contracts import AgentCategory, BaseAgent
from app.core.logging import get_logger

log = get_logger(__name__)

# Fleet order, which is also the order the cards appear in the Agents hub.
# Listed explicitly so the console has a stable, meaningful sequence — the
# SEO/AEO pipeline, then off-page authority, then the paid loop.
FLEET_ORDER: tuple[str, ...] = (
    "on_page_seo_sync",
    "technical_seo_auditor",
"technical_fix_applier",
    "content_refresh",
    "internal_linking",
    "topic_cluster_planner",
    "aeo_qa_injector",
    "ai_visibility_tracker",
    "knowledge_graph_schema",
    "referral_spam_guard",
    "backlink_node_discovery",
    "digital_pr_outreach",
    "competitor_link_monitor",
    "dynamic_creative_optimizer",
    "predictive_budget_engine",
    "first_party_audience_modeler",
    "click_fraud_controller",
    "local_search_tracker",
    "review_responder",
)

_registry: dict[str, BaseAgent] = {}
_loaded = False


def _discover() -> None:
    global _loaded
    if _loaded:
        return

    package_dir = Path(__file__).resolve().parent.parent
    for module_info in pkgutil.iter_modules([str(package_dir)]):
        if not module_info.ispkg or module_info.name == "base":
            continue
        module_name = f"app.agents.{module_info.name}"
        try:
            module = importlib.import_module(module_name)
        except Exception as exc:  # noqa: BLE001 - one bad agent must not
            # take the whole fleet (and the API) down with it.
            log.error("Could not import agent package %s: %s", module_name, exc)
            continue

        agent = getattr(module, "AGENT", None)
        if agent is None:
            log.warning("%s exports no AGENT; skipping", module_name)
            continue
        if not isinstance(agent, BaseAgent):
            log.error("%s.AGENT is not a BaseAgent; skipping", module_name)
            continue
        if agent.spec.slug != module_info.name:
            log.error(
                "Agent slug %r does not match its package %r; skipping",
                agent.spec.slug,
                module_info.name,
            )
            continue
        _registry[agent.spec.slug] = agent

    _loaded = True
    log.info("Registered %d agents: %s", len(_registry), ", ".join(sorted(_registry)))


def all_agents() -> list[BaseAgent]:
    """Every fleet agent, in fleet order (unlisted ones last).

    On-demand agents (``AgentSpec.on_demand``) are left out: they run only
    when a job asks for them, so they are never installed in a workspace,
    scheduled or shown as a card. ``get_agent`` still finds them.
    """
    _discover()
    fleet = {slug: a for slug, a in _registry.items() if not a.spec.on_demand}
    ordered = [fleet[slug] for slug in FLEET_ORDER if slug in fleet]
    extra = sorted(
        (a for slug, a in fleet.items() if slug not in FLEET_ORDER),
        key=lambda a: a.spec.name,
    )
    return ordered + extra


def on_demand_agents() -> list[BaseAgent]:
    """Registered on-demand agents, by name."""
    _discover()
    return sorted(
        (a for a in _registry.values() if a.spec.on_demand), key=lambda a: a.spec.name
    )


def get_agent(slug: str) -> BaseAgent | None:
    _discover()
    return _registry.get(slug)


def agent_slugs() -> list[str]:
    return [agent.spec.slug for agent in all_agents()]


def agents_in_category(category: AgentCategory) -> list[BaseAgent]:
    return [agent for agent in all_agents() if agent.spec.category is category]


def reset_registry() -> None:
    """Force rediscovery — used by tests."""
    global _loaded
    _registry.clear()
    _loaded = False
