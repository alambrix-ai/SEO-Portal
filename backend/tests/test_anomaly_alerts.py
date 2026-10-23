"""Anomaly threshold evaluation and dedupe."""
from __future__ import annotations

from datetime import date, timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session

from app.db.base import Base, new_id, utcnow
from app.models.alerts import AlertEvent, AlertRule
from app.models.metrics import DailyMetric
from app.services import anomaly_alerts as alerts
from tests.test_seo_pipeline_fixes import TENANT


@compiles(JSONB, "sqlite")
def _jsonb_on_sqlite(type_, compiler, **kw):  # noqa: ANN001, ANN003, ANN202
    return "JSON"


@compiles(PG_UUID, "sqlite")
def _uuid_on_sqlite(type_, compiler, **kw):  # noqa: ANN001, ANN003, ANN202
    return "CHAR(36)"


def test_sessions_drop_triggers() -> None:
    rule = AlertRule(
        id=new_id(),
        tenant_id=TENANT,
        name="Sessions",
        metric="organic_sessions_drop",
        threshold=0.3,
        channel="email",
        enabled="on",
    )
    prior = [
        DailyMetric(id=new_id(), tenant_id=TENANT, day=date(2026, 10, 1), organic_sessions=100)
    ]
    recent = [
        DailyMetric(id=new_id(), tenant_id=TENANT, day=date(2026, 10, 8), organic_sessions=40)
    ]
    hit = alerts.evaluate_rule(rule, recent=recent, prior=prior)
    assert hit is not None
    assert "fell" in hit.summary.lower()


def test_below_threshold_is_quiet() -> None:
    rule = AlertRule(
        id=new_id(),
        tenant_id=TENANT,
        name="Sessions",
        metric="organic_sessions_drop",
        threshold=0.5,
        channel="email",
        enabled="on",
    )
    prior = [
        DailyMetric(id=new_id(), tenant_id=TENANT, day=date(2026, 10, 1), organic_sessions=100)
    ]
    recent = [
        DailyMetric(id=new_id(), tenant_id=TENANT, day=date(2026, 10, 8), organic_sessions=80)
    ]
    assert alerts.evaluate_rule(rule, recent=recent, prior=prior) is None


def test_validate_rejects_private_webhook() -> None:
    with pytest.raises(Exception):  # noqa: BLE001 — InvalidInputError from netguard
        alerts.validate_rule_input(
            name="Hook",
            metric="ad_spend_spike",
            threshold=0.3,
            channel="webhook",
            webhook_url="http://127.0.0.1/hooks",
        )


def test_fire_rules_dedupes(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(
        engine, tables=[AlertRule.__table__, AlertEvent.__table__, DailyMetric.__table__]
    )
    today = utcnow().date()
    with Session(engine) as db:
        rule = AlertRule(
            id=new_id(),
            tenant_id=TENANT,
            name="Spend",
            metric="ad_spend_spike",
            threshold=0.2,
            channel="email",
            enabled="on",
        )
        db.add(rule)
        for i in range(7):
            db.add(
                DailyMetric(
                    id=new_id(),
                    tenant_id=TENANT,
                    day=today - timedelta(days=13 - i),
                    ad_spend=10.0,
                )
            )
            db.add(
                DailyMetric(
                    id=new_id(),
                    tenant_id=TENANT,
                    day=today - timedelta(days=6 - i),
                    ad_spend=50.0,
                )
            )
        db.flush()
        first = alerts.fire_rules(db, tenant_id=TENANT)
        second = alerts.fire_rules(db, tenant_id=TENANT)
        assert len(first) == 1
        assert second == []
        assert db.execute(select(AlertEvent)).scalars().all()
        assert len(list(db.execute(select(AlertEvent)).scalars())) == 1
    engine.dispose()


def test_alert_tables_under_rls() -> None:
    from app.db.rls import TENANT_SCOPED_TABLES

    assert "alert_rules" in TENANT_SCOPED_TABLES
    assert "alert_events" in TENANT_SCOPED_TABLES
