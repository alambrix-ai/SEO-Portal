"""Threshold anomaly detection with per-day dedupe and egress-safe webhooks."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core import netguard
from app.core.logging import get_logger
from app.db.base import utcnow
from app.models.alerts import AlertEvent, AlertRule
from app.models.metrics import DailyMetric

log = get_logger(__name__)

ALLOWED_METRICS = frozenset(
    {"organic_sessions_drop", "ad_spend_spike", "conversions_drop"}
)
ALLOWED_CHANNELS = frozenset({"email", "slack", "webhook"})


@dataclass(frozen=True, slots=True)
class AnomalyHit:
    metric: str
    summary: str
    evidence: dict[str, Any]
    dedupe_key: str


def _window_totals(rows: list[DailyMetric]) -> dict[str, float]:
    return {
        "organic_sessions": float(sum(int(r.organic_sessions or 0) for r in rows)),
        "ad_spend": float(sum(float(r.ad_spend or 0) for r in rows)),
        "conversions": float(sum(int(r.conversions or 0) for r in rows)),
    }


def evaluate_rule(
    rule: AlertRule,
    *,
    recent: list[DailyMetric],
    prior: list[DailyMetric],
) -> AnomalyHit | None:
    """Compare the last 7 days to the prior 7 days for one rule."""
    if rule.metric not in ALLOWED_METRICS:
        return None
    threshold = float(rule.threshold or 0.3)
    if threshold <= 0 or threshold >= 1:
        threshold = 0.3
    now = _window_totals(recent)
    before = _window_totals(prior)
    day_key = utcnow().date().isoformat()

    if rule.metric == "organic_sessions_drop":
        base = before["organic_sessions"]
        cur = now["organic_sessions"]
        if base <= 0:
            return None
        drop = (base - cur) / base
        if drop < threshold:
            return None
        return AnomalyHit(
            metric=rule.metric,
            summary=f"Organic sessions fell {drop:.0%} vs prior week",
            evidence={"prior": base, "recent": cur, "threshold": threshold},
            dedupe_key=f"{rule.id}:{rule.metric}:{day_key}",
        )

    if rule.metric == "ad_spend_spike":
        base = before["ad_spend"]
        cur = now["ad_spend"]
        if base <= 0:
            return None
        spike = (cur - base) / base
        if spike < threshold:
            return None
        return AnomalyHit(
            metric=rule.metric,
            summary=f"Ad spend rose {spike:.0%} vs prior week",
            evidence={"prior": base, "recent": cur, "threshold": threshold},
            dedupe_key=f"{rule.id}:{rule.metric}:{day_key}",
        )

    if rule.metric == "conversions_drop":
        base = before["conversions"]
        cur = now["conversions"]
        if base <= 0:
            return None
        drop = (base - cur) / base
        if drop < threshold:
            return None
        return AnomalyHit(
            metric=rule.metric,
            summary=f"Conversions fell {drop:.0%} vs prior week",
            evidence={"prior": base, "recent": cur, "threshold": threshold},
            dedupe_key=f"{rule.id}:{rule.metric}:{day_key}",
        )
    return None


def validate_rule_input(
    *,
    name: str,
    metric: str,
    threshold: float,
    channel: str,
    webhook_url: str = "",
) -> dict[str, Any]:
    from app.core.exceptions import InvalidInputError

    name = (name or "").strip()[:120]
    if len(name) < 2:
        raise InvalidInputError("Rule name is required")
    if metric not in ALLOWED_METRICS:
        raise InvalidInputError(f"Unsupported metric: {metric}")
    if channel not in ALLOWED_CHANNELS:
        raise InvalidInputError(f"Unsupported channel: {channel}")
    url = (webhook_url or "").strip()
    if channel == "webhook":
        if not url:
            raise InvalidInputError("Webhook channel requires a URL")
        # Public HTTPS only — blocks private/link-local and non-http schemes.
        netguard.check_public_url(url)
    return {
        "name": name,
        "metric": metric,
        "threshold": float(threshold),
        "channel": channel,
        "webhook_url": url[:512] if channel == "webhook" else "",
    }


def load_metric_windows(
    db: Session, *, tenant_id: str
) -> tuple[list[DailyMetric], list[DailyMetric]]:
    today = utcnow().date()
    recent_start = today - timedelta(days=7)
    prior_start = today - timedelta(days=14)
    rows = list(
        db.execute(
            select(DailyMetric)
            .where(
                DailyMetric.tenant_id == tenant_id,
                DailyMetric.day >= prior_start,
            )
            .order_by(DailyMetric.day.asc())
        ).scalars()
    )
    recent = [r for r in rows if r.day >= recent_start]
    prior = [r for r in rows if prior_start <= r.day < recent_start]
    return recent, prior


def fire_rules(db: Session, *, tenant_id: str) -> list[AlertEvent]:
    """Evaluate enabled rules; insert events only when dedupe key is new."""
    rules = list(
        db.execute(
            select(AlertRule).where(
                AlertRule.tenant_id == tenant_id,
                AlertRule.enabled == "on",
            )
        ).scalars()
    )
    if not rules:
        return []
    recent, prior = load_metric_windows(db, tenant_id=tenant_id)
    created: list[AlertEvent] = []
    for rule in rules:
        hit = evaluate_rule(rule, recent=recent, prior=prior)
        if hit is None:
            continue
        existing = db.execute(
            select(AlertEvent.id).where(
                AlertEvent.tenant_id == tenant_id,
                AlertEvent.dedupe_key == hit.dedupe_key,
            )
        ).scalar_one_or_none()
        if existing is not None:
            continue
        event = AlertEvent(
            tenant_id=tenant_id,
            rule_id=rule.id,
            metric=hit.metric,
            summary=hit.summary,
            evidence=hit.evidence,
            dedupe_key=hit.dedupe_key,
            fired_at=utcnow(),
            status="open",
        )
        db.add(event)
        created.append(event)
        if rule.channel == "webhook" and rule.webhook_url:
            try:
                deliver_webhook(rule.webhook_url, hit.summary)
            except Exception as exc:  # noqa: BLE001
                log.warning("Alert webhook failed for rule %s: %s", rule.id, exc)
    db.flush()
    return created


def deliver_webhook(url: str, body: str) -> None:
    """POST alert text only after ``check_public_url`` (blocks private hosts)."""
    from app.core import egress

    checked = netguard.check_public_url(url)
    with egress.pinned_client(timeout=5.0, headers={"User-Agent": "Synaprank/1.0 (+alerts)"}) as client:
        egress.fetch(
            client,
            "POST",
            checked.strip(),
            content=body.encode("utf-8"),
            headers={"Content-Type": "text/plain; charset=utf-8"},
            require_https=False,
            max_bytes=65_536,
            truncate=True,
        )
