"""Budget allocation maths.

Deliberately not a model call. Moving real money on an hourly loop needs an
allocation that is deterministic, auditable and bounded — you have to be able
to explain in a review why 4% moved from LinkedIn to Google, and get the same
answer twice from the same inputs.

The policy is inverse-CAC weighting with three safety rails:

* **A per-pass movement cap**, so one bad hour of data cannot swing the account.
* **A per-channel floor** on any channel with live spend, so a temporarily
  expensive channel is throttled rather than switched off — a channel starved to
  zero stops producing the conversion data needed to ever recover.
* **Locked channels are untouchable**, and the remaining share is allocated
  around them.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.core.money import money

# No channel moves more than this many points in one pass.
MAX_SHIFT_POINTS = 8
# A channel with measured spend keeps at least this share.
MIN_ACTIVE_SHARE = 5
# CAC below this is treated as unmeasured rather than free.
MIN_CREDIBLE_CAC = 0.01
# A channel needs this many conversions before its CAC is trusted.
MIN_CONVERSIONS_FOR_CONFIDENCE = 5


@dataclass(slots=True)
class ChannelState:
    channel: str
    percent: int
    cac: float
    spend: float = 0.0
    conversions: int = 0
    locked: bool = False

    @property
    def is_active(self) -> bool:
        return self.spend > 0 or self.percent > 0

    @property
    def cac_is_credible(self) -> bool:
        return self.cac >= MIN_CREDIBLE_CAC and self.conversions >= MIN_CONVERSIONS_FOR_CONFIDENCE


@dataclass(slots=True)
class Allocation:
    """The proposed split, plus what it would change."""

    percents: dict[str, int]
    shifts: dict[str, int]
    rationale: str

    @property
    def total(self) -> int:
        return sum(self.percents.values())

    @property
    def moved_points(self) -> int:
        return sum(abs(v) for v in self.shifts.values()) // 2

    @property
    def is_meaningful(self) -> bool:
        """Below two points of movement, rebalancing is churn."""
        return self.moved_points >= 2


def _baseline(states: list[ChannelState]) -> dict[str, int]:
    """Current shares, normalised to sum to 100.

    Stored percentages can drift from 100 — a channel added after setup, a
    manual edit, a partial write. Capping movement against an un-normalised
    baseline would let the repair step below move a channel far further than
    ``MAX_SHIFT_POINTS``, which is the one thing the cap exists to prevent.
    So the baseline is fixed first, and the cap is applied against that.
    """
    total = sum(s.percent for s in states)
    if total == 100:
        return {s.channel: s.percent for s in states}

    if total <= 0:
        even = 100 // len(states)
        baseline = {s.channel: even for s in states}
    else:
        baseline = {s.channel: round(s.percent * 100 / total) for s in states}

    # Absorb the rounding remainder on the largest share.
    drift = 100 - sum(baseline.values())
    if drift and baseline:
        largest = max(baseline, key=lambda c: baseline[c])
        baseline[largest] = max(0, baseline[largest] + drift)
    return baseline


def allocate(states: list[ChannelState]) -> Allocation:
    """Compute a bounded inverse-CAC allocation."""
    if not states:
        return Allocation({}, {}, "No channels configured")

    # Work from a normalised baseline so the movement cap is meaningful.
    # `shifts` are reported against that baseline, because a normalisation is
    # a correction rather than a reallocation decision — "shifted 4% from
    # LinkedIn to Google" has to describe what the engine actually chose.
    baseline = _baseline(states)
    states = [
        ChannelState(
            channel=s.channel,
            percent=baseline[s.channel],
            cac=s.cac,
            spend=s.spend,
            conversions=s.conversions,
            locked=s.locked,
        )
        for s in states
    ]

    locked = [s for s in states if s.locked]
    movable = [s for s in states if not s.locked]
    locked_share = sum(s.percent for s in locked)
    available = max(0, 100 - locked_share)

    if not movable or available == 0:
        percents = {s.channel: s.percent for s in states}
        return Allocation(
            percents, {s.channel: 0 for s in states}, "Every channel is locked"
        )

    # Only channels with a credible CAC take part in the weighting. Channels
    # without one hold their current share: reallocating on the strength of
    # two conversions is noise, not signal.
    weighted = [s for s in movable if s.cac_is_credible]
    unweighted = [s for s in movable if not s.cac_is_credible]

    if not weighted:
        percents = {s.channel: s.percent for s in states}
        return Allocation(
            percents,
            {s.channel: 0 for s in states},
            "Not enough conversion data yet to reallocate with confidence",
        )

    held = sum(s.percent for s in unweighted)
    pool = max(0, available - held)

    inverse = {s.channel: 1.0 / s.cac for s in weighted}
    total_inverse = sum(inverse.values())
    raw = {
        s.channel: (inverse[s.channel] / total_inverse) * pool for s in weighted
    }

    # Apply the movement cap and the floor, then repair the rounding drift.
    proposed: dict[str, int] = {}
    for s in weighted:
        target = raw[s.channel]
        delta = max(-MAX_SHIFT_POINTS, min(MAX_SHIFT_POINTS, target - s.percent))
        value = round(s.percent + delta)
        if s.is_active:
            value = max(MIN_ACTIVE_SHARE, value)
        proposed[s.channel] = max(0, value)

    for s in unweighted:
        proposed[s.channel] = s.percent
    for s in locked:
        proposed[s.channel] = s.percent

    _repair_total(proposed, weighted)

    shifts = {s.channel: proposed[s.channel] - baseline[s.channel] for s in states}
    best = min(weighted, key=lambda s: s.cac)
    worst = max(weighted, key=lambda s: s.cac)
    rationale = (
        f"Weighted by inverse CAC across {len(weighted)} channels with credible data. "
        f"{best.channel} has the lowest CAC ({money(best.cac, decimals=2)}) and gains share; "
        f"{worst.channel} ({money(worst.cac, decimals=2)}) gives it up. "
        f"Movement capped at {MAX_SHIFT_POINTS} points per pass"
        + (f", {len(locked)} channel(s) locked" if locked else "")
        + (
            f", {len(unweighted)} held for lack of conversion data" if unweighted else ""
        )
        + "."
    )
    return Allocation(proposed, shifts, rationale)


def _repair_total(proposed: dict[str, int], adjustable: list[ChannelState]) -> None:
    """Force the split to exactly 100, absorbing drift on the movable channels.

    Rounding and the floor can leave the total at 99 or 102; the console shows
    an allocation total, so it has to land on 100 exactly.
    """
    if not adjustable:
        return
    order = sorted(adjustable, key=lambda s: s.cac)
    guard = 0

    while sum(proposed.values()) != 100 and guard < 1000:
        guard += 1
        drift = 100 - sum(proposed.values())
        if drift > 0:
            # Give the surplus to the cheapest channel first.
            proposed[order[0].channel] += 1
        else:
            # Take it from the most expensive channel that can afford it.
            for state in reversed(order):
                if proposed[state.channel] > MIN_ACTIVE_SHARE:
                    proposed[state.channel] -= 1
                    break
            else:
                proposed[order[-1].channel] = max(0, proposed[order[-1].channel] - 1)


def blended_cac(states: list[ChannelState]) -> float:
    spend = sum(s.spend for s in states)
    conversions = sum(s.conversions for s in states)
    return round(spend / conversions, 2) if conversions else 0.0


def projected_cac(states: list[ChannelState], percents: dict[str, int]) -> float:
    """Blended CAC the proposed split would produce at the same total spend.

    This is what justifies the change: if the projection is not better than
    the current blend, there is no reason to move money.
    """
    credible = [s for s in states if s.cac_is_credible]
    if not credible:
        return 0.0
    total_share = sum(percents.get(s.channel, 0) for s in credible)
    if total_share == 0:
        return 0.0
    weighted = sum(percents.get(s.channel, 0) * s.cac for s in credible)
    return round(weighted / total_share, 2)
