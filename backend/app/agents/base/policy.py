"""Autonomy policy — the one place that decides apply-now vs queue-for-review.

Three inputs settle it:

* the **workspace guardrail** chosen during onboarding
  (``full`` / ``hybrid`` / ``human``),
* the **agent's own** autonomy switch, which an operator can flip per card,
* the **action's impact**, which is what makes ``hybrid`` meaningful — agents
  draft freely, and only high-impact changes wait for a person.

The daily action cap is enforced here too, so a misbehaving agent cannot
publish a hundred rewrites while nobody is looking.
"""
from __future__ import annotations

from enum import StrEnum

from app.agents.base.contracts import AgentAction, Impact


class Guardrail(StrEnum):
    FULL = "full"      # agents act and publish independently
    HYBRID = "hybrid"  # agents draft; high-impact changes need approval
    HUMAN = "human"    # every action queues for review


GUARDRAIL_LABELS: dict[str, str] = {
    Guardrail.FULL.value: "Full autonomy — agents act and publish independently",
    Guardrail.HYBRID.value: "Hybrid — agents draft, high-impact changes need approval",
    Guardrail.HUMAN.value: "Human-in-the-loop — every action queues for review",
}

#: The short name, and what it means for the customer's site, kept apart.
#: The labels above are one string doing two jobs, which leaves any UI either
#: printing a whole sentence as a radio label or splitting on an em dash.
GUARDRAIL_NAMES: dict[str, str] = {
    Guardrail.FULL.value: "Full autonomy",
    Guardrail.HYBRID.value: "Hybrid",
    Guardrail.HUMAN.value: "Human in the loop",
}

GUARDRAIL_DETAIL: dict[str, str] = {
    Guardrail.FULL.value: (
        "Agents publish to your site and adjust spend without asking. "
        "Fastest, and the setting that needs the most trust in the setup."
    ),
    Guardrail.HYBRID.value: (
        "Agents handle the routine work themselves and send anything "
        "high-impact — rewriting live copy, moving budget — for approval. "
        "The usual choice."
    ),
    Guardrail.HUMAN.value: (
        "Nothing reaches your site until a person approves it. Every agent "
        "still works and still proposes; the queue is where it lands."
    ),
}


class Decision(StrEnum):
    APPLY = "apply"
    QUEUE = "queue"
    BLOCK = "block"


def resolve_guardrail(raw: str | None) -> Guardrail:
    try:
        return Guardrail(raw or Guardrail.HYBRID.value)
    except ValueError:
        return Guardrail.HYBRID


def decide(
    action: AgentAction,
    *,
    guardrail: Guardrail | str,
    agent_autonomous: bool,
    remaining_actions: int,
) -> tuple[Decision, str]:
    """Return ``(decision, reason)`` for one proposed action."""
    rail = resolve_guardrail(guardrail if isinstance(guardrail, str) else guardrail.value)

    # A per-agent switch set to human-in-the-loop always wins: an operator
    # turning one agent off should not be overridden by a permissive default.
    if not agent_autonomous:
        return Decision.QUEUE, "agent is set to human-in-the-loop"

    if rail is Guardrail.HUMAN:
        return Decision.QUEUE, "workspace guardrail requires review of every action"

    if rail is Guardrail.HYBRID and action.impact is Impact.HIGH:
        return Decision.QUEUE, "high-impact change under the hybrid guardrail"

    if remaining_actions <= 0:
        # Not queued: the cap is about rate, and the work will be re-proposed
        # on the next run rather than piling up in someone's queue.
        return Decision.BLOCK, "daily autonomous action limit reached"

    return Decision.APPLY, "within autonomy policy"


def autonomy_label(agent_autonomous: bool) -> str:
    return "Autonomous" if agent_autonomous else "Human-in-loop"
