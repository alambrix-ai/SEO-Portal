"""Site analysis versions: one per finished in-portal analysis, compared check by check.

The owning service (``demo_analysis.workspace``) calls
``history.record_version(db, org, "site_analysis", subject_for(website_url),
{"analysis_id", "website_url", "business_url", "report"}, ...)`` when an
analysis finishes. Each website is its own subject, so its versions run
1, 2, 3 … and compare like with like.

What is kept:

* ``items``   check id -> ``[status, priority, category, title]``. The titles
              are the platform's own fixed check names; the evidence (which
              quotes the customer's pages) is not kept.
* ``metrics`` the overall and per-area scores and the check counts.
* ``inputs``  the analysis id and counts (structural only).
* the website and business addresses, encrypted, as the customer's data.

The report's ``ai`` section is model output and is never read here.
"""
from __future__ import annotations

import hashlib
from typing import Any
from urllib.parse import urlsplit

from app.core.exceptions import InvalidInputError
from app.services.history import store

PASS, WARN, FAIL, INFO = "pass", "warn", "fail", "info"
BAD = frozenset({WARN, FAIL})
STATUS_WORDS = {PASS: "Passing", WARN: "Needs attention", FAIL: "Failing", INFO: "For information"}
_PRIORITY_RANK = {"high": 0, "medium": 1, "low": 2}
NOUN = ("issue", "issues")


def subject_for(website_url: str) -> str:
    """A stable subject id for one website: same site, same id; nothing readable."""
    raw = str(website_url or "").strip().lower()
    host = urlsplit(raw if "://" in raw else f"https://{raw}").hostname or raw
    if host.startswith("www."):
        host = host[4:]
    return "site-" + hashlib.sha256(host.encode("utf-8")).hexdigest()[:32]


def _text(value: Any, limit: int) -> str:  # noqa: ANN401 - report JSON
    return str(value)[:limit] if isinstance(value, (str, int, float)) else ""


def _int(value: Any) -> int | None:  # noqa: ANN401 - report JSON
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def snapshot(payload: Any) -> store.Snapshot:  # noqa: ANN401 - the caller's document
    if not isinstance(payload, dict) or not isinstance(payload.get("report"), dict):
        raise InvalidInputError("This analysis could not be kept in the history.")
    report: dict[str, Any] = payload["report"]
    checks = [c for c in (report.get("checks") or []) if isinstance(c, dict) and c.get("id")]
    order = {FAIL: 0, WARN: 1, INFO: 2, PASS: 3}
    checks.sort(key=lambda c: (order.get(c.get("status"), 9), _PRIORITY_RANK.get(c.get("priority"), 9)))
    items, total = store.cap_items(
        (
            _text(c["id"], 64),
            [
                _text(c.get("status"), 8),
                _text(c.get("priority"), 8),
                _text(c.get("category"), 32),
                _text(c.get("title"), 160),
            ],
        )
        for c in checks
    )
    overall = report.get("overall") if isinstance(report.get("overall"), dict) else {}
    categories = report.get("categories") if isinstance(report.get("categories"), dict) else {}
    summary = report.get("summary") if isinstance(report.get("summary"), dict) else {}
    metrics = {
        "score": _int(overall.get("score")),
        "grade": _text(overall.get("grade"), 4),
        "categories": {
            _text(key, 32): {
                "label": _text((cat or {}).get("label"), 80),
                "score": _int((cat or {}).get("score")),
            }
            for key, cat in list(categories.items())[:20]
            if isinstance(cat, dict)
        },
        "checks": len(checks),
        "failed": sum(1 for c in checks if c.get("status") == FAIL),
        "warnings": sum(1 for c in checks if c.get("status") == WARN),
        "passed": sum(1 for c in checks if c.get("status") == PASS),
        "pages_read": _int(summary.get("pages_read")),
    }
    return store.Snapshot(
        metrics=metrics,
        items=items,
        item_count=total,
        inputs={"analysis_id": _text(payload.get("analysis_id"), 36), "checks": len(checks)},
        secret={
            "website_url": _text(payload.get("website_url"), 500),
            "business_url": _text(payload.get("business_url"), 1000),
        },
    )


def headline(metrics: dict[str, Any]) -> int | None:
    return metrics.get("score")


def entry_metrics(metrics: dict[str, Any]) -> dict[str, Any]:
    return {
        "score": metrics.get("score"),
        "grade": metrics.get("grade"),
        "failed": metrics.get("failed"),
        "warnings": metrics.get("warnings"),
        "checks": metrics.get("checks"),
    }


def _state(items: dict[str, Any], key: str) -> tuple[str, str, str, str]:
    status, priority, category, title = (list(items.get(key) or []) + [""] * 4)[:4]
    return str(status), str(priority), str(category), str(title)


def compare(
    before_metrics: dict[str, Any],
    before_items: dict[str, Any],
    after_metrics: dict[str, Any],
    after_items: dict[str, Any],
    *,
    truncated: bool = False,
) -> dict[str, Any]:
    """New problems, fixed ones, ones that got worse, from the earlier version to the later."""
    new: list[tuple[int, dict[str, Any]]] = []
    fixed: list[dict[str, Any]] = []
    worse: list[dict[str, Any]] = []
    unchanged = 0
    rank = {FAIL: 0, WARN: 1}
    for key in after_items:
        status, priority, _category, title = _state(after_items, key)
        label = title or key.replace("_", " ").capitalize()
        if key not in before_items:
            if status in BAD:
                new.append((rank.get(status, 9), store.change_row(label, before="Not checked", after=STATUS_WORDS[status])))
            continue
        old = _state(before_items, key)[0]
        if status in BAD and old not in BAD:
            new.append((rank.get(status, 9), store.change_row(label, before=STATUS_WORDS.get(old, "Not checked"), after=STATUS_WORDS[status])))
        elif old in BAD and status == PASS:
            fixed.append(store.change_row(label, before=STATUS_WORDS.get(old, old), after=STATUS_WORDS[PASS]))
        elif old == WARN and status == FAIL:
            worse.append(store.change_row(label, before=STATUS_WORDS[WARN], after=STATUS_WORDS[FAIL]))
        elif old == FAIL and status == WARN:
            fixed.append(store.change_row(label, before=STATUS_WORDS[FAIL], after=STATUS_WORDS[WARN]))
        else:
            unchanged += 1
    new.sort(key=lambda pair: (pair[0], pair[1]["label"]))

    measures = [store.measure("Overall score", before_metrics.get("score"), after_metrics.get("score"), better="higher")]
    before_cats = before_metrics.get("categories") or {}
    for key, cat in (after_metrics.get("categories") or {}).items():
        previous = (before_cats.get(key) or {}).get("score")
        measures.append(
            store.measure(cat.get("label") or key, previous, cat.get("score"), better="higher")
        )
    measures.append(store.measure("Checks failing", before_metrics.get("failed"), after_metrics.get("failed"), better="lower"))
    return store.comparison(
        noun=NOUN,
        new=[row for _rank, row in new],
        fixed=fixed,
        worse=worse,
        unchanged=unchanged,
        measures=measures,
        truncated=truncated,
    )
