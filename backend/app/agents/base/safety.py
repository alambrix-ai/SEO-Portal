"""Spending-agent safety rules: what an action costs, and the limits on it.

Pure logic, no database. The runner (``app.orchestration.runner``) builds a
:class:`SafetyGate` from the workspace's settings for each run, and this
module answers three questions about every action a spending agent proposes:

* **What would it change?** :func:`measure` reads the agent's own payload —
  the budget points and amount a rebalance moves, or one campaign, placement,
  creative or audience — so the limits are checked against the numbers the
  agent computed, not a guess.
* **Is it inside the workspace's limits?** :func:`cap_reason` compares it with
  :class:`Caps` and what was already changed automatically today
  (:class:`Ledger`).
* **Does its write go through a connector nobody has proven yet?**
  :func:`unverified_reason` words the hold.

An action over a limit, or one that writes through an unverified connector,
is **never applied automatically**: it is queued for a person with the
reason. Nothing is dropped, because two of these agents (Dynamic Creative
Optimizer and Search Campaign Builder) save their drafts during the run and
do not propose the same draft again — a dropped proposal would be lost work.
"""
from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field, fields
from typing import Any

from app.connectors.base.connector import Capability

# ── The spending agents ────────────────────────────────────────────────────
#: Agents that can spend money or change what an ad account does, and the
#: capability each one writes through. Practice mode, the limits and the
#: unverified-connector rule apply to these.
SPENDING_AGENTS: dict[str, Capability] = {
    "predictive_budget_engine": Capability.WRITE_AD_BUDGET,
    "click_fraud_controller": Capability.BLOCK_PLACEMENT,
    "dynamic_creative_optimizer": Capability.UPLOAD_CREATIVE,
    "search_campaign_builder": Capability.CREATE_SEARCH_CAMPAIGN,
    "first_party_audience_modeler": Capability.PUSH_AUDIENCE,
}

#: Agent setting holding the per-agent practice switch.
PRACTICE_SETTING = "practice_mode"


def is_spending_agent(slug: str) -> bool:
    return slug in SPENDING_AGENTS


def agent_practice_on(slug: str, settings: dict | None) -> bool:
    """The agent's own practice switch.

    A record without the setting keeps the behaviour it had before practice
    mode existed (off). New configurations of a spending agent are saved
    with it on — see ``app.services.agent_safety.default_practice``.
    """
    if not is_spending_agent(slug):
        return False
    return bool((settings or {}).get(PRACTICE_SETTING, False))


# ── Limits ─────────────────────────────────────────────────────────────────
@dataclass(slots=True)
class Caps:
    """Per-workspace limits on what spending agents may do automatically.

    Defaults, and why:

    * ``max_budget_change_pct`` 20 — the budget engine moves at most 8 points
      per channel per pass, so ordinary rebalances (typically 2–16 points)
      go through and only an unusual swing waits for a person.
    * ``max_budget_moved_per_day`` 5,000 — in the workspace's currency. Big
      enough that a small account's hourly rebalances are not all held,
      small enough that a runaway engine cannot move a large account's
      daily budget without someone looking. Set it to your own scale.
    * ``max_campaigns_per_day`` 2 — the campaign builder runs weekly and
      writes one campaign per platform, so two covers a normal run.
      Campaigns are created paused, but each one is still a new campaign in
      the account.
    * ``max_placements_blocked_per_day`` 100 — blocking stops spend rather
      than commits it, but a detector misfire could exclude real traffic
      sources by the hundred.
    * ``max_creatives_uploaded_per_day`` 20 — the creative optimizer writes
      up to five pages' worth of variants a run; twenty a day is a busy day.

    0 means "never automatically": every such change waits for a person.
    """

    max_budget_change_pct: int = 20
    max_budget_moved_per_day: float = 5000.0
    max_campaigns_per_day: int = 2
    max_placements_blocked_per_day: int = 100
    max_creatives_uploaded_per_day: int = 20

    @classmethod
    def from_dict(cls, raw: dict | None) -> Caps:
        """Saved values over the defaults; anything unusable keeps the default."""
        caps = cls()
        for spec in fields(cls):
            value = (raw or {}).get(spec.name)
            if value is None or isinstance(value, bool):
                continue
            try:
                number = float(value)
            except (TypeError, ValueError):
                continue
            if number < 0 or number != number:  # negative or NaN
                continue
            setattr(caps, spec.name, int(number) if spec.type in (int, "int") else round(number, 2))
        return caps

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


