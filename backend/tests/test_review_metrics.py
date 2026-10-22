"""Review-fix cycle 1, group G3b: metrics, Reports, social metrics, beat.

* Reports availability and trend come from *measured* website visits
  (``metric_points`` sessions from analytics sources, never Business
  Profile). A row an agent run created is not a measurement.
* Ad spend and cost per conversion are ``None`` with a note when no ad
  account was measured, never a fake zero.
* The daily rollup records Business Profile as ``local_impressions``.
* Social metrics record no daily total for a network read only in part.
* Beat sweeps stuck creative jobs every five minutes, and a day-or-longer
  interval is a crontab.

Runs without a database: endpoints with overridden dependencies, the rollup
and collection with fakes.
"""
from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.dialects import postgresql
from sqlalchemy.sql import Select

from app.connectors.base.connector import Capability
from app.connectors.base.interfaces import SessionMetrics
from app.db.base import utcnow
from app.services import metrics, social_metrics

TENANT_ID = "00000000-0000-0000-0000-00000000a3b1"


# ── Reports: helpers ───────────────────────────────────────────────────────
def _row(day: date, **counters: float) -> SimpleNamespace:
    """One ``DailyMetric`` stand-in; every counter defaults to zero."""
    base: dict[str, Any] = {
        "day": day, "organic_sessions": 0, "aeo_citations": 0, "backlinks_won": 0,
        "ad_spend": 0.0, "fraud_blocked": 0, "referral_spam_blocked": 0,
        "pages_optimised": 0, "conversions": 0, "spend_saved": 0.0,
        "agent_runs": 0, "actions_approved": 0,
    }
    base.update(counters)
    return SimpleNamespace(**base)


def _app() -> FastAPI:
    from app.api.deps import CurrentUser, get_current_user
    from app.db.session import get_db
    from app.main import create_app
    from tests.conftest import _noop_lifespan

    current = CurrentUser(
        user=SimpleNamespace(role="admin", email="viewer@example.test"),
        organization=SimpleNamespace(id=TENANT_ID, global_autonomy=False),
        claims={},
        enabled_modules=None,
    )
    app = create_app()
    app.router.lifespan_context = _noop_lifespan
    app.dependency_overrides[get_current_user] = lambda: current
    app.dependency_overrides[get_db] = lambda: object()  # never queried
    return app


