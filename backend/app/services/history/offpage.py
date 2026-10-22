"""Off-page scan history: backlink opportunities and competitor links per run.

* ``backlink_scan``    after each Backlink Node Discovery run. ``items`` maps
                       a domain to ``[status, authority, placement_type]``.
                       ``backlink_targets`` is upserted per domain, so without
                       this there is no record of how the list moved.
* ``competitor_scan``  after each Competitor Link Monitor run. ``items`` maps
                       an alert id to ``[competitor, source_domain, authority,
                       countered]`` for the alerts of the last
                       :data:`ALERT_WINDOW_DAYS` days. The alert text is not
                       kept: it quotes third-party pages.

Each builder has the same shape as ``technical`` and ``seo_scores``
(``build``, ``has_data``, ``headline``, ``entry_metrics``, ``compare``), and
its ``compare`` returns the shared comparison structure from ``store``.
"""
from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.base import utcnow
from app.models.offpage import BacklinkStatus, BacklinkTarget, CompetitorAlert
from app.services.history import store

#: Alerts older than this drop out of a competitor snapshot.
ALERT_WINDOW_DAYS = 90
#: A competitor link from a site at least this strong is called out.
HIGH_AUTHORITY = 50

_STAGES = {
    BacklinkStatus.DISCOVERED.value: 0,
    BacklinkStatus.PITCHED.value: 1,
    BacklinkStatus.REPLIED.value: 2,
    BacklinkStatus.WON.value: 3,
}
_REJECTED = BacklinkStatus.REJECTED.value
_WON = BacklinkStatus.WON.value


def _state(items: dict[str, Any], key: str, width: int) -> list[Any]:
    return (list(items.get(key) or []) + [None] * width)[:width]


class BacklinkScan:
    """Backlink opportunities found, pitched, won or rejected."""

    NOUN = ("link opportunity", "link opportunities")

    @staticmethod
    def summarise(rows: list[tuple[str, str, int, str]]) -> tuple[dict[str, Any], dict[str, Any], int]:
        """``(metrics, items, item_count)`` from ``(domain, status, authority, placement)`` rows. Pure."""
        by_status: dict[str, int] = {}
        for _domain, status, _authority, _placement in rows:
            by_status[status] = by_status.get(status, 0) + 1
        authorities = [int(r[2] or 0) for r in rows]
        metrics = {
            "targets": len(rows),
            "won": by_status.get(_WON, 0),
            "in_progress": by_status.get(BacklinkStatus.PITCHED.value, 0) + by_status.get(BacklinkStatus.REPLIED.value, 0),
            "rejected": by_status.get(_REJECTED, 0),
            "by_status": dict(sorted(by_status.items())),
            "average_authority": round(sum(authorities) / len(authorities), 1) if authorities else None,
        }
        ordered = sorted(rows, key=lambda r: (-int(r[2] or 0), r[0]))
        items, total = store.cap_items(
            (r[0][:255], [r[1], int(r[2] or 0), (r[3] or "")[:40]]) for r in ordered
        )
        return metrics, items, total

    @staticmethod
    def build(db: Session, tenant_id: str, run: Any = None) -> tuple[dict[str, Any], dict[str, Any], int]:  # noqa: ANN401, ARG004
        rows = db.execute(
            select(
                BacklinkTarget.domain, BacklinkTarget.status, BacklinkTarget.authority, BacklinkTarget.placement_type
            ).where(BacklinkTarget.tenant_id == tenant_id)
        ).all()
        return BacklinkScan.summarise([(r[0], r[1], r[2], r[3]) for r in rows])

    @staticmethod
    def has_data(db: Session, tenant_id: str) -> bool:
        return bool(
            db.scalar(select(func.count()).select_from(BacklinkTarget).where(BacklinkTarget.tenant_id == tenant_id))
        )

    @staticmethod
    def headline(metrics: dict[str, Any]) -> int | None:
        return metrics.get("won")

    @staticmethod
    def entry_metrics(metrics: dict[str, Any]) -> dict[str, Any]:
        return {
            "targets": metrics.get("targets"),
            "won": metrics.get("won"),
            "in_progress": metrics.get("in_progress"),
            "rejected": metrics.get("rejected"),
        }

    @staticmethod
    def compare(
        now_metrics: dict[str, Any],
        now_items: dict[str, Any],
        before_metrics: dict[str, Any],
        before_items: dict[str, Any],
    ) -> dict[str, Any]:
        """From the earlier version (``before``) to the later (``now``)."""
        new, fixed, worse = [], [], []
        unchanged = 0
        for domain in sorted(now_items):
            status, authority, placement = _state(now_items, domain, 3)
            if domain not in before_items:
                new.append(store.change_row(domain, where=placement or None, after=status))
                continue
            old = _state(before_items, domain, 3)[0]
            if status == old:
                unchanged += 1
            elif status == _REJECTED or (old == _WON and status != _WON):
                worse.append(store.change_row(domain, where=placement or None, before=old, after=status))
            elif _STAGES.get(status, -1) > _STAGES.get(old, -1):
                fixed.append(store.change_row(domain, where=placement or None, before=old, after=status))
            else:
                unchanged += 1
        for domain in sorted(set(before_items) - set(now_items)):
            old = _state(before_items, domain, 3)[0]
            if old == _WON:
                worse.append(store.change_row(domain, before=old, after="No longer listed"))
        return store.comparison(
            noun=BacklinkScan.NOUN,
            new=new,
            fixed=fixed,
            worse=worse,
            unchanged=unchanged,
            fixed_word="moved forward",
            measures=[
                store.measure("Links won", before_metrics.get("won"), now_metrics.get("won"), better="higher"),
                store.measure("Opportunities found", before_metrics.get("targets"), now_metrics.get("targets"), better="higher"),
                store.measure("Rejected", before_metrics.get("rejected"), now_metrics.get("rejected"), better="lower"),
            ],
        )


