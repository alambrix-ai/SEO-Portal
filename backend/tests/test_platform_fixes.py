"""Cross-cutting fixes: settings, housekeeping, measured metrics, earned counters."""
from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy.dialects import postgresql

from app.agents.base.contracts import AgentAction
from app.agents.base.plain import SettingField
from app.orchestration import scheduler as scheduler_module
from app.services import approvals, metrics


def test_declared_settings_are_validated_with_plain_reasons():
    words = SettingField("thin_words", "Thin content below (words)", kind="integer", minimum=50, maximum=5000)
    assert words.clean(" 400 ") == "400"
    assert words.clean("") == ""
    with pytest.raises(ValueError, match="at least 50"):
        words.clean("10")
    with pytest.raises(ValueError, match="must be a number"):
        words.clean("lots")
    relevance = SettingField("min_relevance", "Minimum relevance", kind="number", minimum=0, maximum=1)
    assert relevance.clean("0.75") == "0.75"
    with pytest.raises(ValueError, match="at most 1"):
        relevance.clean("2")


def test_every_setting_key_an_agent_reads_is_declared():
    """The keys agents read with ctx.setting() must be settable from setup."""
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parent.parent / "app" / "agents"
    from app.agents.base.plain import guide_for
    from app.agents.base.registry import all_agents

    for slug, guide in ((a.slug, guide_for(a.slug)) for a in all_agents()):
        source = "".join(p.read_text(encoding="utf-8") for p in (root / slug).glob("*.py"))
        read = set(re.findall(r'ctx\.setting\(\s*"([a-z_]+)"', source))
        declared = {field.key for field in guide.settings} | (
            {guide.scope_required_setting} if guide.scope_required_setting else set()
        )
        assert read <= declared, (slug, read - declared)


def test_daily_housekeeping_runs_once_a_day(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(scheduler_module, "rollup_daily_metrics", lambda: calls.append("rollup"))
    monkeypatch.setattr(scheduler_module, "expire_stale_approvals", lambda: calls.append("expire"))
    monkeypatch.setattr(scheduler_module.settings, "celery_enabled", False)
    sched = scheduler_module.AgentScheduler(tick_seconds=30)
    sched._maybe_daily_housekeeping()
    sched._maybe_daily_housekeeping()
    assert calls == ["rollup", "expire"]


def test_snapshots_replace_the_days_value_instead_of_adding():
    captured: list = []

    class _Db:
        def execute(self, statement):  # noqa: ANN001
            captured.append(statement)

    metrics.record_snapshot(_Db(), tenant_id="t", day=date(2026, 10, 4), values={"ad_spend": 120.5, "agent_runs": 3})
    sql = str(captured[0].compile(dialect=postgresql.dialect()))
    update = sql.split("DO UPDATE SET", 1)[1]
    assert "ad_spend" in update
    assert "agent_runs" not in update  # counters are never snapshots
    assert "daily_metrics.ad_spend +" not in update  # replaced, not added to


def test_earned_counters_ride_with_a_queued_change():
    class _Cipher:
        def encrypt_json(self, value, *, context):  # noqa: ANN001
            captured.append(value)
            return "enc"

    class _Db:
        def add(self, row) -> None:  # noqa: ANN001
            pass

        def flush(self) -> None:
            pass

    captured: list = []
    action = AgentAction(kind="block", title="Block x", target_kind="t", target_id="x", payload={"a": 1}, metrics={"fraud_blocked": 1})
    original = approvals.find_open_proposal
    approvals.find_open_proposal = lambda *a, **k: None
    try:
        record = type("R", (), {"slug": "s", "name": "S"})()
        org = type("O", (), {"id": "o"})()
        approvals.queue_action(_Db(), org=org, record=record, action=action, cipher=_Cipher())
    finally:
        approvals.find_open_proposal = original
    assert captured[0] == {"a": 1, approvals.METRICS_KEY: {"fraud_blocked": 1}}
    assert action.payload == {"a": 1}  # the action itself is untouched


def test_every_counter_has_a_column():
    """A counter without a DailyMetric column fails the whole run's upsert on Postgres."""
    from app.models.metrics import DailyMetric

    columns = set(DailyMetric.__table__.columns.keys())
    assert metrics._COUNTERS <= columns, metrics._COUNTERS - columns
    assert metrics._SNAPSHOTS <= columns


def test_unreported_engagement_is_not_suspicious():
    """Two weak signals plus 'no time on site' used to tip every Google placement."""
    from app.agents.click_fraud_controller import detectors
    from app.connectors.base.interfaces import TrafficSample

    weak = {"fingerprint_repeat_count": 5, "clicks_per_minute": 12}
    measured = TrafficSample(source="s.example", sessions=4, signals=weak)
    unreported = TrafficSample(source="s.example", sessions=4, signals=weak, engagement_known=False)
    assert detectors.assess(measured)[0] is detectors.Verdict.UNCERTAIN
    assert detectors.assess(unreported)[0] is detectors.Verdict.CLEAN


def test_microsoft_ads_is_a_budget_channel_that_starts_at_zero():
    from app.models.ads import CHANNEL_CONNECTOR, AdChannel
    from app.services.provisioning import _INITIAL_SHARE

    assert CHANNEL_CONNECTOR[AdChannel.MICROSOFT.value] == "microsoft_ads"
    assert sum(_INITIAL_SHARE.values()) == 100
    assert _INITIAL_SHARE[AdChannel.MICROSOFT.value] == 0


def test_a_fraud_block_no_platform_accepted_is_not_recorded_as_blocked(monkeypatch):
    from types import SimpleNamespace

    from app.agents.click_fraud_controller import agent as fraud
    from app.connectors.base.interfaces import TrafficSample
    from app.core.exceptions import ConnectorError

    class _Refuses:
        name, slug = "Google Ads", "google_ads"

        def block_placement(self, *, source, reason):  # noqa: ANN001
            raise ConnectorError("quota")

    added: list = []
    ctx = SimpleNamespace(
        tenant_id="t",
        db=SimpleNamespace(add=added.append, flush=lambda: None),
        log_warning=lambda *a, **k: None,
    )
    sample = TrafficSample(source="bad.example", channel="google")
    monkeypatch.setattr(fraud, "_block_targets", lambda ctx, channel: [_Refuses()])
    with pytest.raises(ConnectorError, match="could not be blocked"):
        fraud._make_blocker(sample, "bot", 0.9, 1.0)(ctx)
    assert added == []

    monkeypatch.setattr(fraud, "_block_targets", lambda ctx, channel: [])
    fraud._make_blocker(sample, "bot", 0.9, 1.0)(ctx)
    assert added[0].action == "Recorded only"
