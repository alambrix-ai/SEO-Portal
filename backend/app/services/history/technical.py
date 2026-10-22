"""Technical SEO audit history: open issues per run, and what changed.

``items`` maps an issue id to ``[status, severity, kind, url, target]`` for
every issue that is open or ignored at the time of the snapshot. Fixed issues
are left out: they are what *stopped* being open, which a diff finds by their
absence, and keeping every issue ever fixed would grow without end.
"""
from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.seo import SeoIssue, SeoPage
from app.services.history import store

SEVERITIES = ("high", "medium", "low")
_RANK = {s: i for i, s in enumerate(SEVERITIES)}
#: Findings only a page-speed reading can raise (see the auditor's VITALS_KINDS).
SPEED_KINDS = frozenset({"slow_lcp_mobile", "poor_cls_mobile", "slow_inp_mobile"})
#: Issues listed per group in a comparison; the counts are always complete.
LIST_LIMIT = 200


def _labels() -> dict[str, str]:
    from app.services.views import ISSUE_LABELS

    return ISSUE_LABELS


def label_for(kind: str) -> str:
    return _labels().get(kind, kind.replace("_", " ").capitalize())


# ── Building ────────────────────────────────────────────────────────────────
def summarise(
    issues: Iterable[tuple[str, str, str, str, str, str]],
    *,
    pages_audited: int | None,
    vitals_measured: int | None = None,
    opened: int | None = None,
    resolved: int | None = None,
    fixed_total: int | None = None,
) -> tuple[dict[str, Any], dict[str, Any], int]:
    """``(metrics, items, item_count)`` from ``(id, status, severity, kind, url, target)`` rows.

    Pure, so it is tested without a database. Only open and ignored issues
    are expected; anything else is ignored.
    """
    rows = [r for r in issues if r[1] in ("open", "ignored")]
    open_rows = [r for r in rows if r[1] == "open"]
    by_severity = {s: 0 for s in SEVERITIES}
    by_kind: dict[str, int] = {}
    for _id, _status, severity, kind, _url, _target in open_rows:
        by_severity[severity] = by_severity.get(severity, 0) + 1
        by_kind[kind] = by_kind.get(kind, 0) + 1
    metrics: dict[str, Any] = {
        "open": len(open_rows),
        "ignored": len(rows) - len(open_rows),
        "by_severity": by_severity,
        "by_kind": dict(sorted(by_kind.items(), key=lambda kv: (-kv[1], kv[0]))),
        "speed_open": sum(1 for r in open_rows if r[3] in SPEED_KINDS),
        "pages_audited": pages_audited,
        # None means "not known for this snapshot" (a baseline), 0 means
        # "a run measured no pages" — the screen must not show them alike.
        "vitals_measured": vitals_measured,
        "opened": opened,
        "resolved": resolved,
        "fixed_total": fixed_total,
    }
    # Open first, worst first, so a cut keeps what matters.
    ordered = sorted(rows, key=lambda r: (r[1] != "open", _RANK.get(r[2], 9), r[4], r[3]))
    items, total = store.cap_items((r[0], [r[1], r[2], r[3], r[4][:300], r[5][:200]]) for r in ordered)
    return metrics, items, total


def build(db: Session, tenant_id: str, run: Any = None) -> tuple[dict[str, Any], dict[str, Any], int]:  # noqa: ANN401
    rows = db.execute(
        select(SeoIssue.id, SeoIssue.status, SeoIssue.severity, SeoIssue.kind, SeoIssue.url, SeoIssue.target).where(
            SeoIssue.tenant_id == tenant_id, SeoIssue.status.in_(("open", "ignored"))
        )
    ).all()
    fixed_total = db.scalar(
        select(func.count()).select_from(SeoIssue).where(SeoIssue.tenant_id == tenant_id, SeoIssue.status == "fixed")
    )
    detail = (getattr(run, "detail", None) or {}) if run is not None else {}
    pages = detail.get("pages_audited")
    if pages is None:
        pages = db.scalar(select(func.count()).select_from(SeoPage).where(SeoPage.tenant_id == tenant_id))
    return summarise(
        (tuple(r) for r in rows),
        pages_audited=pages,
        vitals_measured=detail.get("vitals_measured") if run is not None else None,
        opened=detail.get("opened"),
        resolved=detail.get("resolved"),
        fixed_total=int(fixed_total or 0),
    )


