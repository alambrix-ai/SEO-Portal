"""Collecting posted social figures (step 4.1): ``app.services.social_metrics``.

Most tests use a fake session that records statements, so they run without a
database; the end-to-end check at the bottom needs ``TEST_DATABASE_URL``.
"""
from __future__ import annotations

import logging
import math
from datetime import date, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.sql import Insert, Select

from app.db.base import utcnow
from app.models.social import POST_METRIC_FIELDS
from app.services import social_metrics
from app.services.social_metrics import Point
from tests.conftest import requires_db

TENANT_ID = "00000000-0000-0000-0000-0000000050c1"
SECRET_TEXT = "token=sk-live-not-for-logs"


# ── Fakes ──────────────────────────────────────────────────────────────────
class _Result:
    def __init__(self, rows: list[tuple[str, str, str]]) -> None:
        self._rows = rows

    def all(self) -> list[tuple[str, str, str]]:
        return list(self._rows)


class _FakeDb:
    """Answers the asset query with ``rows`` and records every write."""

    def __init__(self, rows: list[tuple[str, str, str]]) -> None:
        self.rows = rows
        self.selects: list[Select] = []
        self.writes: list[Insert] = []

    def execute(self, statement: Any) -> _Result | None:  # noqa: ANN401
        if isinstance(statement, Select):
            self.selects.append(statement)
            return _Result(self.rows)
        self.writes.append(statement)
        return None

    def params(self, table: str) -> list[dict[str, Any]]:
        """The compiled parameters of every insert into ``table``."""
        out = []
        for stmt in self.writes:
            if stmt.table.name == table:
                compiled = stmt.compile(dialect=postgresql.dialect())
                out.append(dict(compiled.params))
        return out


class _Reader:
    """A connector with ``read_post_metrics``."""

    def __init__(self, slug: str, figures: dict[str, dict[str, Any]] | None = None) -> None:
        self.slug = slug
        self.figures = figures or {}
        self.calls: list[list[str]] = []

    def read_post_metrics(self, remote_ids: list[str]) -> dict[str, dict[str, Any]]:
        self.calls.append(list(remote_ids))
        return {rid: self.figures[rid] for rid in remote_ids if rid in self.figures}

    def close(self) -> None:
        return None


class _Broken(_Reader):
    def read_post_metrics(self, remote_ids: list[str]) -> dict[str, dict[str, Any]]:
        self.calls.append(list(remote_ids))
        raise RuntimeError(f"vendor said no ({SECRET_TEXT})")


class _NoMetrics:
    slug = "linkedin_pages"

    def close(self) -> None:
        return None


def _bundle(*connectors: Any) -> SimpleNamespace:  # noqa: ANN401
    return SimpleNamespace(instances={c.slug: c for c in connectors}, close=lambda: None)


def _org() -> SimpleNamespace:
    return SimpleNamespace(id=TENANT_ID)


def _posts(channel: str, count: int, prefix: str = "r") -> list[tuple[str, str, str]]:
    return [(f"asset-{channel}-{i}", channel, f"{prefix}{i}") for i in range(count)]


# ── Collection ─────────────────────────────────────────────────────────────
def test_figures_are_stored_per_post_and_unreported_ones_stay_none() -> None:
    db = _FakeDb([("asset-1", "facebook_pages", "fb_1")])
    reader = _Reader("facebook_pages", {"fb_1": {"likes": 5, "comments": 0, "impressions": None}})

    result = social_metrics.collect(db, _org(), bundle=_bundle(reader))  # type: ignore[arg-type]

    assert result.posts_updated == 1
    [row] = db.params("social_post_metrics")
    assert row["tenant_id"] == TENANT_ID and row["asset_id"] == "asset-1"
    assert row["network"] == "facebook" and row["remote_id"] == "fb_1"
    assert row["likes"] == 5
    assert row["comments"] == 0  # a real zero stays zero
    for name in set(POST_METRIC_FIELDS) - {"likes", "comments"}:
        assert row[name] is None, name  # not reported is not zero


