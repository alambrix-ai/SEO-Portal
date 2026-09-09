"""The budget split, and the one invariant it has to hold.

**The channel shares always sum to 100.** Not approximately, and not "unless a
channel is locked" — exactly 100, after every write. A split totalling 129%
is not a rounding blemish, it is a number that cannot mean anything: the
console shows an allocation total, the agents divide a monthly budget by it,
and 129% of a budget is not a thing anyone can spend.

That invariant used to be enforced in one place and not the other. The
predictive engine's allocator is careful about it — it normalises its baseline
and repairs rounding drift before returning. The console's manual slider was
not: it set one channel's share, locked it, and left the others untouched, so
every hand edit pushed the total further from 100. Five edits and the workspace
was reporting a 129% allocation with fabricated-looking shift deltas.

So the manual path goes through here, where the difference is absorbed by the
channels that are still under automatic control — which is also the honest
answer to "what happens to the other 79%?": it comes out of whatever the
engine is still allowed to move.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.exceptions import ConflictError, NotFoundError
from app.core.logging import get_logger
from app.models.ads import BudgetAllocation

log = get_logger(__name__)

TOTAL = 100


def load_split(db: Session, *, tenant_id: str) -> list[BudgetAllocation]:
    return list(
        db.execute(
            select(BudgetAllocation)
            .where(BudgetAllocation.tenant_id == tenant_id)
            .order_by(BudgetAllocation.channel)
        ).scalars()
    )


def _distribute(rows: list[BudgetAllocation], target: int) -> dict[str, int]:
    """Split ``target`` across ``rows`` in proportion to their current shares.

    Proportional rather than even, so absorbing a change preserves the
    relative standing the engine had already worked out between the remaining
    channels. When they are all at zero there is no proportion to preserve, so
    it falls back to an even split.
    """
    if not rows:
        return {}
    current = {row.channel: max(0, row.percent) for row in rows}
    pool = sum(current.values())

    if pool == 0:
        base, extra = divmod(target, len(rows))
        result = {row.channel: base for row in rows}
        for row in rows[:extra]:
            result[row.channel] += 1
        return result

    raw = {channel: (value / pool) * target for channel, value in current.items()}
    result = {channel: int(value) for channel, value in raw.items()}
    # Hand out the remainder to the largest fractional parts, so the rounding
    # goes where it is least visible rather than always to the first channel.
    remainder = target - sum(result.values())
    by_fraction = sorted(raw, key=lambda c: raw[c] - int(raw[c]), reverse=True)
    for channel in by_fraction[: max(0, remainder)]:
        result[channel] += 1
    return result


def set_share(
    db: Session, *, tenant_id: str, channel: str, percent: int
) -> list[BudgetAllocation]:
    """Set one channel by hand, and rebalance the rest so the total is 100.

    The edited channel is locked, because a hand-set share that the engine
    could quietly undo on its next pass is not really a setting. Everything
    still unlocked absorbs the difference.

    Raises :class:`ConflictError` when the arithmetic cannot work — every other
    channel locked, or the locked shares already exceeding what is left. That
    is a refusal with a reason rather than a silently impossible split.
    """
    rows = load_split(db, tenant_id=tenant_id)
    target = next((r for r in rows if r.channel == channel), None)
    if target is None:
        raise NotFoundError(f"Channel {channel!r} is not configured")

    previous = target.percent
    others = [r for r in rows if r.channel != channel]
    movable = [r for r in others if not r.locked]
    locked_share = sum(r.percent for r in others if r.locked)

    if percent + locked_share > TOTAL:
        held = ", ".join(r.channel for r in others if r.locked) or "none"
        raise ConflictError(
            f"{percent}% leaves less than nothing for the other channels — "
            f"{locked_share}% is locked ({held}). Unlock one, or lower this share."
        )
    if not movable and percent != TOTAL - locked_share:
        raise ConflictError(
            f"Every other channel is locked, so this one has to be "
            f"{TOTAL - locked_share}%. Unlock a channel to change the split."
        )

    target.percent = percent
    target.locked = True
    target.last_shift = percent - previous

    for row in movable:
        row.last_shift = 0
    for row_channel, share in _distribute(movable, TOTAL - percent - locked_share).items():
        row = next(r for r in movable if r.channel == row_channel)
        row.last_shift = share - row.percent
        row.percent = share

    db.flush()
    _assert_total(rows, context=f"manual edit of {channel}")
    return rows


def repair_split(db: Session, *, tenant_id: str) -> int:
    """Force an existing split back to 100, returning the drift it corrected.

    For workspaces written before the manual path enforced the invariant. It
    prefers to take the correction out of unlocked channels, and falls back to
    scaling everything when they are all locked — a locked channel is a
    preference, and a split that does not add up is a defect, so the defect
    wins.
    """
    rows = load_split(db, tenant_id=tenant_id)
    if not rows:
        return 0
    total = sum(r.percent for r in rows)
    drift = total - TOTAL
    if drift == 0:
        return 0

    movable = [r for r in rows if not r.locked]
    locked_share = sum(r.percent for r in rows if r.locked)
    if movable and locked_share <= TOTAL:
        for channel, share in _distribute(movable, TOTAL - locked_share).items():
            next(r for r in movable if r.channel == channel).percent = share
    else:
        for channel, share in _distribute(rows, TOTAL).items():
            next(r for r in rows if r.channel == channel).percent = share

    db.flush()
    _assert_total(rows, context="repair")
    log.info("Repaired a budget split that totalled %d%% for %s", total, tenant_id)
    return drift


def _assert_total(rows: list[BudgetAllocation], *, context: str) -> None:
    total = sum(r.percent for r in rows)
    if total != TOTAL:
        # A bug here silently misreports a customer's spend, so it is worth
        # failing loudly at the point the split was written rather than
        # letting the console render an impossible number.
        raise AssertionError(
            f"Budget split came to {total}% after {context}: "
            + ", ".join(f"{r.channel}={r.percent}" for r in rows)
        )