#: What each limit means, for the settings page.
CAP_LABELS: dict[str, tuple[str, str]] = {
    "max_budget_change_pct": (
        "Largest budget change at once (%)",
        "The share of your total ad budget one change may move between platforms.",
    ),
    "max_budget_moved_per_day": (
        "Most budget moved in a day",
        "In your account's currency, adding up every automatic budget change that day.",
    ),
    "max_campaigns_per_day": (
        "New campaigns a day",
        "Campaigns created in your ad accounts automatically in one day.",
    ),
    "max_placements_blocked_per_day": (
        "Placements blocked a day",
        "Websites and apps excluded from your ads automatically in one day.",
    ),
    "max_creatives_uploaded_per_day": (
        "Ads uploaded a day",
        "New ad copy and images sent to your ad accounts automatically in one day.",
    ),
}


# ── What an action changes ─────────────────────────────────────────────────
@dataclass(slots=True)
class Measure:
    """What one action changes, in the units the limits use."""

    #: Percentage points of the total budget moved between channels.
    budget_points: float = 0.0
    #: The daily budget that changes channel, in the workspace currency.
    #: None when the agent could not price it (no spend measured yet).
    budget_moved: float | None = None
    campaigns: int = 0
    placements: int = 0
    creatives: int = 0
    audiences: int = 0

    @property
    def empty(self) -> bool:
        return not (
            self.budget_points
            or self.budget_moved
            or self.campaigns
            or self.placements
            or self.creatives
            or self.audiences
        )

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v}


