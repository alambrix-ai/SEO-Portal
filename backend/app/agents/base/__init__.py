"""Shared agent framework: contracts, run context, registry, autonomy policy."""
from app.agents.base.context import AgentContext
from app.agents.base.contracts import (
    AgentAction,
    AgentCategory,
    AgentResult,
    AgentSpec,
    BaseAgent,
    Impact,
)
from app.agents.base.policy import Decision, Guardrail, decide, resolve_guardrail
from app.agents.base.registry import all_agents, agent_slugs, get_agent

__all__ = [
    "AgentAction",
    "AgentCategory",
    "AgentContext",
    "AgentResult",
    "AgentSpec",
    "BaseAgent",
    "Decision",
    "Guardrail",
    "Impact",
    "agent_slugs",
    "all_agents",
    "decide",
    "get_agent",
    "resolve_guardrail",
]
