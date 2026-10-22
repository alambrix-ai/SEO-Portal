"""Why the Reports screen looks broken: the diagnosis, pinned (step 1.8).

The screen does not crash. ``GET /api/v1/reports`` answers 200 for every
range and every role that can see it, and its fields match what
``ReportsPage.tsx`` reads. What users report as "not working" is that the
screen is all zeros, or a trend that is partly invented:

* the response cannot tell "no data yet" from "measured zero", so a new
  workspace sees a page of zeros with no hint of what to connect;
* the trend pads a short series by repeating its last value, and ignores the
  calendar, so three days of data fill a 30-day chart edge to edge;
* the daily rollup only ever asks the analytics source for the last three
  days, so a 30- or 90-day range stays mostly empty after connecting;
* the Celery beat entry is a bare 24-hour interval, so it first fires a full
  day after every worker restart.

Each cause is pinned by a test that was a strict ``xfail`` until its fix
landed: the rollup and beat schedule in 4.1, the response and the trend in
5.5. All four are fixed and their markers are gone.

Everything here runs without a database: the endpoint is called through the
real app with its auth and session dependencies overridden, and the rollup
with fake sessions, bundles and connectors.
"""
from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.connectors.base.connector import Capability
from app.connectors.base.interfaces import SessionMetrics
from app.db.base import utcnow
from app.services import metrics

TENANT_ID = "00000000-0000-0000-0000-00000000d1a9"
FRONTEND_TYPES = (
    Path(__file__).resolve().parents[2] / "frontend" / "src" / "api" / "types.ts"
)


# ── Fakes ──────────────────────────────────────────────────────────────────
def _row(day: date, *, organic_sessions: int = 0, **counters: float) -> SimpleNamespace:
    """A stand-in for one ``DailyMetric`` row: every counter defaults to zero."""
    base: dict[str, Any] = {
        "day": day,
        "organic_sessions": organic_sessions,
        "aeo_citations": 0,
        "backlinks_won": 0,
        "ad_spend": 0.0,
        "fraud_blocked": 0,
        "referral_spam_blocked": 0,
        "pages_optimised": 0,
        "conversions": 0,
        "spend_saved": 0.0,
    }
    base.update(counters)
    return SimpleNamespace(**base)


def _current_user(*, role: str = "admin", enabled_modules: frozenset[str] | None = None):  # noqa: ANN202
    from app.api.deps import CurrentUser

    return CurrentUser(
        user=SimpleNamespace(role=role, email="viewer@example.test"),
        organization=SimpleNamespace(id=TENANT_ID),
        claims={},
        enabled_modules=enabled_modules,
    )


def _app(current: object) -> FastAPI:
    from app.api.deps import get_current_user
    from app.db.session import get_db
    from app.main import create_app
    from tests.conftest import _noop_lifespan

    app = create_app()
    app.router.lifespan_context = _noop_lifespan
    app.dependency_overrides[get_current_user] = lambda: current
    # Never reached: rows_for_range is replaced in every test that calls the
    # endpoint, so no query is ever issued.
    app.dependency_overrides[get_db] = lambda: object()
    return app


@pytest.fixture
def measured() -> metrics.Measured:
    """What analytics and ad sources measured. Nothing unless a test adds it."""
    return metrics.Measured()


