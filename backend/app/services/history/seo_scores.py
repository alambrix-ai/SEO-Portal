"""SEO & AEO page score history: the semantic-gap scores per run, and movers.

The score is On-Page SEO Sync's **semantic gap** (0-100): how much of a
page's topic the copy leaves unaddressed. Lower is better. Only pages the
agent has actually analysed count as scored — a page that was only synced
carries the default 0, and averaging that in would flatter the site.

``items`` maps a page url to ``[gap, title]`` for every scored page.
"""
from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.seo import SeoPage
from app.services.history import store

#: The rewrite threshold On-Page SEO Sync uses (REWRITE_THRESHOLD).
REWRITE_AT = 45
BUCKETS = (
    ("small", "Small gap (0-24)", 0, 24),
    ("some", "Some gap (25-44)", 25, REWRITE_AT - 1),
    ("rewrite", "Rewrite suggested (45-69)", REWRITE_AT, 69),
    ("large", "Large gap (70-100)", 70, 100),
)
#: Pages listed per group in a comparison.
MOVERS = 10
LIST_LIMIT = 100


def bucket_of(gap: int) -> str:
    for key, _label, low, high in BUCKETS:
        if low <= gap <= high:
            return key
    return "large"


# ── Building ────────────────────────────────────────────────────────────────
def summarise(
    pages: Iterable[tuple[str, str, int, str, bool]],
) -> tuple[dict[str, Any], dict[str, Any], int]:
    """``(metrics, items, item_count)`` from ``(url, title, gap, status, scored)`` rows. Pure."""
    rows = list(pages)
    scored = [r for r in rows if r[4]]
    distribution = {key: 0 for key, *_ in BUCKETS}
    for r in scored:
        distribution[bucket_of(int(r[2]))] += 1
    statuses: dict[str, int] = {}
    for r in rows:
        statuses[r[3]] = statuses.get(r[3], 0) + 1
    metrics: dict[str, Any] = {
        "pages_total": len(rows),
        "pages_scored": len(scored),
        "average_gap": round(sum(int(r[2]) for r in scored) / len(scored), 1) if scored else None,
        "distribution": distribution,
        # Proposed and not yet published: flagged (needs approval) + queued
        # (awaiting approval).
        "rewrites_pending": statuses.get("flagged", 0) + statuses.get("queued", 0),
        "awaiting_approval": statuses.get("queued", 0),
        # Pages whose current copy is the agent's published rewrite. A page
        # goes back to "live" when its content changes again, so this is the
        # current count, not every rewrite ever applied.
        "rewritten": statuses.get("rewritten", 0),
    }
    ordered = sorted(scored, key=lambda r: (-int(r[2]), r[0]))
    items, total = store.cap_items((r[0], [int(r[2]), (r[1] or "")[:120]]) for r in ordered)
    return metrics, items, total


def build(db: Session, tenant_id: str, run: Any = None) -> tuple[dict[str, Any], dict[str, Any], int]:  # noqa: ANN401, ARG001
    rows = db.execute(
        select(SeoPage.url, SeoPage.title, SeoPage.gap_score, SeoPage.status, SeoPage.last_synced_at).where(
            SeoPage.tenant_id == tenant_id
        )
    ).all()
    return summarise((r[0], r[1], r[2] or 0, r[3], r[4] is not None) for r in rows)


def has_data(db: Session, tenant_id: str) -> bool:
    return bool(
        db.scalar(
            select(func.count())
            .select_from(SeoPage)
            .where(SeoPage.tenant_id == tenant_id, SeoPage.last_synced_at.is_not(None))
        )
    )


def headline(metrics: dict[str, Any]) -> float | None:
    return metrics.get("average_gap")


def entry_metrics(metrics: dict[str, Any]) -> dict[str, Any]:
    return {
        "average_gap": metrics.get("average_gap"),
        "pages_scored": metrics.get("pages_scored"),
        "pages_total": metrics.get("pages_total"),
        "rewrites_pending": metrics.get("rewrites_pending"),
        "rewritten": metrics.get("rewritten"),
    }


# ── Comparing ───────────────────────────────────────────────────────────────
def _page(url: str, state: list[Any]) -> dict[str, Any]:
    gap, title = (list(state) + [0, ""])[:2]
    return {"url": url, "title": title, "gap": gap}


def compare(
    now_metrics: dict[str, Any],
    now_items: dict[str, Any],
    before_metrics: dict[str, Any],
    before_items: dict[str, Any],
) -> dict[str, Any]:
    """Pages whose gap went down (better) or up (worse), and pages new or gone."""
    improved, worsened = [], []
    for url in set(now_items) & set(before_items):
        after, before = int(now_items[url][0]), int(before_items[url][0])
        if after == before:
            continue
        row = {**_page(url, now_items[url]), "before": before, "after": after, "change": after - before}
        (improved if after < before else worsened).append(row)
    improved.sort(key=lambda r: (r["change"], r["url"]))
    worsened.sort(key=lambda r: (-r["change"], r["url"]))
    added = sorted((_page(u, now_items[u]) for u in set(now_items) - set(before_items)), key=lambda r: r["url"])
    removed = sorted((_page(u, before_items[u]) for u in set(before_items) - set(now_items)), key=lambda r: r["url"])

    def pair(key: str) -> dict[str, Any]:
        return {
            "before": before_metrics.get(key),
            "after": now_metrics.get(key),
            "change": store.change(now_metrics.get(key), before_metrics.get(key)),
        }

    return {
        "average_gap": pair("average_gap"),
        "pages_scored": pair("pages_scored"),
        "rewrites_pending": pair("rewrites_pending"),
        "rewritten": pair("rewritten"),
        "distribution": [
            {
                "key": key,
                "label": label,
                "before": (before_metrics.get("distribution") or {}).get(key, 0),
                "after": (now_metrics.get("distribution") or {}).get(key, 0),
            }
            for key, label, *_ in BUCKETS
        ],
        "improved": {"count": len(improved), "items": improved[:MOVERS]},
        "worsened": {"count": len(worsened), "items": worsened[:MOVERS]},
        "unchanged": len(set(now_items) & set(before_items)) - len(improved) - len(worsened),
        "added": {"count": len(added), "items": added[:LIST_LIMIT]},
        "removed": {"count": len(removed), "items": removed[:LIST_LIMIT]},
    }
