"""The daily rollup's metric points and backfill, and the Celery schedule (step 4.1).

Runs without a database: the rollup is driven with a fake session, bundle and
connectors, and the metric writers are captured.
"""
from __future__ import annotations

import logging
import math
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from app.connectors.base.connector import Capability
from app.connectors.base.interfaces import ChannelPerformance, SessionMetrics
from app.db.base import utcnow
from app.orchestration import scheduler
from app.services import metrics, social_metrics

TENANT_ID = "00000000-0000-0000-0000-0000000070f1"


# ── Fakes ──────────────────────────────────────────────────────────────────
class _Analytics:
    slug = "google_analytics_4"

    def __init__(self) -> None:
        self.requested: list[int] = []

    def supports(self, capability: Capability) -> bool:
        return capability is Capability.READ_SESSIONS

    def read_sessions(self, *, days: int = 30) -> list[SessionMetrics]:
        self.requested.append(days)
        today = utcnow().date()
        return [
            SessionMetrics(day=today - timedelta(days=offset), sessions=10, conversions=1)
            for offset in range(days, 0, -1)
        ]

    def close(self) -> None:
        return None


class _SearchConsole:
    slug = "search_console"

    def __init__(self, rows: list[Any] | None = None, *, fail: bool = False) -> None:
        self.requested: list[int] = []
        self.rows = rows
        self.fail = fail

    def supports(self, capability: Capability) -> bool:
        return capability is Capability.READ_SEARCH_PERFORMANCE

    def read_search_daily(self, *, days: int) -> list[Any]:
        self.requested.append(days)
        if self.fail:
            raise RuntimeError("quota: token=sk-live-not-for-logs")
        if self.rows is not None:
            return self.rows
        today = utcnow().date()
        return [
            {"day": today - timedelta(days=offset), "clicks": 5, "impressions": 50, "position": 7.5}
            for offset in range(days, 0, -1)
        ]

    def close(self) -> None:
        return None


class _SearchConsoleWithoutDaily:
    slug = "bing_webmaster"

    def supports(self, capability: Capability) -> bool:
        return capability is Capability.READ_SEARCH_PERFORMANCE

    def close(self) -> None:
        return None


class _Ads:
    def __init__(self, slug: str, performance: ChannelPerformance) -> None:
        self.slug = slug
        self.performance = performance

    def supports(self, capability: Capability) -> bool:
        return capability is Capability.READ_AD_PERFORMANCE

    def read_performance(self, *, days: int = 7) -> ChannelPerformance:
        return self.performance

    def close(self) -> None:
        return None


class _FakeSession:
    """``scalar`` answers the backfill check: ``seen`` sources already have points."""

    def __init__(self, seen: set[str]) -> None:
        self.seen = seen
        self.checks: list[str] = []

    def get(self, model: object, key: str) -> SimpleNamespace:
        return SimpleNamespace(id=key)

    def scalar(self, statement: Any) -> str | None:  # noqa: ANN401
        params = statement.compile().params
        source = params["source_1"]
        self.checks.append(f"{source}:{params['metric_1']}")
        assert params["tenant_id_1"] == TENANT_ID
        return "point-id" if source in self.seen else None

    def execute(self, statement: Any) -> None:  # noqa: ANN401
        raise AssertionError("writes go through the captured writers")

    def close(self) -> None:
        return None


@pytest.fixture
def run(monkeypatch: pytest.MonkeyPatch):  # noqa: ANN201
    """Run the real rollup for one workspace; returns (snapshots, points, session)."""
    from app.services import connectors as connector_service
    from app.services.connectors import ConnectorBundle

    def _run(*connectors: Any, seen: set[str] | None = None) -> tuple[list[dict], list, _FakeSession]:  # noqa: ANN401
        snapshots: list[dict[str, Any]] = []
        points: list[social_metrics.Point] = []
        session = _FakeSession(seen or set())

        monkeypatch.setattr(scheduler, "SessionLocal", lambda: session)
        monkeypatch.setattr(
            scheduler, "run_per_organization", lambda db, handler, **_: [handler(db, TENANT_ID)]
        )
        monkeypatch.setattr(
            connector_service,
            "build_connector_bundle",
            lambda db, org: ConnectorBundle(instances={c.slug: c for c in connectors}),
        )
        monkeypatch.setattr(
            metrics,
            "record_snapshot",
            lambda db, *, tenant_id, day, values: snapshots.append({"day": day, **values}),
        )

        def capture(db: object, *, tenant_id: str, points: Any) -> int:  # noqa: ANN401
            assert tenant_id == TENANT_ID
            batch = list(points)
            captured.extend(batch)
            return len(batch)

        captured = points
        monkeypatch.setattr(social_metrics, "record_points", capture)
        monkeypatch.setattr(scheduler, "_mark_failing", lambda db, tenant_id, slug, exc: failing.append(slug))
        failing.clear()
        scheduler.rollup_daily_metrics()
        return snapshots, points, session

    failing: list[str] = []
    _run.failing = failing  # type: ignore[attr-defined]
    return _run


