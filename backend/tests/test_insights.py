"""Insights overview and LLM usage (step 5.5).

The first half needs no database. The period maths and the builders are
pure; the endpoints run through the real app with auth and the session
overridden and the loaders replaced; a recording session checks that every
statement is tenant-filtered and that the query count is fixed. The
``requires_db`` tests repeat the figures and the tenant isolation against
real Postgres.
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

from app.core.config import settings
from app.core.exceptions import InvalidInputError
from app.services import insights, llm_usage
from app.services.insights import ActivityCounts, ContentCounts, PointRow, SeoCounts, SocialRow, Window
from tests.conftest import requires_db

TENANT = "00000000-0000-0000-0000-0000000015a5"
START, END = date(2026, 3, 1), date(2026, 3, 10)
PREVIOUS_DAY = date(2026, 2, 25)


# ── Periods ────────────────────────────────────────────────────────────────
def test_the_previous_period_has_the_same_length_and_ends_the_day_before() -> None:
    current, previous = insights.windows(START, END)
    assert current.days == previous.days == 10
    assert previous.end == date(2026, 2, 28)
    assert previous.start == date(2026, 2, 19)
    # A single day compares with the day before.
    one, before = insights.windows(END, END)
    assert (one.days, before.start, before.end) == (1, END - timedelta(days=1), END - timedelta(days=1))


def test_a_range_over_the_cap_or_backwards_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "insights_max_range_days", 30)
    insights.windows(START, START + timedelta(days=29))
    with pytest.raises(InvalidInputError):
        insights.windows(START, START + timedelta(days=30))
    with pytest.raises(InvalidInputError):
        insights.windows(END, START)
    with pytest.raises(InvalidInputError):
        insights.windows(date(1999, 12, 31), date(2000, 1, 5))


@pytest.mark.parametrize(
    ("current", "previous", "expected"),
    [(150, 100, 50.0), (50, 100, -50.0), (0, 100, -100.0), (10, 0, None), (None, 5, None), (5, None, None)],
)
def test_change_is_a_percentage_or_nothing(current: Any, previous: Any, expected: Any) -> None:  # noqa: ANN401
    assert insights.change_pct(current, previous) == expected


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "http://facebook.com/post/1",
        "https://user:pw@facebook.com/post",
        "https://",
        "https://facebook.com/a b",
        "https://facebook.com/\x00",
        "https://x.com/" + "a" * 1000,
        None,
        42,
    ],
)
def test_only_plain_https_post_links_are_passed_on(url: object) -> None:
    assert insights.safe_post_url(url) is None


def test_a_normal_post_link_is_kept() -> None:
    assert insights.safe_post_url("https://www.facebook.com/123_456") == "https://www.facebook.com/123_456"


# ── Builders ───────────────────────────────────────────────────────────────
def _window() -> Window:
    return insights.windows(START, END)[0]


def test_search_figures_and_deltas() -> None:
    rows = [
        PointRow("search_console", "clicks", START, 30.0),
        PointRow("search_console", "clicks", START + timedelta(days=1), 30.0),
        PointRow("search_console", "impressions", START, 1000.0),
        PointRow("search_console", "position", START, 4.0),
        PointRow("bing_webmaster", "impressions", START, 3000.0),
        PointRow("bing_webmaster", "position", START, 8.0),
        PointRow("search_console", "clicks", PREVIOUS_DAY, 40.0),
    ]
    counts = SeoCounts(issues_tracked=True, issues_current=6, issues_previous=3)
    section = insights.build_seo(rows, counts, current=_window(), backlinks=True)
    metrics = {m.key: m for m in section.metrics}

    assert section.available is True and section.connect_hint == ""
    assert (metrics["clicks"].value, metrics["clicks"].previous, metrics["clicks"].change_pct) == (60, 40, 50.0)
    # Weighted by impressions: (4 * 1000 + 8 * 3000) / 4000.
    assert metrics["position"].value == 7.0
    assert metrics["position"].better == "down"
    assert metrics["issues_fixed"].change_pct == 100.0
    # Nothing tracks pages or backlinks here: no number, a note.
    for key in ("pages_optimised", "backlinks_won"):
        assert metrics[key].value is None and metrics[key].note == insights.NOT_TRACKED
    clicks = next(s for s in section.series if s.key == "clicks")
    assert len(clicks.points) == 10
    assert clicks.points[0].value == 30 and clicks.points[5].value is None
    assert section.top_queries == []


def test_website_visits_come_from_analytics_with_deltas() -> None:
    analytics = [
        PointRow("google_analytics_4", "sessions", START, 300.0),
        PointRow("google_analytics_4", "sessions", START + timedelta(days=2), 100.0),
        PointRow("google_analytics_4", "conversions", START, 8.0),
        PointRow("google_analytics_4", "sessions", PREVIOUS_DAY, 200.0),
        PointRow("google_analytics_4", "conversions", PREVIOUS_DAY, 4.0),
    ]
    section = insights.build_seo([], SeoCounts(), current=_window(), backlinks=False, analytics=analytics)
    metrics = {m.key: m for m in section.metrics}
    visits = metrics["sessions"]
    assert visits.label == "Website visits"
    assert (visits.value, visits.previous, visits.change_pct, visits.note) == (400, 200, 100.0, "")
    assert (metrics["conversions"].value, metrics["conversions"].change_pct) == (8, 100.0)
    # Visits alone make the section worth showing; search is still missing.
    assert section.available is True and section.connect_hint == insights.HINT_SEARCH
    series = next(s for s in section.series if s.key == "sessions")
    assert series.points[0].value == 300 and series.points[1].value is None  # a gap, not 0


def test_no_analytics_source_says_what_to_connect_not_zero() -> None:
    section = insights.build_seo([], SeoCounts(), current=_window(), backlinks=False)
    metrics = {m.key: m for m in section.metrics}
    for key in ("sessions", "conversions"):
        assert metrics[key].value is None
        assert metrics[key].note == insights.HINT_ANALYTICS
    assert section.available is False
    assert insights.HINT_ANALYTICS == "Connect Google Analytics or Adobe Analytics to see website visits."


def test_overview_reads_visits_for_seo_roles_and_the_client_but_not_from_business_profile(
    data: dict[str, Any],
) -> None:
    data["points"] = [
        PointRow("google_analytics_4", "sessions", START, 50.0),
        # Business Profile "sessions" are local impressions, never website visits.
        PointRow("google_business_profile", "sessions", START, 9000.0),
    ]
    params = {"from": START.isoformat(), "to": END.isoformat()}
    for role in ("admin", "seo", "client"):
        with _client(role) as client:
            seo = client.get("/api/v1/insights/overview", params=params).json()["sections"]["seo"]
        visits = next(m for m in seo["metrics"] if m["key"] == "sessions")
        assert visits["value"] == 50, role
    # A role without SEO view gets no seo section, so no visits either.
    with _client("ads") as client:
        assert "seo" not in client.get("/api/v1/insights/overview", params=params).json()["sections"]


def test_backlinks_are_left_out_without_off_page_access() -> None:
    section = insights.build_seo([], SeoCounts(backlinks_tracked=True), current=_window(), backlinks=False)
    assert "backlinks_won" not in {m.key for m in section.metrics}


def test_ads_per_channel_and_cost_per_conversion() -> None:
    rows = [
        PointRow("google_ads", "spend", START, 100.0),
        PointRow("google_ads", "conversions", START, 4.0),
        PointRow("meta_ads", "spend", START, 50.0),
        PointRow("google_ads", "spend", PREVIOUS_DAY, 300.0),
    ]
    section = insights.build_ads(rows, current=_window())
    metrics = {m.key: m for m in section.metrics}
    assert metrics["spend"].value == 150 and metrics["spend"].change_pct == -50.0
    assert metrics["cac"].value == 37.5
    channels = {c.channel: c for c in section.channels}
    assert channels["google_ads"].cac == 25
    # No conversions: no cost per conversion, never a division by zero or 0.
    assert channels["meta_ads"].cac is None and channels["meta_ads"].note
    assert metrics["clicks"].value is None


def test_social_unreported_figures_are_null_and_named() -> None:
    rows = [
        SocialRow("instagram", True, 2, {"impressions": (0, 0), "reach": (500, 2), "engagements": (40, 2), "clicks": (0, 0)}),
        SocialRow("facebook", True, 1, {"impressions": (900, 1), "reach": (0, 0), "engagements": (10, 1), "clicks": (3, 1)}),
        SocialRow("instagram", False, 1, {"impressions": (0, 0), "reach": (250, 1), "engagements": (0, 0), "clicks": (0, 0)}),
    ]
    top = [("a1", "instagram", "Spring launch", "javascript:alert(1)", datetime(2026, 3, 2, tzinfo=UTC), None, 30)]
    section = insights.build_social(rows, top, [], current=_window())
    platforms = {p.platform: p for p in section.platforms}

    insta = platforms["instagram"]
    assert insta.impressions is None and insta.reach == 500
    assert insta.note.startswith("Not reported by Instagram")
    assert platforms["facebook"].reach is None and "Not reported by Facebook" in platforms["facebook"].note
    totals = {m.key: m for m in section.metrics}
    assert totals["reach"].value == 500 and totals["reach"].previous == 250
    assert totals["reach"].change_pct == 100.0
    post = section.top_posts[0]
    assert post.impressions is None and post.engagement == 30
    assert post.url is None  # an unsafe link is dropped, not passed to the browser


def test_an_empty_workspace_says_what_to_connect_everywhere() -> None:
    window = _window()
    seo = insights.build_seo([], SeoCounts(), current=window, backlinks=True)
    ads = insights.build_ads([], current=window)
    social = insights.build_social([], [], [], current=window)
    content = insights.build_content(ContentCounts())
    agents = insights.build_agents(ActivityCounts(), agents=True)
    for section, hint in (
        (seo, insights.HINT_SEARCH),
        (ads, insights.HINT_ADS),
        (social, insights.HINT_SOCIAL),
        (content, insights.HINT_CONTENT),
        (agents, insights.HINT_AGENTS),
    ):
        assert section.available is False
        assert section.connect_hint == hint
    # Never a fabricated zero from an outside source.
    assert all(m.value is None for m in (*seo.metrics, *ads.metrics, *social.metrics))


def test_a_quiet_period_after_a_busy_one_says_so() -> None:
    ads = insights.build_ads([PointRow("google_ads", "spend", PREVIOUS_DAY, 9.0)], current=_window())
    assert ads.available is False
    assert ads.connect_hint == insights.HINT_QUIET


# ── Endpoints (no database) ────────────────────────────────────────────────
def _current_user(role: str) -> Any:  # noqa: ANN401
    from app.api.deps import CurrentUser

    return CurrentUser(
        user=SimpleNamespace(role=role, email="viewer@example.test"),
        organization=SimpleNamespace(id=TENANT),
        claims={},
    )


def _app(current: object) -> FastAPI:
    from app.api.deps import get_current_user
    from app.db.session import get_db
    from app.main import create_app
    from tests.conftest import _noop_lifespan

    app = create_app()
    app.router.lifespan_context = _noop_lifespan
    app.dependency_overrides[get_current_user] = lambda: current
    app.dependency_overrides[get_db] = lambda: object()
    return app


def _summary(group: Any, runs: int = 0, cost: float | None = None) -> llm_usage.UsageSummary:  # noqa: ANN401
    totals = llm_usage.UsageTotals(
        runs=runs, tokens_in=runs * 10 or None, tokens_out=runs * 5 or None,
        tokens_in_text=str(runs * 10) if runs else "unknown",
        tokens_out_text=str(runs * 5) if runs else "unknown",
        unknown_token_runs=0, cost=cost,
        cost_status=llm_usage.CostStatus.ESTIMATE if cost is not None else llm_usage.CostStatus.NO_PRICE,
        cost_label="Estimate" if cost is not None else "No price set",
    )
    return llm_usage.UsageSummary(
        group=llm_usage.UsageGroup(group), start=START, end=END, lines=[], totals=totals,
        next_cursor=None, currency_symbol="₹",
    )


@pytest.fixture
def data(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Replaces every loader; tests fill in what they need."""
    state: dict[str, Any] = {"points": [], "seo": SeoCounts(), "social": [], "top": [],
                             "content": ContentCounts(), "activity": None, "llm": [], "calls": []}

    def check(tenant_id: str) -> None:
        assert tenant_id == TENANT, "insights must read only the caller's workspace"

    def points(db: object, *, tenant_id: str, sources: Any, metrics: Any, **_: Any) -> list[PointRow]:  # noqa: ANN401
        check(tenant_id)
        return [p for p in state["points"] if p.source in sources and p.metric in metrics]

    def seo(db: object, *, tenant_id: str, **_: Any) -> SeoCounts:  # noqa: ANN401
        check(tenant_id)
        return state["seo"]

    def social(db: object, *, tenant_id: str, **_: Any) -> list[SocialRow]:  # noqa: ANN401
        check(tenant_id)
        return state["social"]

    def top(db: object, *, tenant_id: str, **_: Any) -> list[Any]:  # noqa: ANN401
        check(tenant_id)
        return state["top"]

    def content(db: object, *, tenant_id: str, **_: Any) -> ContentCounts:  # noqa: ANN401
        check(tenant_id)
        return state["content"]

    def activity(db: object, *, tenant_id: str, approvals: bool, agents: bool, **_: Any) -> ActivityCounts:  # noqa: ANN401
        check(tenant_id)
        counts = state["activity"] or ActivityCounts()
        return ActivityCounts(
            pending_approvals=(counts.pending_approvals or 0) if approvals else None,
            runs=counts.runs if agents else {},
        )

    def summary(db: object, tenant_id: str, start: date, end: date, group: Any = "agent",  # noqa: ANN401
                cursor: str | None = None, limit: int | None = None) -> llm_usage.UsageSummary:
        check(tenant_id)
        state["calls"].append((start, end, str(group), cursor, limit))
        if state["llm"]:
            return state["llm"].pop(0)
        return _summary(group)

    monkeypatch.setattr(insights, "load_points", points)
    monkeypatch.setattr(insights, "load_seo_counts", seo)
    monkeypatch.setattr(insights, "load_social", social)
    monkeypatch.setattr(insights, "load_top_posts", top)
    monkeypatch.setattr(insights, "load_content", content)
    monkeypatch.setattr(insights, "load_activity", activity)
    monkeypatch.setattr(llm_usage, "summary", summary)
    return state