def has_data(db: Session, tenant_id: str) -> bool:
    return bool(db.scalar(select(func.count()).select_from(SeoIssue).where(SeoIssue.tenant_id == tenant_id)))


def headline(metrics: dict[str, Any]) -> int | None:
    return metrics.get("open")


def entry_metrics(metrics: dict[str, Any]) -> dict[str, Any]:
    return {
        "open": metrics.get("open"),
        "high": (metrics.get("by_severity") or {}).get("high"),
        "pages_audited": metrics.get("pages_audited"),
        "vitals_measured": metrics.get("vitals_measured"),
        "speed_open": metrics.get("speed_open"),
    }


# ── Comparing ───────────────────────────────────────────────────────────────
def _issue(key: str, state: list[Any]) -> dict[str, Any]:
    status, severity, kind, url, target = (list(state) + [""] * 5)[:5]
    return {"id": key, "severity": severity, "kind": kind, "label": label_for(kind), "url": url, "target": target}


def _listed(keys: Iterable[str], items: dict[str, Any]) -> dict[str, Any]:
    rows = sorted((_issue(k, items[k]) for k in keys), key=lambda i: (_RANK.get(i["severity"], 9), i["label"], i["url"]))
    return {"count": len(rows), "items": rows[:LIST_LIMIT]}


def compare(
    now_metrics: dict[str, Any],
    now_items: dict[str, Any],
    before_metrics: dict[str, Any],
    before_items: dict[str, Any],
) -> dict[str, Any]:
    """Issues newly found, fixed, ignored since, and still open; and the counts."""
    now_open = {k for k, v in now_items.items() if v and v[0] == "open"}
    before_open = {k for k, v in before_items.items() if v and v[0] == "open"}
    gone = before_open - now_open
    ignored_since = {k for k in gone if (now_items.get(k) or [""])[0] == "ignored"}
    fixed = gone - ignored_since

    severity = {
        s: {
            "before": (before_metrics.get("by_severity") or {}).get(s, 0),
            "after": (now_metrics.get("by_severity") or {}).get(s, 0),
        }
        for s in SEVERITIES
    }
    for value in severity.values():
        value["change"] = value["after"] - value["before"]

    kinds_now = now_metrics.get("by_kind") or {}
    kinds_before = before_metrics.get("by_kind") or {}
    kinds = [
        {
            "kind": k,
            "label": label_for(k),
            "before": kinds_before.get(k, 0),
            "after": kinds_now.get(k, 0),
            "change": kinds_now.get(k, 0) - kinds_before.get(k, 0),
        }
        for k in sorted(set(kinds_now) | set(kinds_before))
    ]
    kinds.sort(key=lambda r: (-abs(r["change"]), r["label"]))

    return {
        "open": {
            "before": before_metrics.get("open"),
            "after": now_metrics.get("open"),
            "change": store.change(now_metrics.get("open"), before_metrics.get("open")),
        },
        "pages_audited": {
            "before": before_metrics.get("pages_audited"),
            "after": now_metrics.get("pages_audited"),
        },
        "speed": {
            "measured_before": before_metrics.get("vitals_measured"),
            "measured_after": now_metrics.get("vitals_measured"),
            "open_before": before_metrics.get("speed_open"),
            "open_after": now_metrics.get("speed_open"),
        },
        "severity": severity,
        "kinds": kinds,
        "new_issues": _listed(now_open - before_open, now_items),
        "fixed": _listed(fixed, before_items),
        "ignored_since": _listed(ignored_since, before_items),
        "still_open": _listed(now_open & before_open, now_items),
    }