# ── Backfill ───────────────────────────────────────────────────────────────
def test_a_new_analytics_source_is_backfilled_by_the_configured_days(
    monkeypatch: pytest.MonkeyPatch, run: Any  # noqa: ANN401
) -> None:
    monkeypatch.setattr(scheduler.settings, "metrics_backfill_days", 45)
    source = _Analytics()

    snapshots, points, session = run(source)

    assert source.requested == [45]
    assert len({s["day"] for s in snapshots}) == 45
    assert session.checks == ["google_analytics_4:sessions"]
    # Every day also lands in metric_points, which is what marks the source seen.
    assert {(p.source, p.metric) for p in points} == {
        ("google_analytics_4", "sessions"), ("google_analytics_4", "conversions"),
    }
    assert len([p for p in points if p.metric == "sessions"]) == 45


def test_a_source_seen_before_only_rereads_the_last_few_days(
    monkeypatch: pytest.MonkeyPatch, run: Any  # noqa: ANN401
) -> None:
    monkeypatch.setattr(scheduler.settings, "metrics_backfill_days", 45)
    source = _Analytics()

    snapshots, _points, _session = run(source, seen={"google_analytics_4"})

    assert source.requested == [3]
    assert len(snapshots) == 3


def test_a_backfill_of_zero_days_turns_it_off_without_a_query(
    monkeypatch: pytest.MonkeyPatch, run: Any  # noqa: ANN401
) -> None:
    monkeypatch.setattr(scheduler.settings, "metrics_backfill_days", 0)
    source = _Analytics()

    _snapshots, _points, session = run(source)

    assert source.requested == [3]
    assert session.checks == []


def test_a_failed_backfill_marks_the_connector_and_records_nothing(
    monkeypatch: pytest.MonkeyPatch, run: Any  # noqa: ANN401
) -> None:
    class _Broken(_Analytics):
        def read_sessions(self, *, days: int = 30) -> list[SessionMetrics]:
            raise RuntimeError("401")

    monkeypatch.setattr(scheduler.settings, "metrics_backfill_days", 30)
    snapshots, points, _session = run(_Broken())

    assert snapshots == [] and points == []
    # No points were written, so tomorrow it is still "new" and backfills again.
    assert run.failing == ["google_analytics_4"]


# ── Search Console ─────────────────────────────────────────────────────────
def test_search_console_daily_figures_become_points_never_sessions(
    monkeypatch: pytest.MonkeyPatch, run: Any  # noqa: ANN401
) -> None:
    monkeypatch.setattr(scheduler.settings, "metrics_backfill_days", 30)
    search = _SearchConsole()

    snapshots, points, session = run(search)

    assert snapshots == []  # clicks are not sessions
    assert search.requested == [30]
    assert session.checks == ["search_console:clicks"]
    assert {p.metric for p in points} == {"clicks", "impressions", "position"}
    assert {p.source for p in points} == {"search_console"}
    assert len({p.day for p in points}) == 30


def test_a_search_source_seen_before_rereads_a_week(monkeypatch: pytest.MonkeyPatch, run: Any) -> None:  # noqa: ANN401
    monkeypatch.setattr(scheduler.settings, "metrics_backfill_days", 30)
    search = _SearchConsole()
    run(search, seen={"search_console"})
    assert search.requested == [7]


def test_search_rows_from_the_connector_are_validated(monkeypatch: pytest.MonkeyPatch, run: Any) -> None:  # noqa: ANN401
    monkeypatch.setattr(scheduler.settings, "metrics_backfill_days", 0)
    rows = [
        {"day": "2026-10-01", "clicks": 3, "impressions": 30, "position": None},
        SimpleNamespace(day=date(2026, 10, 2), clicks=4, impressions=40, position=2.5),
        {"day": datetime(2026, 10, 3, 1, 0), "clicks": 1},  # a datetime is not a day
        {"day": "yesterday", "clicks": 1},
        {"day": "2026-10-04", "clicks": -1, "impressions": math.nan, "position": True},
        {"day": "2026-10-05", "clicks": "12"},
        "not a row",
    ]
    _snapshots, points, _session = run(_SearchConsole(rows))

    assert sorted((p.day.isoformat(), p.metric, p.value) for p in points) == [
        ("2026-10-01", "clicks", 3.0),
        ("2026-10-01", "impressions", 30.0),
        ("2026-10-02", "clicks", 4.0),
        ("2026-10-02", "impressions", 40.0),
        ("2026-10-02", "position", 2.5),
    ]