def _client(role: str) -> TestClient:
    return TestClient(_app(_current_user(role)))


@pytest.fixture
def admin() -> Iterator[TestClient]:
    with _client("admin") as client:
        yield client


def test_an_empty_workspace_gets_every_section_unavailable_with_hints(
    admin: TestClient, data: dict[str, Any]
) -> None:
    response = admin.get("/api/v1/insights/overview", params={"from": START.isoformat(), "to": END.isoformat()})
    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["from"], body["to"]) == ("2026-03-01", "2026-03-10")
    assert (body["previous_from"], body["previous_to"], body["days"]) == ("2026-02-19", "2026-02-28", 10)
    sections = body["sections"]
    assert set(sections) == {"seo", "ads", "social", "content", "agents", "llm"}
    for name in ("seo", "ads", "social", "content", "llm"):
        assert sections[name]["available"] is False, name
        assert sections[name]["connect_hint"], name
    # Pending approvals is a live, measured count, so it is shown as 0.
    pending = next(m for m in sections["agents"]["metrics"] if m["key"] == "pending_approvals")
    assert pending["value"] == 0
    # No price set means no cost, never "free".
    cost = next(m for m in sections["llm"]["metrics"] if m["key"] == "cost")
    assert cost["value"] is None and cost["note"] == "No price set" and cost["is_estimate"] is True
    point = sections["seo"]["series"][0]["points"][0]
    assert point == {"date": "2026-03-01", "value": None}