def _number(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return number if number == number else None


def measure(action: Any) -> Measure:  # noqa: ANN401 - an AgentAction
    """What ``action`` changes, read from the payload its agent wrote.

    Field names are the agents' own:

    * Predictive Budget Engine ``rebalance_budget``: ``shifts`` ({channel:
      points}, positive and negative summing to zero), ``moved_points`` and
      ``budget_moved_per_day`` (added for this check).
    * Click-Fraud Controller ``block_placement``: one placement.
    * Dynamic Creative Optimizer ``publish_creative``: one creative.
    * Search Campaign Builder ``create_search_campaign``: one campaign,
      unless ``export_only`` (a download file creates nothing anywhere).
    * First-Party Audience Modeler ``activate_audience``: one audience per
      platform in ``platforms``.
    """
    payload = getattr(action, "payload", None) or {}
    kind = getattr(action, "kind", "")
    out = Measure()
    if kind == "rebalance_budget":
        points = _number(payload.get("moved_points"))
        if points is None:
            shifts = payload.get("shifts") or {}
            moved = sum(abs(_number(v) or 0.0) for v in shifts.values())
            points = moved / 2
        out.budget_points = round(points, 2)
        amount = _number(payload.get("budget_moved_per_day"))
        out.budget_moved = round(amount, 2) if amount is not None else None
    elif kind == "block_placement":
        out.placements = 1
    elif kind == "publish_creative":
        out.creatives = 1
    elif kind == "create_search_campaign":
        out.campaigns = 0 if payload.get("export_only") else 1
    elif kind == "activate_audience":
        out.audiences = max(1, len(payload.get("platforms") or []))
    return out


@dataclass(slots=True)
class Ledger:
    """What spending agents already changed automatically today."""

    budget_moved: float = 0.0
    campaigns: int = 0
    placements: int = 0
    creatives: int = 0

    def add(self, m: Measure) -> None:
        self.budget_moved += m.budget_moved or 0.0
        self.campaigns += m.campaigns
        self.placements += m.placements
        self.creatives += m.creatives

    @classmethod
    def from_measures(cls, measures: Iterable[dict]) -> Ledger:
        ledger = cls()
        for raw in measures:
            if not isinstance(raw, dict):
                continue
            ledger.budget_moved += _number(raw.get("budget_moved")) or 0.0
            ledger.campaigns += int(_number(raw.get("campaigns")) or 0)
            ledger.placements += int(_number(raw.get("placements")) or 0)
            ledger.creatives += int(_number(raw.get("creatives")) or 0)
        return ledger


def _amount(value: float, money: Callable[[float], str] | None) -> str:
    if money is not None:
        return money(value)
    return f"{value:,.2f}".rstrip("0").rstrip(".")


def _points(value: float) -> str:
    return f"{value:g}"


def cap_reason(
    m: Measure,
    caps: Caps,
    ledger: Ledger,
    *,
    money: Callable[[float], str] | None = None,
) -> str:
    """Why this action is over a limit, in plain words, or "" when it is not."""
    if m.budget_points and m.budget_points > caps.max_budget_change_pct:
        return (
            f"Moves {_points(m.budget_points)}% of budget; your limit is "
            f"{_points(caps.max_budget_change_pct)}% per change."
        )
    if m.budget_moved:
        after = ledger.budget_moved + m.budget_moved
        if after > caps.max_budget_moved_per_day:
            already = (
                f" on top of {_amount(ledger.budget_moved, money)} already moved today"
                if ledger.budget_moved
                else ""
            )
            return (
                f"Moves {_amount(m.budget_moved, money)} of daily budget{already}; your "
                f"limit is {_amount(caps.max_budget_moved_per_day, money)} a day."
            )
    counted = (
        (m.campaigns, ledger.campaigns, caps.max_campaigns_per_day, "new campaign", "new campaigns"),
        (m.placements, ledger.placements, caps.max_placements_blocked_per_day, "blocked placement", "placements blocked"),
        (m.creatives, ledger.creatives, caps.max_creatives_uploaded_per_day, "uploaded ad", "ads uploaded"),
    )
    for wanted, done, limit, one, many in counted:
        if wanted and done + wanted > limit:
            if limit == 0:
                return f"Your limit is 0 {many} a day, so each one waits for your approval."
            return (
                f"Would be {one} number {done + wanted} today; your limit is "
                f"{limit} {many} a day."
            )
    return ""


def unverified_reason(names: Iterable[str]) -> str:
    """The hold for writes through connectors nobody has proven yet."""
    unique = list(dict.fromkeys(n for n in names if n))
    if not unique:
        return ""
    if len(unique) == 1:
        subject, verb = unique[0], "hasn't"
    else:
        subject, verb = ", ".join(unique[:-1]) + f" and {unique[-1]}", "haven't"
    return (
        f"{subject} {verb} been confirmed against a live account yet, so changes "
        "wait for your approval."
    )


def automatic_note(names: Iterable[str]) -> str:
    """The setup screen's line next to the autonomy switch."""
    unique = list(dict.fromkeys(n for n in names if n))
    if not unique:
        return ""
    subject = unique[0] if len(unique) == 1 else ", ".join(unique[:-1]) + f" and {unique[-1]}"
    verb = "is" if len(unique) == 1 else "are"
    return f"Automatic changes switch on once {subject} {verb} verified."


# ── The gate the runner consults ───────────────────────────────────────────
@dataclass(slots=True)
class SafetyGate:
    """Everything the runner needs to apply the safety rules to one run.

    Built by ``app.services.agent_safety.gate_for`` from the database; built
    by hand in tests. ``halted`` is called before every apply, so a stop
    pressed while a run is in progress takes effect on its next action.
    """

    #: Where practice mode comes from: "platform", "workspace", "agent" or
    #: "" when it is off for this run.
    practice: str = ""
    caps: Caps = field(default_factory=Caps)
    ledger: Ledger = field(default_factory=Ledger)
    #: Applies to this agent (a spending agent). The rules are skipped for
    #: every other agent.
    spending: bool = False
    is_verified: Callable[[str], bool] = lambda slug: True
    connector_name: Callable[[str], str] = lambda slug: slug
    #: The stop in force ("" when none). Re-read on every call.
    halted: Callable[[], str] = lambda: ""
    money: Callable[[float], str] | None = None
    #: Practice-only ledger, so a practice run's "would have" respects the
    #: limits without counting as real changes.
    practice_ledger: Ledger = field(default_factory=Ledger)


@dataclass(slots=True)
class Route:
    """What the gate says about one action, before the policy decides."""

    measure: Measure
    #: Plain-language holds from the safety rules: unverified connector and/or
    #: over a limit. Each one stops automatic application.
    notes: list[str]
    unverified: list[str]
    over_cap: str


def assess(action: Any, gate: SafetyGate) -> Route:  # noqa: ANN401 - an AgentAction
    """Measure ``action`` and collect the safety holds that apply to it."""
    m = measure(action)
    if not gate.spending:
        return Route(measure=m, notes=[], unverified=[], over_cap="")
    slugs = list(getattr(action, "writes_via", ()) or ())
    unverified = [gate.connector_name(slug) for slug in slugs if not gate.is_verified(slug)]
    ledger = gate.practice_ledger if gate.practice else gate.ledger
    over = cap_reason(m, gate.caps, ledger, money=gate.money)
    notes = [note for note in (unverified_reason(unverified), over) if note]
    return Route(measure=m, notes=notes, unverified=unverified, over_cap=over)


def practice_label(source: str) -> str:
    """Why practice mode is on, for the run summary and the console."""
    return {
        "platform": "Practice mode is on for every workspace (set by the platform team)",
        "workspace": "Practice mode is on for all spending agents in this workspace",
        "agent": "This agent is in practice mode",
    }.get(source, "")