@pytest.fixture
def rows(monkeypatch: pytest.MonkeyPatch, measured: metrics.Measured) -> list[SimpleNamespace]:
    """The daily rows the endpoint will see. Empty unless a test adds some.

    Website visits come from ``measured`` (``metric_points``), not from these
    rows: agents create rows whose ``organic_sessions`` is a default 0.
    """
    stored: list[SimpleNamespace] = []

    def fake_rows(db: object, *, tenant_id: str, range_key: str) -> list[SimpleNamespace]:
        assert tenant_id == TENANT_ID, "reports must read only the caller's workspace"
        days = metrics.RANGE_DAYS.get(range_key, 30)
        start = utcnow().date() - timedelta(days=days - 1)
        return sorted((r for r in stored if r.day >= start), key=lambda r: r.day)

    def fake_measured(db: object, *, tenant_id: str, range_key: str) -> metrics.Measured:
        assert tenant_id == TENANT_ID, "reports must read only the caller's workspace"
        days = metrics.RANGE_DAYS.get(range_key, 30)
        start = utcnow().date() - timedelta(days=days - 1)
        return metrics.Measured(
            sessions={d: v for d, v in measured.sessions.items() if d >= start}, ads=measured.ads
        )

    monkeypatch.setattr(metrics, "rows_for_range", fake_rows)
    monkeypatch.setattr(metrics, "measured_for_range", fake_measured)
    # The backlink tracker: none tracked unless a test says otherwise.
    monkeypatch.setattr(metrics, "backlinks_won", lambda db, *, tenant_id, range_key: None)
    return stored


@pytest.fixture
def client(rows: list[SimpleNamespace]) -> Iterator[TestClient]:  # noqa: ARG001
    with TestClient(_app(_current_user())) as test_client:
        yield test_client


def _frontend_report_fields() -> set[str]:
    source = FRONTEND_TYPES.read_text(encoding="utf-8")
    match = re.search(r"export interface ReportsOut \{(.*?)\n\}", source, re.S)
    assert match, "ReportsOut is no longer declared in frontend/src/api/types.ts"
    return set(re.findall(r"^\s+(\w+)\??:", match.group(1), re.M))


# ── Refuted: the endpoint itself is sound ──────────────────────────────────
@pytest.mark.parametrize("range_key", ["7d", "30d", "90d"])
def test_an_empty_workspace_gets_a_complete_report(client: TestClient, range_key: str) -> None:
    """No rows at all must still be a 200 with every tile, not a crash."""
    response = client.get(f"/api/v1/reports?range={range_key}")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["range"] == range_key
    assert body["organic_sessions"] == "0"
    # No ad account: "Not tracked yet", never a fake zero (and no division).
    assert body["blended_cac"] is None and body["blended_cac_note"] == metrics.NOT_TRACKED
    assert body["ad_spend"] is None and body["ad_spend_note"] == metrics.NOT_TRACKED
    assert body["trend"] == [0] * 6


@pytest.mark.skipif(not FRONTEND_TYPES.exists(), reason="frontend sources not present")
def test_the_response_carries_every_field_the_reports_screen_reads(client: TestClient) -> None:
    """A renamed or missing field would render ``undefined`` or throw in React."""
    body = client.get("/api/v1/reports?range=30d").json()

    missing = _frontend_report_fields() - set(body)
    assert not missing, f"ReportsPage reads fields the API does not send: {sorted(missing)}"
    # The two the page formats itself, and would throw on if absent.
    assert isinstance(body["referral_spam_blocked"], int)
    # Nullable since review cycle 1: present, and None when not tracked.
    assert "blended_cac" in body and body["blended_cac"] is None


@pytest.mark.parametrize("role", ["admin", "manager", "seo", "ads", "approver", "client"])
def test_every_role_can_open_reports(rows: list[SimpleNamespace], role: str) -> None:  # noqa: ARG001
    with TestClient(_app(_current_user(role=role))) as test_client:
        assert test_client.get("/api/v1/reports?range=90d").status_code == 200


@pytest.mark.parametrize("bad", ["all-time", "365d", "30d;drop", "7D", ""])
def test_an_unknown_range_is_rejected_with_a_plain_message(client: TestClient, bad: str) -> None:
    response = client.get("/api/v1/reports", params={"range": bad})

    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "validation_error"
    assert body["detail"] == "Check the highlighted fields"


def test_reports_switched_off_by_the_platform_answers_403_in_plain_words(
    rows: list[SimpleNamespace],  # noqa: ARG001
) -> None:
    current = _current_user(enabled_modules=frozenset({"dashboard"}))
    with TestClient(_app(current)) as test_client:
        response = test_client.get("/api/v1/reports?range=30d")

    assert response.status_code == 403
    detail = response.json()["detail"]
    assert "not enabled for your workspace" in detail
    assert "Traceback" not in detail and "Error" not in detail