def test_data_present_gives_figures_and_deltas(admin: TestClient, data: dict[str, Any]) -> None:
    data["points"] = [
        PointRow("search_console", "clicks", START, 120.0),
        PointRow("search_console", "clicks", PREVIOUS_DAY, 80.0),
        PointRow("google_ads", "spend", START, 200.0),
    ]
    data["content"] = ContentCounts(by_kind={"article": (3, 1)}, creatives_current=2, creatives_previous=4)
    data["llm"] = [_summary("provider", runs=4, cost=2.0), _summary("provider", runs=2, cost=1.0)]

    body = admin.get("/api/v1/insights/overview", params={"from": "2026-03-01", "to": "2026-03-10"}).json()
    sections = body["sections"]
    clicks = next(m for m in sections["seo"]["metrics"] if m["key"] == "clicks")
    assert (clicks["value"], clicks["previous"], clicks["change_pct"]) == (120, 80, 50.0)
    assert sections["seo"]["available"] is True
    spend = next(m for m in sections["ads"]["metrics"] if m["key"] == "spend")
    assert spend["unit"] == "currency" and spend["previous"] is None and spend["change_pct"] is None
    creatives = next(m for m in sections["content"]["metrics"] if m["key"] == "creatives")
    assert creatives["change_pct"] == -50.0
    assert sections["content"]["by_kind"] == [
        {"key": "article", "label": "Articles", "value": 3, "unit": "count", "note": ""}
    ]
    cost = next(m for m in sections["llm"]["metrics"] if m["key"] == "cost")
    assert (cost["value"], cost["change_pct"]) == (2.0, 100.0)
    # Current period, then the previous period of equal length.
    assert [c[:2] for c in data["calls"]] == [(START, END), (date(2026, 2, 19), date(2026, 2, 28))]