def test_the_upsert_overwrites_with_none_and_never_touches_another_workspace() -> None:
    db = _FakeDb([("asset-1", "facebook_pages", "fb_1")])
    social_metrics.collect(
        db, _org(), bundle=_bundle(_Reader("facebook_pages", {"fb_1": {"likes": 1}}))  # type: ignore[arg-type]
    )
    [stmt] = [s for s in db.writes if s.table.name == "social_post_metrics"]
    sql = str(stmt.compile(dialect=postgresql.dialect()))
    update = sql.split("DO UPDATE SET", 1)[1]
    for name in POST_METRIC_FIELDS:
        assert f"{name} =" in update, name
    assert "WHERE social_post_metrics.tenant_id =" in update


def test_the_post_query_is_tenant_filtered_and_never_reads_post_text() -> None:
    db = _FakeDb([])
    social_metrics.collect(db, _org(), bundle=_bundle())  # type: ignore[arg-type]

    [query] = db.selects
    compiled = query.compile(dialect=postgresql.dialect())
    sql = str(compiled)
    assert "marketing_assets.tenant_id = %(tenant_id_1)s" in sql
    assert compiled.params["tenant_id_1"] == TENANT_ID
    assert "content_encrypted" not in sql
    assert compiled.params["kind_1"] == "social_post"
    assert compiled.params["status_1"] == "published"
    assert "LIMIT" in sql


def test_a_connector_without_read_post_metrics_is_skipped_not_failed() -> None:
    db = _FakeDb(_posts("linkedin_pages", 2) + _posts("pinterest", 1))
    result = social_metrics.collect(db, _org(), bundle=_bundle(_NoMetrics()))  # type: ignore[arg-type]

    # LinkedIn has no method; Pinterest is not connected at all.
    assert result.connectors_skipped == 2
    assert result.connectors_failed == 0
    assert db.writes == []


def test_ids_are_read_in_bounded_batches() -> None:
    db = _FakeDb(_posts("facebook_pages", 120))
    reader = _Reader("facebook_pages")
    social_metrics.collect(db, _org(), bundle=_bundle(reader))  # type: ignore[arg-type]

    assert [len(call) for call in reader.calls] == [50, 50, 20]


