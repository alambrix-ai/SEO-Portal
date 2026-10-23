"""Blended-ROAS budget allocation across ad channels with daily move caps."""
from __future__ import annotations

from dataclasses import dataclass

from app.agents.predictive_budget_engine import allocator

#: Max percentage points that may be proposed across all channels in one UTC day.
DAILY_MOVE_CAP_POINTS = 24
#: Minimum relative ROAS gain before proposing a shift.
MIN_ROAS_IMPROVEMENT = 0.05


@dataclass(frozen=True, slots=True)
class ChannelRoas:
    channel: str
    percent: int
    spend: float
    conversions: int
    locked: bool = False

    @property
    def roas(self) -> float:
        """Unit-revenue ROAS proxy: conversions per dollar spent."""
        if self.spend <= 0:
            return 0.0
        return self.conversions / self.spend


@dataclass(frozen=True, slots=True)
class RoasPlan:
    current_shares: dict[str, int]
    proposed_shares: dict[str, int]
    shifts: dict[str, int]
    rationale: str
    blended_before: float
    blended_after: float
    moved_points: int
    meaningful: bool


def blended_roas(channels: list[ChannelRoas], shares: dict[str, int] | None = None) -> float:
    """Projected blended ROAS if total spend is redistributed by ``shares``.

    Channel ROAS is held constant; only the mix changes. This matches how the
    CAC allocator projects outcomes without inventing new conversion rates.
    """
    weights = shares or {c.channel: c.percent for c in channels}
    total_spend = sum(c.spend for c in channels)
    if total_spend <= 0:
        num = sum(c.roas * max(weights.get(c.channel, 0), 0) for c in channels)
        den = sum(max(weights.get(c.channel, 0), 0) for c in channels) or 1
        return num / den
    # Redistribute the observed total spend by target share.
    projected = 0.0
    for c in channels:
        share = max(weights.get(c.channel, 0), 0) / 100.0
        projected += c.roas * (total_spend * share)
    return projected / total_spend


def plan_reallocation(
    channels: list[ChannelRoas],
    *,
    points_used_today: int = 0,
) -> RoasPlan | None:
    """Allocate toward higher-ROAS channels using the predictive engine rails."""
    if len(channels) < 2:
        return None
    remaining = max(0, DAILY_MOVE_CAP_POINTS - points_used_today)
    if remaining < 2:
        return None

    states = [
        allocator.ChannelState(
            channel=c.channel,
            percent=c.percent,
            # Inverse ROAS as CAC so the existing inverse-cost allocator applies.
            cac=(1.0 / c.roas) if c.roas > 0 else 0.0,
            spend=c.spend,
            conversions=c.conversions,
            locked=c.locked,
        )
        for c in channels
    ]
    allocation = allocator.allocate(states)
    if not allocation.is_meaningful:
        return None

    # Clamp total movement to the remaining daily cap.
    moved = allocation.moved_points
    if moved > remaining:
        # Scale shifts proportionally down to the remaining cap.
        scale = remaining / moved
        scaled_shifts = {
            ch: int(round(delta * scale)) for ch, delta in allocation.shifts.items()
        }
        # Repair to sum-zero and apply to baseline.
        baseline = allocator.current_shares(states)
        proposed = dict(baseline)
        for ch, delta in scaled_shifts.items():
            proposed[ch] = max(0, proposed.get(ch, 0) + delta)
        # Renormalise to 100
        total = sum(proposed.values()) or 1
        proposed = {ch: int(round(v * 100 / total)) for ch, v in proposed.items()}
        drift = 100 - sum(proposed.values())
        if proposed:
            first = next(iter(proposed))
            proposed[first] += drift
        shifts = {ch: proposed.get(ch, 0) - baseline.get(ch, 0) for ch in proposed}
        moved = sum(abs(v) for v in shifts.values()) // 2
        allocation = allocator.Allocation(
            percents=proposed,
            shifts=shifts,
            rationale=allocation.rationale + f" (clamped to daily cap {remaining} pts)",
        )

    before = blended_roas(channels)
    after = blended_roas(channels, allocation.percents)
    if before > 0 and (after - before) / before < MIN_ROAS_IMPROVEMENT:
        return None
    if moved < 2:
        return None

    return RoasPlan(
        current_shares=allocator.current_shares(states),
        proposed_shares=dict(allocation.percents),
        shifts=dict(allocation.shifts),
        rationale=allocation.rationale,
        blended_before=round(before, 4),
        blended_after=round(after, 4),
        moved_points=moved,
        meaningful=True,
    )