def test_a_client_viewer_sees_results_but_never_cost_or_activity(data: dict[str, Any]) -> None:
    data["llm"] = [_summary("provider", runs=4, cost=2.0)] * 2
    data["activity"] = ActivityCounts(pending_approvals=3, runs={"succeeded": (5, 2)})
    data["seo"] = SeoCounts(backlinks_tracked=True, backlinks_current=2, backlinks_previous=1)
    data["points"] = [PointRow("search_console", "clicks", START, 120.0)]
    with _client("client") as client:
        response = client.get(
            "/api/v1/insights/overview", params={"from": START.isoformat(), "to": END.isoformat()}
        )
        assert response.status_code == 200, response.text
        sections = response.json()["sections"]
        # The results sections, read-only.
        assert set(sections) == {"seo", "ads", "social", "content"}
        seo = {m["key"]: m for m in sections["seo"]["metrics"]}
        assert seo["clicks"]["value"] == 120
        assert seo["backlinks_won"]["value"] == 2  # backlinks are a result too
        # Never cost, agents or approvals activity.
        assert "llm" not in sections and "agents" not in sections
        assert "pending_approvals" not in response.text and "tokens_in" not in response.text
        usage = client.get("/api/v1/insights/llm-usage")
        assert usage.status_code == 403
        assert "Traceback" not in usage.text
        for group in ("agent", "model", "day", "month", "job"):
            assert client.get("/api/v1/insights/llm-usage", params={"group": group}).status_code == 403
    assert data["calls"] == []  # cost was never even read for them