def test_x_reads_are_capped_per_run(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(social_metrics.settings, "x_metrics_max_reads_per_run", 3)
    db = _FakeDb(_posts("x_twitter", 10))
    reader = _Reader("x_twitter")

    result = social_metrics.collect(db, _org(), bundle=_bundle(reader))  # type: ignore[arg-type]

    assert result.posts_requested == 3
    assert sum(len(call) for call in reader.calls) == 3


def test_an_x_cap_of_zero_reads_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(social_metrics.settings, "x_metrics_max_reads_per_run", 0)
    reader = _Reader("x_twitter")
    social_metrics.collect(_FakeDb(_posts("x_twitter", 5)), _org(), bundle=_bundle(reader))  # type: ignore[arg-type]
    assert reader.calls == []


def test_one_failing_network_does_not_stop_the_others_and_its_text_is_not_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    db = _FakeDb(_posts("facebook_pages", 60) + [("asset-ig", "instagram_business", "ig_1")])
    broken = _Broken("facebook_pages")
    healthy = _Reader("instagram_business", {"ig_1": {"reach": 9}})

    with caplog.at_level(logging.WARNING):
        result = social_metrics.collect(db, _org(), bundle=_bundle(broken, healthy))  # type: ignore[arg-type]

    assert result.connectors_failed == 1
    assert len(broken.calls) == 1  # stopped after the first failed batch
    assert [row["network"] for row in db.params("social_post_metrics")] == ["instagram"]
    assert "sk-live-not-for-logs" not in caplog.text


@pytest.mark.parametrize(
    "raw",
    [-1, True, False, math.nan, math.inf, "12", 2**63, None, [3], {"n": 1}],
)
def test_values_that_are_not_counts_are_stored_as_none(raw: Any) -> None:  # noqa: ANN401
    db = _FakeDb([("asset-1", "facebook_pages", "fb_1")])
    social_metrics.collect(
        db, _org(), bundle=_bundle(_Reader("facebook_pages", {"fb_1": {"likes": raw}}))  # type: ignore[arg-type]
    )
    [row] = db.params("social_post_metrics")
    assert row["likes"] is None


def test_ids_the_platform_was_not_asked_about_are_ignored() -> None:
    class _Chatty(_Reader):
        def read_post_metrics(self, remote_ids: list[str]) -> dict[str, dict[str, Any]]:
            return {"someone-elses-post": {"likes": 99}, "fb_1": {"likes": 1}}

    db = _FakeDb([("asset-1", "facebook_pages", "fb_1")])
    social_metrics.collect(db, _org(), bundle=_bundle(_Chatty("facebook_pages")))  # type: ignore[arg-type]

    rows = db.params("social_post_metrics")
    assert [(r["asset_id"], r["likes"]) for r in rows] == [("asset-1", 1)]


def test_a_reply_that_is_not_a_mapping_stores_nothing() -> None:
    class _Odd(_Reader):
        def read_post_metrics(self, remote_ids: list[str]) -> Any:  # noqa: ANN401
            return ["fb_1", 5]

    db = _FakeDb([("asset-1", "facebook_pages", "fb_1")])
    result = social_metrics.collect(db, _org(), bundle=_bundle(_Odd("facebook_pages")))  # type: ignore[arg-type]
    assert result.posts_updated == 0 and db.writes == []
    assert result.incomplete_networks == {"facebook"}


def test_a_refused_post_in_an_otherwise_good_batch_skips_the_days_points() -> None:
    """A non-dict figure for one post means the network was not read completely."""

    class _Partial(_Reader):
        def read_post_metrics(self, remote_ids: list[str]) -> dict[str, Any]:
            self.calls.append(list(remote_ids))
            return {
                remote_ids[0]: {"likes": 2},
                remote_ids[1]: "refused",
                remote_ids[2]: {"likes": 3},
            }

    db = _FakeDb(_posts("facebook_pages", 3))
    result = social_metrics.collect(db, _org(), bundle=_bundle(_Partial("facebook_pages")))  # type: ignore[arg-type]

    assert result.incomplete_networks == {"facebook"}
    assert result.posts_updated == 2
    assert len(db.params("social_post_metrics")) == 2
    assert db.params("metric_points") == []


def test_the_run_cap_is_judged_before_dropping_unusable_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(social_metrics, "MAX_POSTS_PER_RUN", 2)
    long_id = "x" * (social_metrics._REMOTE_ID_MAX + 1)
    db = _FakeDb(
        [
            ("a1", "facebook_pages", "fb_1"),
            ("a2", "facebook_pages", "fb_2"),
            ("a3", "facebook_pages", long_id),
        ]
    )
    reader = _Reader("facebook_pages", {"fb_1": {"likes": 1}, "fb_2": {"likes": 1}})
    result = social_metrics.collect(db, _org(), bundle=_bundle(reader))  # type: ignore[arg-type]
    assert result.incomplete_networks == {"facebook"}
    assert db.params("metric_points") == []


def test_daily_points_sum_each_network_and_skip_metrics_nobody_reported() -> None:
    db = _FakeDb(
        [
            ("a1", "facebook_pages", "fb_1"),
            ("a2", "facebook_pages", "fb_2"),
            ("a3", "reddit", "t3_abc"),
        ]
    )
    facebook = _Reader("facebook_pages", {"fb_1": {"likes": 2, "reach": None}, "fb_2": {"likes": 3}})
    reddit = _Reader("reddit", {"t3_abc": {"comments": 4, "score": 10, "upvote_ratio": 0.9}})
    now = utcnow()

    social_metrics.collect(db, _org(), bundle=_bundle(facebook, reddit), now=now)  # type: ignore[arg-type]

    [points] = [s for s in db.writes if s.table.name == "metric_points"]
    rows = points.compile(dialect=postgresql.dialect()).params
    values = {
        (rows[f"dimension_m{i}"], rows[f"metric_m{i}"]): rows[f"value_m{i}"]
        for i in range(len([k for k in rows if k.startswith("metric_m")]))
    }
    assert values == {("facebook", "likes"): 5.0, ("reddit", "comments"): 4.0}
    assert {rows[k] for k in rows if k.startswith("source_m")} == {"social"}
    assert {rows[k] for k in rows if k.startswith("day_m")} == {now.date()}
    assert {rows[k] for k in rows if k.startswith("tenant_id_m")} == {TENANT_ID}


def test_no_posts_means_no_connector_is_built() -> None:
    db = _FakeDb([])
    result = social_metrics.collect(db, _org(), bundle=None)  # type: ignore[arg-type]
    assert result.posts_requested == 0 and db.writes == []


def test_network_names_come_from_the_publishing_catalogue() -> None:
    assert social_metrics.network_for("facebook_pages") == "facebook"
    assert social_metrics.network_for("x_twitter") == "x"
    assert social_metrics.network_for("reddit") == "reddit"
    assert len(social_metrics.network_for("y" * 200)) == 32


# ── Points ─────────────────────────────────────────────────────────────────
def test_points_that_cannot_be_stored_are_dropped_and_duplicates_collapse() -> None:
    db = _FakeDb([])
    day = date(2026, 10, 1)
    sent = social_metrics.record_points(
        db,  # type: ignore[arg-type]
        tenant_id=TENANT_ID,
        points=[
            Point(day, "social", "likes", 1.0, "facebook"),
            Point(day, "social", "likes", 2.0, "facebook"),  # same key: last wins
            Point(day, "social", "reach", math.nan, "facebook"),
            Point(day, "social", "reach", math.inf, "facebook"),
            Point(day, "", "likes", 1.0),
            Point(day, "s" * 65, "likes", 1.0),
            Point(day, "social", "m" * 33, 1.0),
            Point(day, "social", "likes", 1.0, "d" * 129),
            Point(utcnow(), "social", "likes", 1.0),  # type: ignore[arg-type]  # a datetime is not a day
            Point(day, "social", "likes", True),  # type: ignore[arg-type]
        ],
    )
    assert sent == 1
    params = db.writes[0].compile(dialect=postgresql.dialect()).params
    assert params["value_m0"] == 2.0


def test_points_replace_the_days_value() -> None:
    db = _FakeDb([])
    social_metrics.record_points(
        db, tenant_id=TENANT_ID, points=[Point(date(2026, 10, 1), "gsc", "clicks", 4)]  # type: ignore[arg-type]
    )
    sql = str(db.writes[0].compile(dialect=postgresql.dialect()))
    assert "ON CONFLICT ON CONSTRAINT uq_metric_points_key DO UPDATE SET value = excluded.value" in sql


def test_no_points_writes_nothing() -> None:
    db = _FakeDb([])
    assert social_metrics.record_points(db, tenant_id=TENANT_ID, points=[]) == 0  # type: ignore[arg-type]
    assert db.writes == []


# ── Scheduling ─────────────────────────────────────────────────────────────
def test_in_process_collection_waits_after_start_then_runs_on_its_interval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.orchestration import scheduler as scheduler_module

    runs: list[int] = []
    monkeypatch.setattr(scheduler_module, "collect_social_metrics", lambda: runs.append(1) or 0)
    monkeypatch.setattr(scheduler_module.settings, "celery_enabled", False)
    sched = scheduler_module.AgentScheduler(tick_seconds=30)

    sched._maybe_collect_social_metrics()
    assert runs == []  # not at start-up
    sched._next_social_metrics = utcnow() - timedelta(seconds=1)
    sched._maybe_collect_social_metrics()
    sched._maybe_collect_social_metrics()
    assert runs == [1]
    assert sched._next_social_metrics > utcnow() + timedelta(
        hours=scheduler_module.settings.social_metrics_interval_hours, minutes=-1
    )


def test_in_process_collection_is_left_to_celery_when_it_is_on(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.orchestration import scheduler as scheduler_module

    runs: list[int] = []
    monkeypatch.setattr(scheduler_module, "collect_social_metrics", lambda: runs.append(1) or 0)
    monkeypatch.setattr(scheduler_module.settings, "celery_enabled", True)
    sched = scheduler_module.AgentScheduler(tick_seconds=30)
    sched._next_social_metrics = utcnow() - timedelta(seconds=1)
    sched._maybe_collect_social_metrics()
    assert runs == []


def test_collect_social_metrics_runs_per_workspace(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.orchestration import scheduler as scheduler_module

    seen: list[str] = []

    class _Session:
        def get(self, model: object, key: str) -> SimpleNamespace:
            return SimpleNamespace(id=key)

        def close(self) -> None:
            return None

    def fake_collect(db: object, org: SimpleNamespace) -> social_metrics.CollectResult:
        seen.append(org.id)
        return social_metrics.CollectResult(posts_requested=2)

    monkeypatch.setattr(scheduler_module, "SessionLocal", _Session)
    monkeypatch.setattr(
        scheduler_module,
        "run_per_organization",
        lambda db, handler, **_: [handler(db, "t1"), handler(db, "t2")],
    )
    monkeypatch.setattr(social_metrics, "collect", fake_collect)

    assert scheduler_module.collect_social_metrics() == 4
    assert seen == ["t1", "t2"]


# ── End to end (needs a database) ──────────────────────────────────────────
@requires_db
def test_collection_stores_rows_for_this_workspace_only(clean_db, org, second_org) -> None:  # noqa: ANN001
    from sqlalchemy import select

    from app.db.session import bind_tenant
    from app.models.content import AssetKind, AssetStatus, MarketingAsset
    from app.models.social import MetricPoint, SocialPostMetric

    published = utcnow() - timedelta(days=1)
    for tenant in (org, second_org):
        bind_tenant(clean_db, tenant.id)
        clean_db.add(
            MarketingAsset(
                tenant_id=tenant.id, agent_slug="social_media_manager", kind=AssetKind.SOCIAL_POST,
                channel="facebook_pages", status=AssetStatus.PUBLISHED, published_at=published,
                remote_id=f"fb_{tenant.id[:8]}",
            )
        )
        clean_db.commit()

    reader = _Reader("facebook_pages", {f"fb_{org.id[:8]}": {"likes": 7, "reach": None}})
    bind_tenant(clean_db, org.id)
    social_metrics.collect(clean_db, org, bundle=_bundle(reader))
    clean_db.commit()

    # Only this workspace's post was asked about — no cross-tenant read.
    assert reader.calls == [[f"fb_{org.id[:8]}"]]
    # Filter by tenant_id explicitly: superuser test roles bypass RLS (even
    # with FORCE), so an unscoped SELECT would still see this workspace's row
    # after switching the pin. Production still pins + filters; this asserts
    # the written row belongs only to org.
    row = clean_db.execute(
        select(SocialPostMetric).where(SocialPostMetric.tenant_id == org.id)
    ).scalar_one()
    assert row.tenant_id == org.id and row.likes == 7 and row.reach is None
    point = clean_db.execute(
        select(MetricPoint).where(
            MetricPoint.tenant_id == org.id,
            MetricPoint.source == "social",
            MetricPoint.metric == "likes",
        )
    ).scalar_one()
    assert point.dimension == "facebook" and point.value == 7.0

    bind_tenant(clean_db, second_org.id)
    # Collect for org must not have written any row for the other workspace.
    assert (
        clean_db.execute(
            select(SocialPostMetric).where(SocialPostMetric.tenant_id == second_org.id)
        ).first()
        is None
    )
    assert (
        clean_db.execute(
            select(MetricPoint).where(MetricPoint.tenant_id == second_org.id)
        ).first()
        is None
    )
