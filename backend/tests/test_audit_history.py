"""Audit history: snapshot building, diffs, pruning, brand versions, guards — no DB."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from app.core.exceptions import ConflictError, ForbiddenError, NotFoundError
from app.core.rbac import Module
from app.services import history
from app.services.history import brand, local_ranks, seo_scores, store, technical


# ── Store helpers ───────────────────────────────────────────────────────────
def test_cap_items_keeps_the_first_and_counts_all() -> None:
    items, total = store.cap_items(((str(i), i) for i in range(10)), limit=3)
    assert items == {"0": 0, "1": 1, "2": 2} and total == 10


def test_default_cap_is_5000() -> None:
    items, total = store.cap_items((str(i), i) for i in range(6000))
    assert len(items) == store.MAX_ITEMS == 5000 and total == 6000


def test_prune_drops_only_the_oldest_beyond_the_cap() -> None:
    newest_first = ["e", "d", "c", "b", "a"]
    assert store.ids_to_prune(newest_first, 3) == ["b", "a"]
    assert store.ids_to_prune(newest_first, 5) == []
    assert store.ids_to_prune(newest_first, 0) == []  # 0 or less keeps everything


def test_change_handles_missing_values() -> None:
    assert store.change(5, 8) == -3
    assert store.change(4.25, 2.0) == 2.2
    assert store.change(None, 3) is None and store.change(3, None) is None


class _FakeDb:
    def __init__(self) -> None:
        self.commits = 0
        self.rollbacks = 0
        self.savepoints = 0
        self.savepoint_rolled_back = 0

    @contextmanager
    def begin_nested(self):  # noqa: ANN201
        self.savepoints += 1
        try:
            yield
        except Exception:
            self.savepoint_rolled_back += 1
            raise

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


def test_guarded_returns_the_value_or_none_and_never_raises() -> None:
    db = _FakeDb()
    assert store.guarded(db, "ok", lambda: 42) == 42

    def boom() -> None:
        raise RuntimeError("relation audit_snapshots does not exist")

    assert store.guarded(db, "boom", boom) is None
    assert db.savepoints == 2 and db.savepoint_rolled_back == 1


def test_runner_commits_the_run_then_snapshot_failure_is_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.orchestration import runner

    def broken(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("bad")

    monkeypatch.setattr(history, "record_after_run", broken)
    db = _FakeDb()
    runner._record_history(db, SimpleNamespace(id="t1"), "technical_seo_auditor", SimpleNamespace(id="r1"))
    # The run's own work was committed before the snapshot was attempted.
    assert db.commits == 1 and db.savepoint_rolled_back == 1


def test_runner_records_and_commits_for_history_agents(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.orchestration import runner

    seen: list[str] = []
    monkeypatch.setattr(history, "record_after_run", lambda db, org, slug, run: seen.append(slug) or "snap")
    db = _FakeDb()
    runner._record_history(db, SimpleNamespace(id="t1"), "on_page_seo_sync", SimpleNamespace(id="r1"))
    assert seen == ["on_page_seo_sync"] and db.commits == 2


def test_runner_skips_agents_without_history() -> None:
    from app.orchestration import runner

    db = _FakeDb()
    runner._record_history(db, SimpleNamespace(id="t1"), "review_responder", SimpleNamespace(id="r1"))
    assert db.commits == 0 and db.savepoints == 0


def test_record_after_run_builds_from_the_agents_kind(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}
    monkeypatch.setattr(technical, "build", lambda db, tenant, run: ({"open": 3}, {"i1": ["open"]}, 1))
    monkeypatch.setattr(store, "record", lambda db, **kw: captured.update(kw) or "row")
    run = SimpleNamespace(id="run-1", trigger="manual", triggered_by="Asha")
    assert history.record_after_run(None, SimpleNamespace(id="t1"), "technical_seo_auditor", run) == "row"
    assert captured["kind"] == "technical_seo" and captured["source"] == "agent_run"
    assert captured["run_id"] == "run-1" and captured["label"] == "Run now" and captured["created_by_name"] == "Asha"
    assert history.record_after_run(None, SimpleNamespace(id="t1"), "review_responder", run) is None


# ── Technical SEO ───────────────────────────────────────────────────────────
def _issue(i: str, status: str = "open", severity: str = "high", kind: str = "thin_content") -> tuple:
    return (i, status, severity, kind, f"/p/{i}", "")


def test_technical_summary_counts_open_by_severity_and_kind() -> None:
    metrics, items, total = technical.summarise(
        [
            _issue("a"),
            _issue("b", severity="low", kind="missing_h1"),
            _issue("c", status="ignored"),
            _issue("d", kind="slow_lcp_mobile", severity="medium"),
            _issue("e", status="fixed"),  # not expected, and ignored if present
        ],
        pages_audited=12,
        vitals_measured=4,
        opened=2,
        resolved=1,
    )
    assert metrics["open"] == 3 and metrics["ignored"] == 1
    assert metrics["by_severity"] == {"high": 1, "medium": 1, "low": 1}
    assert metrics["by_kind"]["thin_content"] == 1 and metrics["speed_open"] == 1
    assert metrics["pages_audited"] == 12 and metrics["vitals_measured"] == 4
    assert total == 4 and set(items) == {"a", "b", "c", "d"}
    # Open, worst first; ignored last.
    assert list(items)[-1] == "c" and list(items)[0] == "a"


def test_technical_compare_new_fixed_ignored_still_open() -> None:
    before_m, before_i, _ = technical.summarise(
        [_issue("a"), _issue("b", severity="low"), _issue("c")], pages_audited=10
    )
    now_m, now_i, _ = technical.summarise(
        [_issue("a"), _issue("c", status="ignored"), _issue("d", severity="medium")], pages_audited=11
    )
    diff = technical.compare(now_m, now_i, before_m, before_i)
    assert [i["id"] for i in diff["new_issues"]["items"]] == ["d"]
    assert [i["id"] for i in diff["fixed"]["items"]] == ["b"]
    assert [i["id"] for i in diff["ignored_since"]["items"]] == ["c"]
    assert [i["id"] for i in diff["still_open"]["items"]] == ["a"]
    assert diff["open"] == {"before": 3, "after": 2, "change": -1}
    assert diff["severity"]["high"]["change"] == -1 and diff["severity"]["medium"]["change"] == 1
    assert diff["fixed"]["items"][0]["label"] == "Thin content"


# ── SEO & AEO scores ────────────────────────────────────────────────────────
def test_scores_only_count_analysed_pages() -> None:
    metrics, items, total = seo_scores.summarise(
        [
            ("/a", "A", 10, "live", True),
            ("/b", "B", 50, "flagged", True),
            ("/c", "C", 80, "queued", True),
            ("/d", "D", 0, "live", False),  # synced, never analysed
            ("/e", "E", 30, "rewritten", True),
        ]
    )
    assert metrics["pages_total"] == 5 and metrics["pages_scored"] == 4
    assert metrics["average_gap"] == 42.5
    assert metrics["distribution"] == {"small": 1, "some": 1, "rewrite": 1, "large": 1}
    assert metrics["rewrites_pending"] == 2 and metrics["awaiting_approval"] == 1 and metrics["rewritten"] == 1
    assert "/d" not in items and total == 4 and list(items)[0] == "/c"


def test_scores_with_nothing_analysed_have_no_average() -> None:
    metrics, items, _ = seo_scores.summarise([("/a", "A", 0, "live", False)])
    assert metrics["average_gap"] is None and items == {}


def test_scores_compare_movers_new_and_removed() -> None:
    before_m, before_i, _ = seo_scores.summarise(
        [("/a", "A", 60, "flagged", True), ("/b", "B", 20, "live", True), ("/gone", "G", 30, "live", True)]
    )
    now_m, now_i, _ = seo_scores.summarise(
        [("/a", "A", 30, "live", True), ("/b", "B", 35, "live", True), ("/new", "N", 70, "flagged", True)]
    )
    diff = seo_scores.compare(now_m, now_i, before_m, before_i)
    assert diff["improved"]["items"][0] == {"url": "/a", "title": "A", "gap": 30, "before": 60, "after": 30, "change": -30}
    assert diff["worsened"]["items"][0]["url"] == "/b" and diff["worsened"]["items"][0]["change"] == 15
    assert [p["url"] for p in diff["added"]["items"]] == ["/new"]
    assert [p["url"] for p in diff["removed"]["items"]] == ["/gone"]
    assert diff["unchanged"] == 0
    assert diff["average_gap"]["change"] == round(45.0 - 36.7, 1)


# ── Local ranks ─────────────────────────────────────────────────────────────
def _check(phrase: str, position: int | None, *, at: datetime, top: list[str] | None = None) -> SimpleNamespace:
    names = top or ["Alpha", "Beta", "Gamma", "Delta"]
    return SimpleNamespace(
        phrase=phrase, position=position, results_seen=20, checked_at=at, run_id="r", place_id="me",
        top_results=[{"position": i + 1, "place_id": n.lower(), "name": n, "rating": 4.5, "review_count": 10}
                     for i, n in enumerate(names)],
    )


def test_local_compare_moves_and_map_pack() -> None:
    t0 = datetime(2026, 10, 1, tzinfo=UTC)
    t1 = t0 + timedelta(days=1)
    before = [_check("salon near me", 5, at=t0), _check("hair salon", 2, at=t0), _check("barber", None, at=t0),
              _check("old phrase", 4, at=t0)]
    now = [_check("salon near me", 3, at=t1), _check("hair salon", 6, at=t1), _check("barber", 9, at=t1),
           _check("new phrase", 1, at=t1)]
    diff = local_ranks.compare(now, before)
    assert diff["moved_up"] == [{"phrase": "salon near me", "before": 5, "after": 3, "moved": 2}]
    assert diff["moved_down"][0]["phrase"] == "hair salon"
    assert [r["phrase"] for r in diff["entered_pack"]] == ["salon near me"]
    assert [r["phrase"] for r in diff["left_pack"]] == ["hair salon"]
    assert [r["phrase"] for r in diff["entered_results"]] == ["barber"]
    assert diff["new_phrases"] == ["new phrase"] and diff["stopped_phrases"] == ["old phrase"]
    assert local_ranks.summarise(now) == {"phrases": 4, "found_in": 4, "in_pack": 2, "best": 1, "average_position": 4.8}


def test_local_trends_and_who_ranks_above() -> None:
    t0 = datetime(2026, 10, 1, tzinfo=UTC)
    checks = local_ranks.group(
        [_check("salon", 4, at=t0), _check("salon", 2, at=t0 + timedelta(days=1)), _check("spa", None, at=t0)]
    )
    assert [m for m, _ in checks] == [t0 + timedelta(days=1), t0]
    trend = {t["phrase"]: t for t in local_ranks.trends(checks)}
    assert [p["position"] for p in trend["salon"]["points"]] == [4, 2]
    assert trend["salon"]["latest"] == 2 and [a["name"] for a in trend["salon"]["above"]] == ["Alpha"]
    # Not listed: the map pack is who ranks above.
    assert [a["name"] for a in trend["spa"]["above"]] == ["Alpha", "Beta", "Gamma"]


def test_local_check_id_round_trips_exactly() -> None:
    moment = datetime(2026, 10, 8, 9, 30, 15, 123456, tzinfo=UTC)
    assert local_ranks.moment_of(local_ranks.check_id(moment)) == moment
    with pytest.raises(NotFoundError):
        local_ranks.moment_of("not-a-number")


# ── Brand profile versions ──────────────────────────────────────────────────
PROFILE = {
    "business_name": "Glow Salon",
    "summary": "A salon.",
    "industry": "hair salon",
    "keywords": ["salon near me", "haircut"],
    "source_url": "https://glow.example",
    "generated_at": "2026-10-01T00:00:00",
    "edited": False,
}


def test_brand_same_ignores_bookkeeping_fields() -> None:
    assert brand.same(PROFILE, {**PROFILE, "edited": True, "edited_at": "x"})
    assert not brand.same(PROFILE, {**PROFILE, "tone": "warm"})


def test_brand_field_diff_against_current() -> None:
    current = {**PROFILE, "summary": "A unisex salon.", "keywords": ["haircut", "unisex salon"]}
    diff = {row["field"]: row for row in brand.field_diff(PROFILE, current)}
    assert diff["summary"]["changed"] and diff["summary"]["version"] == "A salon."
    assert diff["keywords"]["only_in_version"] == ["salon near me"]
    assert diff["keywords"]["only_in_current"] == ["unisex salon"]
    assert not diff["business_name"]["changed"]
    assert brand.changed_labels(current, PROFILE) == ["What the business does", "What customers search for"]


def test_brand_restored_profile_is_marked() -> None:
    out = brand.restored_profile({**PROFILE, "restored_at": "old"}, version_at="2026-10-01", now="2026-10-08")
    assert out["edited"] is True and out["restored_from_at"] == "2026-10-01" and out["restored_at"] == "2026-10-08"
    assert brand.same(out, PROFILE)


def test_brand_record_version_skips_an_identical_save(monkeypatch: pytest.MonkeyPatch) -> None:
    recorded: list[dict] = []
    monkeypatch.setattr(store, "latest", lambda db, tenant, kind: SimpleNamespace(items=PROFILE))
    monkeypatch.setattr(store, "record", lambda db, **kw: recorded.append(kw) or "row")
    org = SimpleNamespace(id="t1")
    assert brand.record_version(None, org, {**PROFILE, "edited": True}, source="edited") is None
    user = SimpleNamespace(id="u1", name="Asha")
    assert brand.record_version(None, org, {**PROFILE, "tone": "warm"}, source="edited", user=user) == "row"
    assert recorded[0]["created_by_name"] == "Asha" and recorded[0]["kind"] == "brand_profile"
    assert brand.record_version(None, org, None, source="edited") is None


def test_brand_restore_writes_the_version_and_records_a_new_one(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services import brand_profile

    current = {**PROFILE, "summary": "Changed later."}
    org = SimpleNamespace(id="t1", settings={"brand_profile": current})
    recorded: list[dict] = []
    stored: list[dict] = []
    monkeypatch.setattr(store, "latest", lambda db, tenant, kind: SimpleNamespace(items=current))
    monkeypatch.setattr(store, "record", lambda db, **kw: recorded.append(kw) or "row")
    monkeypatch.setattr(brand_profile, "store", lambda o, p: stored.append(p))
    db = SimpleNamespace(flush=lambda: None)
    version = SimpleNamespace(items=PROFILE, created_at=datetime(2026, 10, 1, 9, 0, tzinfo=UTC))

    out = brand.restore(db, org, version, user=SimpleNamespace(id="u1", name="Asha"))
    assert stored and stored[0]["summary"] == "A salon." and out["summary"] == "A salon."
    # The current profile was already the latest version, so only the restore is added.
    assert len(recorded) == 1 and recorded[0]["source"] == "restored"
    assert recorded[0]["label"] == "Restored the version from 01 Oct 2026, 09:00"

    with pytest.raises(ConflictError):
        brand.restore(db, SimpleNamespace(id="t1", settings={"brand_profile": PROFILE}), version, user=None)


# ── API: routes, guards, plan ───────────────────────────────────────────────
def test_routes_are_registered() -> None:
    from app.api.v1.router import api_router
    from tests.support.routes import route_path_methods

    paths = route_path_methods(api_router)
    assert ("/history/{kind}", ("GET",)) in paths
    assert ("/history/{kind}/{entry_id}", ("GET",)) in paths
    assert ("/history/brand_profile/{entry_id}/restore", ("POST",)) in paths


def test_kinds_map_to_the_right_module() -> None:
    assert history.module_for("technical_seo") is Module.SEO
    assert history.module_for("seo_scores") is Module.SEO
    assert history.module_for("local_ranks") is Module.SEO
    assert history.module_for("brand_profile") is Module.CONTENT
    with pytest.raises(NotFoundError):
        history.module_for("everything")


class _Denied:
    """A caller with no access to anything; records what was checked."""

    def __init__(self) -> None:
        self.checked: list[tuple[str, Module]] = []

    def require_view(self, module: Module) -> None:
        self.checked.append(("view", module))
        raise ForbiddenError("No access")

    def require_write(self, module: Module) -> None:
        self.checked.append(("write", module))
        raise ForbiddenError("View-only access for your role")


@pytest.mark.parametrize(
    ("kind", "module"),
    [("technical_seo", Module.SEO), ("seo_scores", Module.SEO), ("local_ranks", Module.SEO),
     ("brand_profile", Module.CONTENT)],
)
def test_reading_history_is_guarded_before_the_database(kind: str, module: Module) -> None:
    from app.api.v1 import history as routes

    for call in (lambda c: routes.list_history(kind, c, None, 10), lambda c: routes.read_history_entry(kind, "x", c, None)):
        caller = _Denied()
        with pytest.raises(ForbiddenError):
            call(caller)
        assert caller.checked == [("view", module)]


def test_restore_needs_content_write() -> None:
    from app.api.v1 import history as routes

    caller = _Denied()
    with pytest.raises(ForbiddenError):
        routes.restore_brand_profile("x", caller, None)
    assert caller.checked == [("write", Module.CONTENT)]


def test_plan_can_switch_off_or_shorten_history(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services import entitlements

    monkeypatch.setattr(entitlements, "feature_policy", lambda db, org, f: entitlements.FeaturePolicy())
    assert store.visible_limit(None, None, 60) == 60
    monkeypatch.setattr(entitlements, "feature_policy", lambda db, org, f: entitlements.FeaturePolicy(max_history=7))
    assert store.visible_limit(None, None, 60) == 7
    monkeypatch.setattr(
        entitlements, "feature_policy", lambda db, org, f: entitlements.FeaturePolicy(allowed=False, reason="On Pro")
    )
    with pytest.raises(ForbiddenError, match="On Pro"):
        store.visible_limit(None, None, 60)


def test_table_is_tenant_scoped_and_migrated() -> None:
    import importlib.util
    from pathlib import Path

    from app.db.rls import TENANT_SCOPED_TABLES
    from app.models import AuditSnapshot

    assert "audit_snapshots" in TENANT_SCOPED_TABLES
    assert AuditSnapshot.__table__.c.tenant_id.foreign_keys
    path = Path(__file__).resolve().parents[1] / "alembic" / "versions" / "0015_audit_history.py"
    spec = importlib.util.spec_from_file_location("m0015", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    assert module.revision == "0015_audit_history" and module.down_revision == "0014_demo_analysis"
    assert "apply_policies" in path.read_text(encoding="utf-8")


def test_retention_setting_defaults_to_365() -> None:
    from app.core.config import settings

    assert settings.audit_history_max_per_kind == 365