class CompetitorScan:
    """Links competitors earned recently, and which ones were answered."""

    NOUN = ("competitor link", "competitor links")

    @staticmethod
    def summarise(rows: list[tuple[str, str, str, int, bool]]) -> tuple[dict[str, Any], dict[str, Any], int]:
        """``(metrics, items, item_count)`` from ``(id, competitor, source_domain, authority, countered)`` rows. Pure."""
        metrics = {
            "alerts": len(rows),
            "countered": sum(1 for r in rows if r[4]),
            "competitors": len({r[1] for r in rows}),
            "high_authority": sum(1 for r in rows if int(r[3] or 0) >= HIGH_AUTHORITY),
            "window_days": ALERT_WINDOW_DAYS,
        }
        ordered = sorted(rows, key=lambda r: (-int(r[3] or 0), r[0]))
        items, total = store.cap_items(
            (r[0], [(r[1] or "")[:160], (r[2] or "")[:255], int(r[3] or 0), bool(r[4])]) for r in ordered
        )
        return metrics, items, total

    @staticmethod
    def build(db: Session, tenant_id: str, run: Any = None) -> tuple[dict[str, Any], dict[str, Any], int]:  # noqa: ANN401, ARG004
        since = utcnow() - timedelta(days=ALERT_WINDOW_DAYS)
        rows = db.execute(
            select(
                CompetitorAlert.id,
                CompetitorAlert.competitor,
                CompetitorAlert.source_domain,
                CompetitorAlert.authority,
                CompetitorAlert.counter_launched,
            ).where(CompetitorAlert.tenant_id == tenant_id, CompetitorAlert.detected_at >= since)
        ).all()
        return CompetitorScan.summarise([(r[0], r[1], r[2], r[3], r[4]) for r in rows])

    @staticmethod
    def has_data(db: Session, tenant_id: str) -> bool:
        return bool(
            db.scalar(select(func.count()).select_from(CompetitorAlert).where(CompetitorAlert.tenant_id == tenant_id))
        )

    @staticmethod
    def headline(metrics: dict[str, Any]) -> int | None:
        return metrics.get("alerts")

    @staticmethod
    def entry_metrics(metrics: dict[str, Any]) -> dict[str, Any]:
        return {
            "alerts": metrics.get("alerts"),
            "countered": metrics.get("countered"),
            "competitors": metrics.get("competitors"),
            "high_authority": metrics.get("high_authority"),
        }

    @staticmethod
    def compare(
        now_metrics: dict[str, Any],
        now_items: dict[str, Any],
        before_metrics: dict[str, Any],
        before_items: dict[str, Any],
    ) -> dict[str, Any]:
        """From the earlier version (``before``) to the later (``now``)."""
        new, fixed = [], []
        unchanged = 0
        for key in now_items:
            competitor, source, authority, countered = _state(now_items, key, 4)
            label = f"{competitor or 'A competitor'} got a link"
            if key not in before_items:
                new.append(
                    (
                        -int(authority or 0),
                        store.change_row(label, where=source, after=f"Site strength {int(authority or 0)}"),
                    )
                )
                continue
            if countered and not _state(before_items, key, 4)[3]:
                fixed.append(store.change_row(label, where=source, before="Not answered", after="Counter-pitch sent"))
            else:
                unchanged += 1
        new.sort(key=lambda pair: (pair[0], pair[1]["label"]))
        return store.comparison(
            noun=CompetitorScan.NOUN,
            new=[row for _rank, row in new],
            fixed=fixed,
            unchanged=unchanged,
            fixed_word="answered",
            measures=[
                store.measure("Competitor links found", before_metrics.get("alerts"), now_metrics.get("alerts"), better="lower"),
                store.measure("Answered with a pitch", before_metrics.get("countered"), now_metrics.get("countered"), better="higher"),
            ],
        )