# ── Confirmed causes ───────────────────────────────────────────────────────
def test_an_empty_report_says_it_has_no_data(
    client: TestClient, rows: list[SimpleNamespace], measured: metrics.Measured
) -> None:
    """Root cause A, fixed in 5.5: "no data yet" is not a measured zero."""
    empty = client.get("/api/v1/reports?range=30d").json()
    assert empty["available"] is False
    assert empty["connect_hint"], "an empty report should say what to connect"

    yesterday = utcnow().date() - timedelta(days=1)
    rows.append(_row(yesterday, organic_sessions=120))
    measured.sessions[yesterday] = 120
    body = client.get("/api/v1/reports?range=30d").json()
    assert body["available"] is True
    assert body["connect_hint"] == ""


def test_untracked_backlinks_are_not_reported_as_zero(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing writes daily_metrics.backlinks_won; the tracker is the source."""
    body = client.get("/api/v1/reports?range=30d").json()
    assert body["backlinks_won"] is None
    assert body["backlinks_won_note"] == metrics.NOT_TRACKED

    monkeypatch.setattr(metrics, "backlinks_won", lambda db, *, tenant_id, range_key: 0)
    body = client.get("/api/v1/reports?range=30d").json()
    assert body["backlinks_won"] == 0  # tracked, and none won: a real zero
    assert body["backlinks_won_note"] == ""


def test_three_recent_days_do_not_fill_a_thirty_day_trend(
    client: TestClient, measured: metrics.Measured
) -> None:
    """Root cause B, fixed in 5.5: every calendar day is a slot, then bucketed."""
    today = utcnow().date()
    # Exactly what one run of the daily rollup leaves behind.
    for offset, sessions in ((3, 100), (2, 200), (1, 300)):
        measured.sessions[today - timedelta(days=offset)] = sessions

    body = client.get("/api/v1/reports?range=30d").json()
    trend = body["trend"]

    # Before the fix: [100, 200, 300, 300, 300, 300] — two invented points,
    # and the oldest bucket showed traffic from three days ago.
    assert trend[:5] == [0, 0, 0, 0, 0], trend
    assert trend[5] > 0, trend
    # The last bucket is days -4..0: (0 + 100 + 200 + 300 + 0) / 5.
    assert trend[5] == 120
    # Unmeasured buckets are gaps, not zeros; the measured one averages
    # only the days that have data.
    assert body["trend_with_gaps"] == [None, None, None, None, None, 200]
    assert body["trend_days"] == 30


def test_the_trend_buckets_whole_calendar_ranges() -> None:
    today = date(2026, 3, 31)
    daily = metrics.calendar_series({today: 7, today - timedelta(days=89): 3}, start=today - timedelta(days=89), days=90)
    assert len(daily) == 90 and daily[0] == 3 and daily[-1] == 7
    assert daily.count(None) == 88
    assert metrics._bucket_gaps(daily) == [3, None, None, None, None, 7]
    assert len(metrics._bucket_trend([None] * 7)) == 6
    # Shorter than the bucket count: padded with gaps at the old end, never
    # by repeating the last value.
    assert metrics._bucket_gaps([5, 6]) == [None, None, None, None, 5, 6]
    assert metrics._bucket_trend([5, 6]) == [0, 0, 0, 0, 5, 6]


# ── The rollup that feeds daily_metrics ────────────────────────────────────
class _FakeSessionsSource:
    """An analytics connector with 90 days of organic history."""

    slug = "google_analytics_4"

    def __init__(self) -> None:
        self.requested_days: list[int] = []

    def supports(self, capability: Capability) -> bool:
        return capability is Capability.READ_SESSIONS

    def read_sessions(self, *, days: int = 30) -> list[SessionMetrics]:
        self.requested_days.append(days)
        today = utcnow().date()
        return [
            SessionMetrics(day=today - timedelta(days=offset), sessions=50)
            for offset in range(days, 0, -1)
        ]

    def close(self) -> None:
        return None


class _FakeSearchConsole(_FakeSessionsSource):
    slug = "search_console"

    def supports(self, capability: Capability) -> bool:
        return capability is Capability.READ_SEARCH_PERFORMANCE


def _run_rollup(
    monkeypatch: pytest.MonkeyPatch, source: _FakeSessionsSource
) -> list[dict[str, Any]]:
    """Run the real rollup for one workspace and capture what it records."""
    from app.orchestration import scheduler
    from app.services import connectors as connector_service
    from app.services.connectors import ConnectorBundle

    recorded: list[dict[str, Any]] = []

    class _FakeSession:
        def get(self, model: object, key: str) -> object:
            return SimpleNamespace(id=key)

        def scalar(self, statement: object) -> None:
            # The backfill check: no metric points yet, so every source is new.
            return None

        def execute(self, statement: object) -> None:
            # metric_points upserts (step 4.1); not what these tests inspect.
            return None

        def close(self) -> None:
            return None

    monkeypatch.setattr(scheduler, "SessionLocal", _FakeSession)
    monkeypatch.setattr(
        scheduler,
        "run_per_organization",
        lambda db, handler, **_: [handler(db, TENANT_ID)],
    )
    monkeypatch.setattr(
        connector_service,
        "build_connector_bundle",
        lambda db, org: ConnectorBundle(instances={source.slug: source}),  # type: ignore[dict-item]
    )
    monkeypatch.setattr(
        metrics,
        "record_snapshot",
        lambda db, *, tenant_id, day, values: recorded.append(
            {"tenant_id": tenant_id, "day": day, **values}
        ),
    )
    scheduler.rollup_daily_metrics()
    return recorded


def test_the_rollup_records_sessions_from_a_connected_analytics_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorded = _run_rollup(monkeypatch, _FakeSessionsSource())

    assert recorded, "a connected analytics source must produce daily rows"
    assert {r["tenant_id"] for r in recorded} == {TENANT_ID}
    assert all(r["organic_sessions"] == 50 for r in recorded)


def test_search_console_alone_never_feeds_the_reports_screen(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Search Console clicks are not sessions, and nothing else carries them.

    A workspace whose only analytics connection is Search Console therefore
    has an empty Reports screen however long it waits. Pinned as current
    behaviour; 4.1 adds Search Console points and 5.5 shows them in Insights.
    """
    source = _FakeSearchConsole()
    recorded = _run_rollup(monkeypatch, source)

    assert recorded == []
    assert source.requested_days == []


def test_the_first_rollup_after_connecting_backfills_the_longest_range(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Root cause C, fixed in 4.1: the first rollup backfills.

    It reads ``metrics_backfill_days``; set here to the longest Reports range,
    which is what a deployment needs for the 90-day view to be full.
    """
    from app.orchestration import scheduler

    longest = max(metrics.RANGE_DAYS.values())
    monkeypatch.setattr(scheduler.settings, "metrics_backfill_days", longest)
    source = _FakeSessionsSource()
    recorded = _run_rollup(monkeypatch, source)

    assert max(source.requested_days) >= longest, source.requested_days
    assert len({r["day"] for r in recorded}) >= longest


def test_the_celery_rollup_is_anchored_to_the_clock() -> None:
    """Root cause D, fixed in 4.1: 00:20 UTC, not 24 hours after a restart."""
    from app.orchestration import tasks

    if not tasks.CELERY_AVAILABLE:
        pytest.skip("Celery is not installed")
    from celery.schedules import crontab

    entry = next(
        value
        for value in tasks.celery_app.conf.beat_schedule.values()
        if value["task"] == "metrics.rollup"
    )
    assert isinstance(entry["schedule"], crontab), entry["schedule"]
    assert entry["schedule"].minute == {20} and entry["schedule"].hour == {0}