def test_a_search_source_without_daily_figures_is_skipped(run: Any) -> None:  # noqa: ANN401
    snapshots, points, session = run(_SearchConsoleWithoutDaily())
    assert snapshots == [] and points == [] and session.checks == []
    assert run.failing == []


def test_a_failing_search_source_does_not_stop_sessions_or_spend(
    monkeypatch: pytest.MonkeyPatch, run: Any, caplog: pytest.LogCaptureFixture  # noqa: ANN401
) -> None:
    monkeypatch.setattr(scheduler.settings, "metrics_backfill_days", 0)
    ads = _Ads("google_ads", ChannelPerformance(channel="google", spend=12.5))
    with caplog.at_level(logging.WARNING):
        snapshots, _points, _session = run(_Analytics(), _SearchConsole(fail=True), ads)

    assert run.failing == ["search_console"]
    assert any("organic_sessions" in s for s in snapshots)
    assert any(s.get("ad_spend") == 12.5 for s in snapshots)
    assert "sk-live-not-for-logs" not in caplog.text


def test_too_many_rows_from_a_connector_are_capped(monkeypatch: pytest.MonkeyPatch, run: Any) -> None:  # noqa: ANN401
    monkeypatch.setattr(scheduler.settings, "metrics_backfill_days", 0)
    monkeypatch.setattr(scheduler, "_MAX_DAILY_ROWS", 5)
    rows = [{"day": f"2026-09-{d:02d}", "clicks": 1} for d in range(1, 31)]
    _snapshots, points, _session = run(_SearchConsole(rows))
    assert len(points) == 5


# ── Ad channels ────────────────────────────────────────────────────────────
def test_each_ad_channel_gets_yesterdays_points_and_spend_is_still_summed(run: Any) -> None:  # noqa: ANN401
    google = _Ads("google_ads", ChannelPerformance(channel="google", spend=10.0, impressions=1000, clicks=50, conversions=2))
    meta = _Ads("meta_ads", ChannelPerformance(channel="meta", spend=5.25, impressions=400, clicks=8, conversions=0))

    snapshots, points, _session = run(google, meta)

    yesterday = utcnow().date() - timedelta(days=1)
    assert snapshots == [{"day": yesterday, "ad_spend": 15.25}]
    assert {(p.source, p.metric, p.value) for p in points} == {
        ("google_ads", "spend", 10.0), ("google_ads", "impressions", 1000.0),
        ("google_ads", "clicks", 50.0), ("google_ads", "conversions", 2.0),
        ("meta_ads", "spend", 5.25), ("meta_ads", "impressions", 400.0),
        ("meta_ads", "clicks", 8.0), ("meta_ads", "conversions", 0.0),
    }
    assert {p.day for p in points} == {yesterday}


def test_ad_figures_that_are_not_numbers_are_dropped(run: Any) -> None:  # noqa: ANN401
    odd = ChannelPerformance(channel="x", spend=1.0)
    odd.clicks = math.nan  # type: ignore[assignment]
    odd.impressions = -5
    odd.conversions = None  # type: ignore[assignment]
    _snapshots, points, _session = run(_Ads("tiktok_ads", odd))
    assert {(p.metric, p.value) for p in points} == {("spend", 1.0)}


# ── Celery schedule and heartbeat ─────────────────────────────────────────
def _beat() -> dict[str, dict[str, Any]]:
    from app.orchestration import tasks

    if not tasks.CELERY_AVAILABLE:
        pytest.skip("Celery is not installed")
    return {entry["task"]: entry for entry in tasks.celery_app.conf.beat_schedule.values()}


def test_beat_runs_every_housekeeping_task() -> None:
    assert set(_beat()) >= {
        "agents.tick", "metrics.rollup", "approvals.expire",
        "retention.prune", "social.metrics", "ops.beat_heartbeat",
    }


def test_daily_beat_entries_are_anchored_to_the_clock() -> None:
    from celery.schedules import crontab

    beat = _beat()
    for task in ("metrics.rollup", "approvals.expire", "retention.prune"):
        assert isinstance(beat[task]["schedule"], crontab), task
    rollup = beat["metrics.rollup"]["schedule"]
    assert rollup.minute == {20} and rollup.hour == {0}


def test_social_metrics_follow_the_configured_interval() -> None:
    from celery.schedules import crontab

    from app.core.config import settings
    from app.orchestration import tasks

    entry = _beat()["social.metrics"]["schedule"]
    hours = settings.social_metrics_interval_hours
    if hours <= 23:
        assert isinstance(entry, crontab)
        assert entry.hour == set(range(0, 24, hours))
    # A day or more is anchored to the clock too, never a bare interval.
    two_days = tasks._every_hours(48)
    assert isinstance(two_days, crontab)
    assert two_days.minute == {10} and two_days.hour == {0}