@pytest.fixture
def state(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Daily rows and measured figures the endpoint sees; empty by default."""
    data = SimpleNamespace(rows=[], measured=metrics.Measured())

    def fake_rows(
        db: object, *, tenant_id: str, range_key: str, today: date | None = None
    ) -> list[SimpleNamespace]:
        assert tenant_id == TENANT_ID
        return list(data.rows)

    def fake_measured(
        db: object, *, tenant_id: str, range_key: str, today: date | None = None
    ) -> metrics.Measured:
        assert tenant_id == TENANT_ID
        return data.measured

    monkeypatch.setattr(metrics, "rows_for_range", fake_rows)
    monkeypatch.setattr(metrics, "measured_for_range", fake_measured)
    monkeypatch.setattr(metrics, "backlinks_won", lambda db, *, tenant_id, range_key: None)
    return data


@pytest.fixture
def client(state: SimpleNamespace) -> Iterator[TestClient]:  # noqa: ARG001
    with TestClient(_app()) as test_client:
        yield test_client


# ── Reports: measured visits only ──────────────────────────────────────────
def test_rows_from_agent_runs_only_are_not_data(client: TestClient, state: SimpleNamespace) -> None:
    """The review's case: agent runs and approvals create rows, sessions 0."""
    today = utcnow().date()
    state.rows.extend(
        _row(today - timedelta(days=offset), agent_runs=4, actions_approved=1)
        for offset in range(5)
    )

    body = client.get("/api/v1/reports?range=30d").json()

    assert body["available"] is False
    assert body["connect_hint"] == metrics.REPORTS_CONNECT_HINT
    assert body["trend_with_gaps"] == [None] * 6
    assert body["organic_sessions"] == "0"


def test_old_session_figures_in_daily_rows_are_not_a_measurement(
    client: TestClient, state: SimpleNamespace
) -> None:
    """Rows once filled from Business Profile impressions must not show as visits."""
    state.rows.append(_row(utcnow().date() - timedelta(days=1), organic_sessions=900))

    body = client.get("/api/v1/reports?range=7d").json()

    assert body["available"] is False
    assert body["organic_sessions"] == "0"
    assert body["trend_with_gaps"] == [None] * 6


def test_a_measured_zero_is_data_and_an_unmeasured_day_is_a_gap(
    client: TestClient, state: SimpleNamespace
) -> None:
    yesterday = utcnow().date() - timedelta(days=1)
    state.measured.sessions[yesterday] = 0

    body = client.get("/api/v1/reports?range=30d").json()

    assert body["available"] is True and body["connect_hint"] == ""
    assert body["trend_with_gaps"] == [None, None, None, None, None, 0]


def test_measured_visits_are_summed_for_the_tile(client: TestClient, state: SimpleNamespace) -> None:
    today = utcnow().date()
    state.measured.sessions.update({today - timedelta(days=2): 1500, today - timedelta(days=1): 500})
    assert client.get("/api/v1/reports?range=7d").json()["organic_sessions"] == "2.0K"


# ── Reports: ad figures ────────────────────────────────────────────────────
def test_no_ad_account_means_not_tracked_not_zero(client: TestClient, state: SimpleNamespace) -> None:
    # An agent counter alone is not a measured ad account.
    state.rows.append(_row(utcnow().date(), ad_spend=50.0, conversions=5))

    body = client.get("/api/v1/reports?range=30d").json()

    assert body["ad_spend"] is None and body["ad_spend_note"] == metrics.NOT_TRACKED
    assert body["blended_cac"] is None and body["blended_cac_note"] == metrics.NOT_TRACKED


def test_measured_ads_give_spend_and_cost_per_conversion(
    client: TestClient, state: SimpleNamespace
) -> None:
    state.measured.ads = True
    state.rows.append(_row(utcnow().date() - timedelta(days=1), ad_spend=1000.0, conversions=8))

    body = client.get("/api/v1/reports?range=30d").json()

    assert body["ad_spend"].startswith("₹") and body["ad_spend_note"] == ""
    assert body["blended_cac"] == 125.0 and body["blended_cac_note"] == ""


def test_measured_ads_with_no_conversions_have_no_cost_per_conversion(
    client: TestClient, state: SimpleNamespace
) -> None:
    state.measured.ads = True
    state.rows.append(_row(utcnow().date() - timedelta(days=1), ad_spend=300.0))

    body = client.get("/api/v1/reports?range=30d").json()

    assert body["ad_spend"].startswith("₹")
    assert body["blended_cac"] is None and body["blended_cac_note"] == metrics.NO_CONVERSIONS


def test_the_dashboard_still_renders_with_nothing_measured(
    monkeypatch: pytest.MonkeyPatch, state: SimpleNamespace  # noqa: ARG001
) -> None:
    from app.services import approvals, connectors, views

    monkeypatch.setattr(views, "list_agents", lambda db, **_: [])
    monkeypatch.setattr(connectors, "counts", lambda db, **_: (0, 0))
    monkeypatch.setattr(connectors, "list_records", lambda db, **_: [])
    monkeypatch.setattr(approvals, "pending_count", lambda db, **_: 0)
    monkeypatch.setattr(approvals, "list_pending", lambda db, **_: [])
    with TestClient(_app()) as test_client:
        response = test_client.get("/api/v1/dashboard")
    assert response.status_code == 200, response.text
    assert response.json()["kpis"]["organic_sessions_raw"] == 0


# ── measured_for_range: the query ──────────────────────────────────────────
class _QueryDb:
    def __init__(self, rows: list[tuple[str, str, date, float]]) -> None:
        self.rows = rows
        self.statements: list[Select] = []

    def execute(self, statement: Select) -> SimpleNamespace:
        self.statements.append(statement)
        return SimpleNamespace(all=lambda: list(self.rows))


def test_the_measured_query_is_tenant_filtered_and_skips_business_profile() -> None:
    db = _QueryDb([])
    metrics.measured_for_range(db, tenant_id=TENANT_ID, range_key="7d")  # type: ignore[arg-type]

    [statement] = db.statements
    compiled = statement.compile(dialect=postgresql.dialect())
    params = compiled.params
    assert params["tenant_id_1"] == TENANT_ID
    sources = set(params["source_1"])
    assert "google_analytics_4" in sources and "google_ads" in sources
    assert "google_business_profile" not in sources
    assert set(params["metric_1"]) == {"sessions", "spend"}
    assert params["day_1"] == utcnow().date() - timedelta(days=6)


def test_measured_rows_take_the_larger_source_per_day_and_never_business_profile() -> None:
    day = utcnow().date() - timedelta(days=1)
    db = _QueryDb(
        [
            ("google_analytics_4", "sessions", day, 10.0),
            ("adobe_analytics", "sessions", day, 30.0),  # same day, switched source
            ("google_business_profile", "sessions", day, 9999.0),  # never visits
            ("google_ads", "clicks", day, 4.0),  # not spend: not a measurement
            ("google_analytics_4", "sessions", day - timedelta(days=1), -3.0),
        ]
    )
    out = metrics.measured_for_range(db, tenant_id=TENANT_ID, range_key="30d")  # type: ignore[arg-type]

    assert out.sessions == {day: 30, day - timedelta(days=1): 0}
    assert out.ads is False

    db.rows.append(("meta_ads", "spend", day, 0.0))
    assert metrics.measured_for_range(db, tenant_id=TENANT_ID, range_key="30d").ads is True  # type: ignore[arg-type]


# ── Rollup: Business Profile is local impressions ──────────────────────────
class _SessionsSource:
    def __init__(self, slug: str, sessions: int, *, fail: bool = False) -> None:
        self.slug = slug
        self.sessions = sessions
        self.fail = fail
        self.requested: list[int] = []

    def supports(self, capability: Capability) -> bool:
        return capability is Capability.READ_SESSIONS

    def read_sessions(self, *, days: int = 30) -> list[SessionMetrics]:
        self.requested.append(days)
        if self.fail:
            raise RuntimeError("quota: token=sk-live-not-for-logs")
        today = utcnow().date()
        return [
            SessionMetrics(day=today - timedelta(days=offset), sessions=self.sessions, conversions=1)
            for offset in range(days, 0, -1)
        ]

    def close(self) -> None:
        return None


class _RollupSession:
    def __init__(self) -> None:
        self.checks: list[str] = []

    def get(self, model: object, key: str) -> SimpleNamespace:
        return SimpleNamespace(id=key)

    def scalar(self, statement: Any) -> None:  # noqa: ANN401
        params = statement.compile().params
        assert params["tenant_id_1"] == TENANT_ID
        self.checks.append(f"{params['source_1']}:{params['metric_1']}")
        return None

    def execute(self, statement: Any) -> None:  # noqa: ANN401
        raise AssertionError("writes go through the captured writers")

    def close(self) -> None:
        return None


@pytest.fixture
def rollup(monkeypatch: pytest.MonkeyPatch):  # noqa: ANN201
    from app.orchestration import scheduler
    from app.services import connectors as connector_service
    from app.services.connectors import ConnectorBundle

    def _run(*sources: Any) -> SimpleNamespace:  # noqa: ANN401
        out = SimpleNamespace(snapshots=[], points=[], failing=[], session=_RollupSession())
        monkeypatch.setattr(scheduler.settings, "metrics_backfill_days", 10)
        monkeypatch.setattr(scheduler, "SessionLocal", lambda: out.session)
        monkeypatch.setattr(
            scheduler, "run_per_organization", lambda db, handler, **_: [handler(db, TENANT_ID)]
        )
        monkeypatch.setattr(
            connector_service, "build_connector_bundle",
            lambda db, org: ConnectorBundle(instances={s.slug: s for s in sources}),
        )
        monkeypatch.setattr(
            metrics, "record_snapshot",
            lambda db, *, tenant_id, day, values: out.snapshots.append({"day": day, **values}),
        )

        def capture(db: object, *, tenant_id: str, points: Any) -> int:  # noqa: ANN401
            batch = list(points)
            out.points.extend(batch)
            return len(batch)

        monkeypatch.setattr(social_metrics, "record_points", capture)
        monkeypatch.setattr(
            scheduler, "_mark_failing", lambda db, tenant_id, slug, exc: out.failing.append(slug)
        )
        scheduler.rollup_daily_metrics()
        return out

    return _run


def test_business_profile_alone_is_never_counted_as_sessions(rollup: Any) -> None:  # noqa: ANN401
    gbp = _SessionsSource("google_business_profile", 70)

    out = rollup(gbp)

    assert out.snapshots == []  # nothing reaches daily_metrics.organic_sessions
    assert {(p.source, p.metric) for p in out.points} == {("google_business_profile", "local_impressions")}
    assert len(out.points) == 10  # backfilled like any new source
    assert out.session.checks == ["google_business_profile:local_impressions"]


def test_with_analytics_and_business_profile_only_analytics_is_sessions(rollup: Any) -> None:  # noqa: ANN401
    adobe = _SessionsSource("adobe_analytics", 12)
    gbp = _SessionsSource("google_business_profile", 70)

    out = rollup(gbp, adobe)

    assert out.snapshots and all(s["organic_sessions"] == 12 for s in out.snapshots)
    sessions = {p.source for p in out.points if p.metric == "sessions"}
    assert sessions == {"adobe_analytics"}
    assert {p.value for p in out.points if p.metric == "local_impressions"} == {70.0}


def test_a_failing_business_profile_does_not_stop_analytics(
    rollup: Any, caplog: pytest.LogCaptureFixture  # noqa: ANN401
) -> None:
    out = rollup(_SessionsSource("google_business_profile", 1, fail=True), _SessionsSource("google_analytics_4", 5))

    assert out.failing == ["google_business_profile"]
    assert out.snapshots and not [p for p in out.points if p.metric == "local_impressions"]
    assert "sk-live-not-for-logs" not in caplog.text


def test_analytics_rows_whose_day_is_not_a_date_are_skipped(rollup: Any) -> None:  # noqa: ANN401
    class _Odd(_SessionsSource):
        def read_sessions(self, *, days: int = 30) -> list[Any]:  # noqa: ANN401
            self.requested.append(days)
            today = utcnow().date()
            return [
                SessionMetrics(day=today, sessions=4, conversions=1),
                SimpleNamespace(day=datetime(2026, 10, 1, 12, 0), sessions=9, conversions=1),
                SimpleNamespace(day="yesterday", sessions=9, conversions=1),
                SimpleNamespace(day=None, sessions=9, conversions=1),
            ]

    out = rollup(_Odd("google_analytics_4", 4))
    assert [s["day"] for s in out.snapshots] == [utcnow().date()]
    assert {p.day for p in out.points if p.source == "google_analytics_4"} == {utcnow().date()}


# ── Social metrics: no partial daily totals ────────────────────────────────
class _SocialDb:
    def __init__(self, rows: list[tuple[str, str, str]]) -> None:
        self.rows = rows
        self.writes: list[Any] = []

    def execute(self, statement: Any) -> SimpleNamespace | None:  # noqa: ANN401
        if isinstance(statement, Select):
            return SimpleNamespace(all=lambda: list(self.rows))
        self.writes.append(statement)
        return None

    def point_networks(self) -> set[str]:
        out: set[str] = set()
        for stmt in self.writes:
            if stmt.table.name == "metric_points":
                params = stmt.compile(dialect=postgresql.dialect()).params
                out |= {v for k, v in params.items() if k.startswith("dimension_m")}
        return out

    def post_rows(self) -> int:
        return sum(1 for s in self.writes if s.table.name == "social_post_metrics")


class _SocialReader:
    def __init__(self, slug: str, *, fail_on_call: int | None = None, reply: Any = None) -> None:  # noqa: ANN401
        self.slug = slug
        self.fail_on_call = fail_on_call
        self.reply = reply
        self.calls = 0

    def read_post_metrics(self, remote_ids: list[str]) -> Any:  # noqa: ANN401
        self.calls += 1
        if self.fail_on_call == self.calls:
            raise RuntimeError("vendor said no (token=sk-live-not-for-logs)")
        if self.reply is not None:
            return self.reply
        return {rid: {"likes": 1} for rid in remote_ids}

    def close(self) -> None:
        return None


def _posts(channel: str, count: int) -> list[tuple[str, str, str]]:
    return [(f"asset-{channel}-{i}", channel, f"{channel}-{i}") for i in range(count)]


def _bundle(*readers: Any) -> SimpleNamespace:  # noqa: ANN401
    return SimpleNamespace(instances={r.slug: r for r in readers}, close=lambda: None)


def test_a_second_batch_failure_records_no_daily_total_for_that_network() -> None:
    db = _SocialDb(_posts("facebook_pages", 60) + _posts("instagram_business", 2))
    facebook = _SocialReader("facebook_pages", fail_on_call=2)
    instagram = _SocialReader("instagram_business")

    result = social_metrics.collect(db, SimpleNamespace(id=TENANT_ID), bundle=_bundle(facebook, instagram))  # type: ignore[arg-type]

    assert facebook.calls == 2 and result.connectors_failed == 1
    assert result.incomplete_networks == {"facebook"}
    # The first batch's per-post figures are still kept ...
    assert db.post_rows() == 50 + 2
    # ... but no partial sum overwrites Facebook's day; Instagram is complete.
    assert db.point_networks() == {"instagram"}


def test_a_complete_network_still_gets_its_daily_total() -> None:
    db = _SocialDb(_posts("facebook_pages", 60))
    result = social_metrics.collect(db, SimpleNamespace(id=TENANT_ID), bundle=_bundle(_SocialReader("facebook_pages")))  # type: ignore[arg-type]
    assert result.incomplete_networks == set()
    assert db.point_networks() == {"facebook"}


def test_an_x_read_cut_short_by_its_cap_records_no_daily_total(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(social_metrics.settings, "x_metrics_max_reads_per_run", 3)
    db = _SocialDb(_posts("x_twitter", 5))
    result = social_metrics.collect(db, SimpleNamespace(id=TENANT_ID), bundle=_bundle(_SocialReader("x_twitter")))  # type: ignore[arg-type]
    assert result.posts_requested == 3 and result.incomplete_networks == {"x"}
    assert db.point_networks() == set()


def test_more_posts_than_one_run_reads_records_no_daily_totals(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(social_metrics, "MAX_POSTS_PER_RUN", 3)
    db = _SocialDb(_posts("reddit", 4))  # the query asks for one more than the cap
    reader = _SocialReader("reddit")
    result = social_metrics.collect(db, SimpleNamespace(id=TENANT_ID), bundle=_bundle(reader))  # type: ignore[arg-type]
    assert result.posts_requested == 3
    assert result.incomplete_networks == {"reddit"}
    assert db.point_networks() == set()


def test_a_reply_that_is_not_a_mapping_makes_the_network_incomplete() -> None:
    db = _SocialDb(_posts("reddit", 1))
    result = social_metrics.collect(db, SimpleNamespace(id=TENANT_ID), bundle=_bundle(_SocialReader("reddit", reply=["x"])))  # type: ignore[arg-type]
    assert result.incomplete_networks == {"reddit"} and db.writes == []


def test_a_refused_post_in_a_good_batch_records_no_daily_total() -> None:
    db = _SocialDb(_posts("facebook_pages", 3))

    class _Partial(_SocialReader):
        def read_post_metrics(self, remote_ids: list[str]) -> Any:  # noqa: ANN401
            self.calls += 1
            out: dict[str, Any] = {rid: {"likes": 1} for rid in remote_ids}
            out[remote_ids[1]] = "refused"
            return out

    result = social_metrics.collect(  # type: ignore[arg-type]
        db, SimpleNamespace(id=TENANT_ID), bundle=_bundle(_Partial("facebook_pages"))
    )
    assert result.incomplete_networks == {"facebook"}
    assert result.posts_updated == 2 and db.post_rows() == 2
    assert db.point_networks() == set()


def test_the_run_cap_is_judged_on_query_rows_not_usable_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(social_metrics, "MAX_POSTS_PER_RUN", 2)
    long_id = "x" * (social_metrics._REMOTE_ID_MAX + 1)
    db = _SocialDb(
        [
            ("a1", "facebook_pages", "fb-0"),
            ("a2", "facebook_pages", "fb-1"),
            ("a3", "facebook_pages", long_id),
        ]
    )
    result = social_metrics.collect(  # type: ignore[arg-type]
        db, SimpleNamespace(id=TENANT_ID), bundle=_bundle(_SocialReader("facebook_pages"))
    )
    assert result.incomplete_networks == {"facebook"}
    assert db.point_networks() == set()


# ── Beat: stale creative jobs and long intervals ───────────────────────────
def _tasks() -> Any:  # noqa: ANN401
    from app.orchestration import tasks

    if not tasks.CELERY_AVAILABLE:
        pytest.skip("Celery is not installed")
    return tasks


def test_stuck_creative_jobs_are_swept_every_five_minutes_off_the_render_queue() -> None:
    tasks = _tasks()
    [entry] = [
        e for e in tasks.celery_app.conf.beat_schedule.values() if e["task"] == "ops.creative_sweep"
    ]
    assert entry["schedule"] == 300.0
    assert entry["options"]["expires"] <= 300
    # "creative.*" routes to the render queue; a DB sweep must not wait there.
    assert not entry["task"].startswith("creative.")


def test_the_sweep_does_not_walk_workspaces_twice(monkeypatch: pytest.MonkeyPatch) -> None:
    """pipeline.sweep_stale_jobs already loops workspaces.

    Two tenants through an outer run_per_organization would make the pipeline
    run twice (N²). The scheduler must call it once.
    """
    from app.orchestration import scheduler as scheduler_module
    from app.orchestration import tasks
    from app.services.creative import pipeline

    calls: list[object] = []

    class _Db:
        def close(self) -> None:
            return None

    def fake_sweep(db: object) -> int:
        calls.append(db)
        return 4

    def fake_run(db: object, handler: Any, **_: Any) -> list[int]:  # noqa: ANN401
        return [handler(db, "t1"), handler(db, "t2")]

    monkeypatch.setattr(pipeline, "sweep_stale_jobs", fake_sweep, raising=False)
    monkeypatch.setattr(scheduler_module, "SessionLocal", _Db)
    monkeypatch.setattr(scheduler_module, "run_per_organization", fake_run)

    assert tasks.sweep_stale_creative_jobs_task() == {"swept": 4}
    assert len(calls) == 1


@pytest.mark.parametrize("raw, expected", [(None, 0), (True, 0), (2, 2)])
def test_the_sweep_counts_only_numbers(
    raw: object, expected: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.orchestration import scheduler as scheduler_module
    from app.services.creative import pipeline

    class _Db:
        def close(self) -> None:
            return None

    monkeypatch.setattr(pipeline, "sweep_stale_jobs", lambda db: raw, raising=False)
    monkeypatch.setattr(scheduler_module, "SessionLocal", _Db)
    assert scheduler_module.sweep_stale_creative_jobs() == expected


# ── In-process scheduler: the same sweep when Celery is off ────────────────
def test_the_in_process_scheduler_sweeps_every_five_minutes(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.orchestration import scheduler as scheduler_module

    runs: list[int] = []
    monkeypatch.setattr(scheduler_module, "sweep_stale_creative_jobs", lambda: runs.append(1) or 0)
    monkeypatch.setattr(scheduler_module.settings, "celery_enabled", False)
    sched = scheduler_module.AgentScheduler(tick_seconds=30)

    sched._maybe_sweep_creative_jobs()
    sched._maybe_sweep_creative_jobs()  # within five minutes: not again
    assert runs == [1]
    assert sched._next_creative_sweep is not None
    assert sched._next_creative_sweep > utcnow() + timedelta(minutes=4)

    sched._next_creative_sweep = utcnow() - timedelta(seconds=1)
    sched._maybe_sweep_creative_jobs()
    assert runs == [1, 1]


def test_the_in_process_sweep_is_left_to_celery_when_it_is_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """Beat runs ops.creative_sweep then; running both would sweep twice."""
    from app.orchestration import scheduler as scheduler_module

    runs: list[int] = []
    monkeypatch.setattr(scheduler_module, "sweep_stale_creative_jobs", lambda: runs.append(1) or 0)
    monkeypatch.setattr(scheduler_module.settings, "celery_enabled", True)
    scheduler_module.AgentScheduler(tick_seconds=30)._maybe_sweep_creative_jobs()
    assert runs == []


def test_a_failing_sweep_never_stops_the_scheduler(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from app.orchestration import scheduler as scheduler_module

    def boom() -> int:
        raise RuntimeError("db said no (password=hunter2)")

    monkeypatch.setattr(scheduler_module, "sweep_stale_creative_jobs", boom)
    monkeypatch.setattr(scheduler_module.settings, "celery_enabled", False)
    sched = scheduler_module.AgentScheduler(tick_seconds=30)
    sched._maybe_sweep_creative_jobs()  # does not raise
    assert sched._next_creative_sweep is not None
    assert "hunter2" not in caplog.text


def test_the_sweep_calls_the_pipeline_once_with_two_tenants(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Outer run_per_organization around the pipeline walk would be N²."""
    from app.orchestration import scheduler as scheduler_module
    from app.services.creative import pipeline

    seen: list[object] = []

    class _Db:
        def close(self) -> None:
            return None

    def fake_sweep(db: object) -> int:
        seen.append(db)
        return 3

    monkeypatch.setattr(pipeline, "sweep_stale_jobs", fake_sweep, raising=False)
    monkeypatch.setattr(scheduler_module, "SessionLocal", _Db)
    monkeypatch.setattr(
        scheduler_module,
        "run_per_organization",
        lambda db, handler, **_: [handler(db, "t1"), handler(db, "t2")],
    )
    assert scheduler_module.sweep_stale_creative_jobs() == 3
    assert len(seen) == 1


@pytest.mark.parametrize(
    ("hours", "hour", "day_of_month"),
    [(24, {0}, set(range(1, 32))), (30, {0}, set(range(1, 32, 2))), (72, {0}, set(range(1, 32, 3)))],
)
def test_intervals_of_a_day_or_more_are_clock_anchored(hours: int, hour: set[int], day_of_month: set[int]) -> None:
    from celery.schedules import crontab

    schedule = _tasks()._every_hours(hours)
    assert isinstance(schedule, crontab)
    assert schedule.minute == {10} and schedule.hour == hour
    assert schedule.day_of_month == day_of_month


def test_an_interval_below_one_hour_is_treated_as_hourly() -> None:
    schedule = _tasks()._every_hours(0)
    assert schedule.hour == set(range(24))


# ── accumulate: snapshot keys are never added ──────────────────────────────
def test_accumulate_adds_counters_and_ignores_snapshot_keys(
    caplog: pytest.LogCaptureFixture,
) -> None:
    captured: list[Any] = []

    class _Db:
        def execute(self, statement: Any) -> None:  # noqa: ANN401
            captured.append(statement)

    with caplog.at_level("WARNING"):
        metrics.accumulate(
            _Db(),  # type: ignore[arg-type]
            tenant_id=TENANT_ID,
            values={
                "pages_optimised": 2,
                "organic_sessions": 99,
                "ad_spend": 5,
                "conversions": 1,
            },
        )

    assert len(captured) == 1
    compiled = captured[0].compile(dialect=postgresql.dialect())
    update = str(compiled).split("DO UPDATE SET", 1)[1]
    assert "pages_optimised" in update
    assert "organic_sessions" not in update
    assert "ad_spend" not in update
    assert "conversions" not in update
    params = dict(compiled.params)
    assert 2 in params.values()
    assert 99 not in params.values()
    assert "organic_sessions" in caplog.text
    assert "ad_spend" in caplog.text
    assert "conversions" in caplog.text
    assert "99" not in caplog.text


def test_accumulate_writes_nothing_when_only_snapshot_keys_are_sent(
    caplog: pytest.LogCaptureFixture,
) -> None:
    captured: list[Any] = []

    class _Db:
        def execute(self, statement: Any) -> None:  # noqa: ANN401
            captured.append(statement)

    with caplog.at_level("WARNING"):
        metrics.accumulate(
            _Db(),  # type: ignore[arg-type]
            tenant_id=TENANT_ID,
            values={"organic_sessions": 5, "conversions": 2, "ad_spend": 1.5},
        )
    assert captured == []
    assert "organic_sessions" in caplog.text


def test_snapshot_uses_one_today_for_the_whole_range(monkeypatch: pytest.MonkeyPatch) -> None:
    today = date(2026, 10, 21)
    seen: list[date | None] = []
    starts: list[date] = []
    real_series = metrics.calendar_series

    def fake_rows(
        db: object, *, tenant_id: str, range_key: str, today: date | None = None
    ) -> list[Any]:
        seen.append(today)
        return []

    def fake_measured(
        db: object, *, tenant_id: str, range_key: str, today: date | None = None
    ) -> metrics.Measured:
        seen.append(today)
        return metrics.Measured()

    def fake_series(values: dict[date, int], *, start: date, days: int) -> list[int | None]:
        starts.append(start)
        return real_series(values, start=start, days=days)

    monkeypatch.setattr(metrics, "rows_for_range", fake_rows)
    monkeypatch.setattr(metrics, "measured_for_range", fake_measured)
    monkeypatch.setattr(metrics, "calendar_series", fake_series)
    monkeypatch.setattr(
        metrics, "utcnow", lambda: datetime(2026, 10, 21, 23, 59, 59, tzinfo=UTC)
    )

    metrics.snapshot(object(), tenant_id=TENANT_ID, range_key="7d")  # type: ignore[arg-type]
    assert seen == [today, today]
    assert starts == [today - timedelta(days=6)]