def test_a_client_viewer_without_reports_view_gets_no_results() -> None:
    """The client widening hangs off Reports view; without it nothing is shown."""
    from app.core.rbac import Module, Role

    viewer = SimpleNamespace(
        tenant_id=TENANT, role=Role.CLIENT, can_view=lambda module: False
    )
    for module in (Module.SEO, Module.OFFPAGE, Module.ADS, Module.CONTENT):
        assert insights.can_see_results(viewer, module) is False  # type: ignore[arg-type]


def test_only_a_client_viewer_gets_results_without_the_module() -> None:
    from app.core.rbac import Module, Role

    reports_only = {Module.REPORTS}
    client = SimpleNamespace(tenant_id=TENANT, role=Role.CLIENT, can_view=lambda m: m in reports_only)
    other = SimpleNamespace(tenant_id=TENANT, role=Role.SEO, can_view=lambda m: m in reports_only)
    for module in (Module.SEO, Module.OFFPAGE, Module.ADS, Module.CONTENT):
        assert insights.can_see_results(client, module) is True  # type: ignore[arg-type]
        assert insights.can_see_results(other, module) is False  # type: ignore[arg-type]
    # Activity and admin modules are never widened, even for the client.
    for module in (Module.AGENTS, Module.APPROVALS, Module.ADMIN, Module.CONNECTORS):
        assert insights.can_see_results(client, module) is False  # type: ignore[arg-type]
    assert insights.can_see_llm_cost(client) is False  # type: ignore[arg-type]


def test_a_role_without_a_module_gets_that_section_left_out(data: dict[str, Any]) -> None:
    with _client("ads") as client:
        sections = client.get("/api/v1/insights/overview").json()["sections"]
    assert "seo" not in sections
    assert {"ads", "content", "social", "agents", "llm"} <= set(sections)
    with _client("seo") as client:
        sections = client.get("/api/v1/insights/overview").json()["sections"]
    assert "ads" not in sections and "seo" in sections


def test_a_range_over_the_cap_is_a_422(admin: TestClient, data: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "insights_max_range_days", 366)
    too_long = {"from": "2025-01-01", "to": "2026-01-02"}
    for path in ("/api/v1/insights/overview", "/api/v1/insights/llm-usage"):
        response = admin.get(path, params=too_long)
        assert response.status_code == 422, path
        assert response.json()["code"] == "validation_error"
        assert "366 days" in response.json()["detail"]
    assert data["calls"] == []