def test_the_heartbeat_runs_every_30_seconds_and_never_queues_a_backlog() -> None:
    entry = _beat()["ops.beat_heartbeat"]
    assert entry["schedule"] == 30.0
    assert entry["options"]["expires"] <= 30


class _FakeRedis:
    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[tuple[str, str, int]] = []
        self.closed = False
        self.fail = fail

    def set(self, key: str, value: str, *, ex: int) -> bool:
        if self.fail:
            import redis

            raise redis.ConnectionError("cannot reach redis://:hunter2@cache:6379/0")
        self.calls.append((key, value, ex))
        return True

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def fake_redis(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    redis = pytest.importorskip("redis")
    state: dict[str, Any] = {"client": _FakeRedis(), "urls": []}

    def from_url(url: str, **kwargs: Any) -> _FakeRedis:  # noqa: ANN401
        state["urls"].append(url)
        state["kwargs"] = kwargs
        return state["client"]

    monkeypatch.setattr(redis.Redis, "from_url", staticmethod(from_url))
    return state


def test_the_heartbeat_sets_the_key_with_a_two_minute_expiry(
    monkeypatch: pytest.MonkeyPatch, fake_redis: dict[str, Any]
) -> None:
    from app.orchestration import tasks

    if not tasks.CELERY_AVAILABLE:
        pytest.skip("Celery is not installed")
    monkeypatch.setattr(tasks.settings, "celery_enabled", True)
    monkeypatch.setattr(tasks.settings, "redis_url", "redis://cache:6379/2")

    before = int(utcnow().timestamp())
    assert tasks.beat_heartbeat_task() == {"status": "ok"}

    [(key, value, ttl)] = fake_redis["client"].calls
    assert key == "beat:hb" and ttl == 120
    assert before <= int(value) <= before + 5
    assert fake_redis["urls"] == ["redis://cache:6379/2"]
    assert fake_redis["kwargs"]["socket_timeout"] > 0
    assert fake_redis["client"].closed


def test_the_heartbeat_falls_back_to_a_redis_broker(
    monkeypatch: pytest.MonkeyPatch, fake_redis: dict[str, Any]
) -> None:
    from app.orchestration import tasks

    monkeypatch.setattr(tasks.settings, "celery_enabled", True)
    monkeypatch.setattr(tasks.settings, "redis_url", "")
    monkeypatch.setattr(tasks.settings, "celery_broker_url", "redis://broker:6379/0")
    assert tasks.beat_heartbeat_task() == {"status": "ok"}
    assert fake_redis["urls"] == ["redis://broker:6379/0"]


@pytest.mark.parametrize(
    ("celery_enabled", "redis_url", "broker"),
    [
        (False, "redis://cache:6379/0", "redis://cache:6379/0"),  # Celery off
        (True, "", "amqp://guest@rabbit//"),  # no Redis anywhere
        (True, "http://evil.example/", "amqp://guest@rabbit//"),  # not a Redis URL
    ],
    ids=["celery-off", "no-redis", "not-redis"],
)
def test_the_heartbeat_skips_quietly_without_celery_or_redis(
    monkeypatch: pytest.MonkeyPatch,
    fake_redis: dict[str, Any],
    caplog: pytest.LogCaptureFixture,
    celery_enabled: bool,
    redis_url: str,
    broker: str,
) -> None:
    from app.orchestration import tasks

    monkeypatch.setattr(tasks.settings, "celery_enabled", celery_enabled)
    monkeypatch.setattr(tasks.settings, "celery_broker_url", broker)
    monkeypatch.setattr(tasks.settings, "redis_url", redis_url)

    with caplog.at_level(logging.WARNING):
        assert tasks.beat_heartbeat_task() == {"status": "skipped"}
    assert fake_redis["urls"] == []
    assert caplog.text == ""


def test_a_redis_failure_is_reported_without_the_url_or_its_password(
    monkeypatch: pytest.MonkeyPatch, fake_redis: dict[str, Any], caplog: pytest.LogCaptureFixture
) -> None:
    from app.orchestration import tasks

    monkeypatch.setattr(tasks.settings, "celery_enabled", True)
    monkeypatch.setattr(tasks.settings, "redis_url", "redis://:hunter2@cache:6379/0")
    fake_redis["client"] = _FakeRedis(fail=True)

    with caplog.at_level(logging.WARNING):
        assert tasks.beat_heartbeat_task() == {"status": "failed"}
    assert "hunter2" not in caplog.text
    assert fake_redis["client"].closed
