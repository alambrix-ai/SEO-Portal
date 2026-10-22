"""Autonomy policy — the one place that decides apply-now vs queue-for-review.

Three inputs settle it:

* the **workspace guardrail** chosen during onboarding
  (``full`` / ``hybrid`` / ``human``), shown to people as "Review before
  publishing" (see :data:`GUARDRAIL_NAMES`),
* the **agent's own** autonomy switch, which an operator can flip per card,
* the **action's impact**, which is what makes ``hybrid`` meaningful — agents
  draft freely, and only high-impact changes wait for a person.

The daily action cap is enforced here too, so a misbehaving agent cannot
publish a hundred rewrites while nobody is looking.
"""
from __future__ import annotations

from collections.abc import Sequence
from enum import StrEnum

from app.agents.base.contracts import AgentAction, Impact


class Guardrail(StrEnum):
    FULL = "full"      # agents act and publish independently
    HYBRID = "hybrid"  # agents draft; high-impact changes need approval
    HUMAN = "human"    # every action queues for review


#: What the setting is called wherever a person sees it. The stored values
#: (``full`` / ``hybrid`` / ``human``) are unchanged; only the words are.
REVIEW_SETTING_NAME = "Review before publishing"

#: Ordered most careful first, which is the order the choices are offered in.
GUARDRAIL_LABELS: dict[str, str] = {
    Guardrail.HUMAN.value: "Always ask me: every action queues until you approve it",
    Guardrail.HYBRID.value: (
        "Ask me for big changes: agents publish small changes and ask you about big ones"
    ),
    Guardrail.FULL.value: "Publish automatically: agents publish changes without asking you",
}

#: The short name, and what it means for the customer's site, kept apart.
#: The labels above are one string doing two jobs, which leaves any UI either
#: printing a whole sentence as a radio label or splitting on a separator.
GUARDRAIL_NAMES: dict[str, str] = {
    Guardrail.FULL.value: "Publish automatically",
    Guardrail.HYBRID.value: "Ask me for big changes",
    Guardrail.HUMAN.value: "Always ask me",
}

GUARDRAIL_DETAIL: dict[str, str] = {
    Guardrail.FULL.value: (
        "Agents publish to your site and change ad spend without asking you. "
        "Fastest, but nobody checks a change before it goes live."
    ),
    Guardrail.HYBRID.value: (
        "Agents publish small changes themselves and ask you before big ones, "
        "such as rewriting live pages or moving ad budget. The usual choice."
    ),
    Guardrail.HUMAN.value: (
        "Nothing goes live until you approve it. Agents still do the work and "
        "send every change to your Approvals list."
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
    holds: Sequence[str] = (),
) -> tuple[Decision, str]:
    """Return ``(decision, reason)`` for one proposed action.

    ``holds`` are the spending-agent safety rules that apply to this action
    (``app.agents.base.safety.assess``): a write through a connector not yet
    verified against a live account, or a change over the workspace's
    limits. Any hold means the action is **never applied automatically**,
    whatever the guardrail or the agent's switch — it is queued for a person,
    with the hold as the reason.
    """
    rail = resolve_guardrail(guardrail if isinstance(guardrail, str) else guardrail.value)

    # A per-agent switch set to human-in-the-loop always wins: an operator
    # turning one agent off should not be overridden by a permissive default.
    if not agent_autonomous:
        return Decision.QUEUE, "agent is set to human-in-the-loop"

    if rail is Guardrail.HUMAN:
        return Decision.QUEUE, "workspace guardrail requires review of every action"

    if rail is Guardrail.HYBRID and action.impact is Impact.HIGH:
        return Decision.QUEUE, "high-impact change under the hybrid guardrail"

    held = [hold for hold in holds if hold]
    if held:
        # Queued, not blocked: these are about whether a person should look,
        # not about rate, and two of the spending agents do not propose the
        # same draft twice — a blocked one would be lost.
        return Decision.QUEUE, " ".join(held)

    if remaining_actions <= 0:
        # Not queued: the cap is about rate, and the work will be re-proposed
        # on the next run rather than piling up in someone's queue.
        return Decision.BLOCK, "daily autonomous action limit reached"

    return Decision.APPLY, "within autonomy policy"


def autonomy_label(agent_autonomous: bool) -> str:
    return "Autonomous" if agent_autonomous else "Human-in-loop"