@pytest.mark.parametrize(
    "params",
    [
        {"from": "2026-13-01"},
        {"from": "2026-03-10", "to": "2026-03-01"},
        {"group": "everything"},
        {"limit": 0},
        {"limit": 101},
        {"cursor": "x" * 401},
        {"cursor": "'; DROP TABLE agent_runs; --"},
    ],
)
def test_bad_usage_parameters_are_rejected(admin: TestClient, data: dict[str, Any], params: dict[str, Any]) -> None:
    response = admin.get("/api/v1/insights/llm-usage", params=params)
    assert response.status_code == 422
    assert data["calls"] == []


def test_llm_usage_is_the_summary_shape(admin: TestClient, data: dict[str, Any]) -> None:
    body = admin.get("/api/v1/insights/llm-usage", params={"group": "provider", "limit": 5}).json()
    assert set(body) == {"group", "start", "end", "lines", "totals", "next_cursor", "currency_symbol", "note"}
    assert body["group"] == "provider" and body["totals"]["is_estimate"] is True
    assert data["calls"][0][2:] == ("provider", None, 5)


# ── Statements: tenant filter, fixed query count ───────────────────────────
class RecordingSession:
    """Records each statement; every query returns no rows."""

    def __init__(self) -> None:
        self.statements: list[Any] = []

    def execute(self, stmt: Any) -> RecordingSession:  # noqa: ANN401
        self.statements.append(stmt)
        return self

    def all(self) -> list[Any]:
        return []


def test_every_query_is_tenant_filtered_and_the_count_is_fixed() -> None:
    db = RecordingSession()
    out = insights.overview(db, _current_user("admin"), start=START, end=END)  # type: ignore[arg-type]
    assert set(out.sections.model_dump()) == {"seo", "ads", "social", "content", "agents", "llm"}
    # points 1, seo 4, social 2, content 2, agents 2, llm 2 x 2: never per row.
    assert len(db.statements) == 15
    for stmt in db.statements:
        compiled = stmt.compile(dialect=postgresql.dialect())
        assert "tenant_id = " in str(compiled)
        assert TENANT in compiled.params.values()
    # The post text is never loaded.
    assert not any("content_encrypted" in str(s.compile(dialect=postgresql.dialect())) for s in db.statements)


def test_a_client_viewer_reads_only_results_and_never_activity_or_cost() -> None:
    db = RecordingSession()
    out = insights.overview(db, _current_user("client"), start=START, end=END)  # type: ignore[arg-type]
    assert set(out.sections.model_dump(exclude_none=True)) == {"seo", "ads", "social", "content"}
    # points 1, seo 4, social 2, content 2: no agents or llm queries.
    assert len(db.statements) == 9
    for stmt in db.statements:
        compiled = stmt.compile(dialect=postgresql.dialect())
        sql = str(compiled)
        assert "tenant_id = " in sql
        assert TENANT in compiled.params.values()
        assert "agent_runs" not in sql and "approval_items" not in sql


# ── Against Postgres ───────────────────────────────────────────────────────
@requires_db
def test_figures_and_cross_tenant_isolation(clean_db, org, second_org) -> None:  # noqa: ANN001
    from app.db.session import bind_tenant
    from app.models.offpage import BacklinkTarget
    from app.models.social import MetricPoint

    won_at = datetime(2026, 3, 3, 12, tzinfo=UTC)
    for tenant, clicks, links in ((org.id, 10.0, 1), (second_org.id, 9999.0, 5)):
        bind_tenant(clean_db, tenant)
        clean_db.add_all(
            [
                MetricPoint(tenant_id=tenant, day=START, source="search_console", metric="clicks", value=clicks),
                MetricPoint(tenant_id=tenant, day=PREVIOUS_DAY, source="search_console", metric="clicks", value=5.0),
                *(
                    BacklinkTarget(tenant_id=tenant, domain=f"site{i}.example", placement_type="Guest Post",
                                   status="Won", won_at=won_at)
                    for i in range(links)
                ),
            ]
        )
        clean_db.flush()

    bind_tenant(clean_db, org.id)
    viewer = SimpleNamespace(
        tenant_id=org.id, role="admin", can_view=lambda module: True
    )
    out = insights.overview(clean_db, viewer, start=START, end=END)  # type: ignore[arg-type]
    seo = {m.key: m for m in out.sections.seo.metrics}  # type: ignore[union-attr]
    assert (seo["clicks"].value, seo["clicks"].previous, seo["clicks"].change_pct) == (10, 5, 100.0)
    assert seo["backlinks_won"].value == 1  # the other workspace's 5 are nowhere
    assert seo["pages_optimised"].value is None  # no pages tracked: not a zero
