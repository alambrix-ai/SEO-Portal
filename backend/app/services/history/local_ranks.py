"""Local ranking history, read straight from ``local_rank_checks``.

No snapshots: every check is already stored as a dated row per search phrase
with the places listed around the business, so history is a matter of
grouping those rows by check. One check (one run of Local Search Tracker)
shares one ``checked_at`` across its phrases; that timestamp is the check's
identity, sent to the browser as integer microseconds since the epoch.

"Top 3" is the three-place map pack Google shows above the full list.
"""
from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.exceptions import NotFoundError
from app.models.local import LocalRankCheck

PACK = 3
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def check_id(moment: datetime) -> str:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    delta = moment - _EPOCH
    return str((delta.days * 86_400 + delta.seconds) * 1_000_000 + delta.microseconds)


def moment_of(check: str) -> datetime:
    try:
        micros = int(check)
    except (TypeError, ValueError):
        raise NotFoundError("That check was not found") from None
    return _EPOCH + timedelta(microseconds=micros)


# ── Pure summaries ──────────────────────────────────────────────────────────
def summarise(rows: Sequence[Any]) -> dict[str, Any]:
    """Headline numbers for one check's rows."""
    found = [r.position for r in rows if r.position is not None]
    return {
        "phrases": len(rows),
        "found_in": len(found),
        "in_pack": sum(1 for p in found if p <= PACK),
        "best": min(found) if found else None,
        "average_position": round(sum(found) / len(found), 1) if found else None,
    }


def _above(row: Any) -> list[dict[str, Any]]:  # noqa: ANN401
    """Who is listed above the business; the top three when it is not listed."""
    limit = (row.position - 1) if row.position is not None else PACK
    out = []
    for place in (row.top_results or [])[: max(limit, 0)]:
        if place.get("place_id") and place.get("place_id") == row.place_id:
            continue
        out.append({
            "position": place.get("position"),
            "name": place.get("name") or "",
            "rating": place.get("rating"),
            "review_count": place.get("review_count"),
        })
    return out


def compare(now: Sequence[Any], before: Sequence[Any]) -> dict[str, Any]:
    """Per phrase: moved up or down, entered or left the map pack, in or out of the results."""
    prev = {r.phrase: r for r in before}
    cur = {r.phrase: r for r in now}
    up, down, entered, left, appeared, dropped, same = [], [], [], [], [], [], []
    for phrase in sorted(cur):
        if phrase not in prev:
            continue
        a, b = cur[phrase].position, prev[phrase].position
        row = {"phrase": phrase, "before": b, "after": a}
        if a == b:
            same.append(phrase)
            continue
        if a is None:
            dropped.append(row)
        elif b is None:
            appeared.append(row)
        elif a < b:
            up.append({**row, "moved": b - a})
        else:
            down.append({**row, "moved": a - b})
        in_now = a is not None and a <= PACK
        in_before = b is not None and b <= PACK
        if in_now and not in_before:
            entered.append(row)
        elif in_before and not in_now:
            left.append(row)
    return {
        "moved_up": up,
        "moved_down": down,
        "entered_pack": entered,
        "left_pack": left,
        "entered_results": appeared,
        "dropped_out": dropped,
        "unchanged": same,
        "new_phrases": sorted(set(cur) - set(prev)),
        "stopped_phrases": sorted(set(prev) - set(cur)),
    }


def phrase_rows(rows: Sequence[Any]) -> list[dict[str, Any]]:
    return [
        {
            "phrase": r.phrase,
            "position": r.position,
            "results_seen": r.results_seen,
            "above": _above(r),
        }
        for r in sorted(rows, key=lambda r: r.phrase)
    ]


def trends(checks: Sequence[tuple[datetime, Sequence[Any]]]) -> list[dict[str, Any]]:
    """Position over time per phrase, oldest first; ``checks`` is newest first."""
    phrases: dict[str, dict[str, Any]] = {}
    for moment, rows in reversed(list(checks)):
        for r in rows:
            entry = phrases.setdefault(r.phrase, {"phrase": r.phrase, "points": []})
            entry["points"].append({"at": moment.isoformat(), "position": r.position, "results_seen": r.results_seen})
            entry["latest"] = r.position
            entry["above"] = _above(r)
    return sorted(phrases.values(), key=lambda e: e["phrase"])


def group(rows: Iterable[Any]) -> list[tuple[datetime, list[Any]]]:
    """Rows grouped by ``checked_at``, newest first."""
    out: dict[datetime, list[Any]] = {}
    for r in rows:
        out.setdefault(r.checked_at, []).append(r)
    return sorted(out.items(), key=lambda kv: kv[0], reverse=True)


# ── Reading ─────────────────────────────────────────────────────────────────
def _checks(db: Session, tenant_id: str, limit: int, *, before: datetime | None = None) -> list[tuple[datetime, list[Any]]]:
    stmt = select(LocalRankCheck.checked_at).where(LocalRankCheck.tenant_id == tenant_id)
    if before is not None:
        stmt = stmt.where(LocalRankCheck.checked_at < before)
    moments = list(db.scalars(stmt.distinct().order_by(LocalRankCheck.checked_at.desc()).limit(limit)))
    if not moments:
        return []
    rows = db.scalars(
        select(LocalRankCheck).where(LocalRankCheck.tenant_id == tenant_id, LocalRankCheck.checked_at.in_(moments))
    )
    return group(rows)


def _entry(moment: datetime, rows: list[Any], previous: list[Any] | None) -> dict[str, Any]:
    metrics = summarise(rows)
    before = summarise(previous) if previous is not None else None
    run_ids = {r.run_id for r in rows if r.run_id}
    return {
        "id": check_id(moment),
        "created_at": moment.isoformat(),
        "source": "agent_run",
        "label": "Local Search Tracker run",
        "created_by": None,
        "run_id": next(iter(run_ids)) if len(run_ids) == 1 else None,
        "truncated": False,
        "headline": metrics["in_pack"],
        "change": metrics["in_pack"] - before["in_pack"] if before is not None else None,
        "metrics": metrics,
    }


def history(db: Session, tenant_id: str, limit: int) -> dict[str, Any]:
    checks = _checks(db, tenant_id, limit + 1)
    entries = [
        _entry(moment, rows, checks[i + 1][1] if i + 1 < len(checks) else None)
        for i, (moment, rows) in enumerate(checks[:limit])
    ]
    return {
        "entries": entries,
        "trends": trends(checks[:limit]),
        "started_at": None,
        "baseline": False,
    }


def detail(db: Session, tenant_id: str, check: str) -> dict[str, Any]:
    moment = moment_of(check)
    rows = list(
        db.scalars(
            select(LocalRankCheck).where(
                LocalRankCheck.tenant_id == tenant_id,
                LocalRankCheck.checked_at >= moment,
                LocalRankCheck.checked_at < moment + timedelta(microseconds=1),
            )
        )
    )
    if not rows:
        raise NotFoundError("That check was not found")
    prior = _checks(db, tenant_id, 1, before=rows[0].checked_at)
    previous = prior[0] if prior else None
    entry = _entry(rows[0].checked_at, rows, previous[1] if previous else None)
    return {
        **entry,
        "phrases": phrase_rows(rows),
        "comparison": (
            {
                "previous_id": check_id(previous[0]),
                "previous_at": previous[0].isoformat(),
                "before": summarise(previous[1]),
                **compare(rows, previous[1]),
            }
            if previous
            else None
        ),
    }
