"""Version history: numbering, comparison in plain words, restore rules, retention, audit.

The pure parts (comparison shapes, retention arithmetic, adapters, guards)
run without a database. The rest is marked ``requires_db``: numbering under
concurrency, another workspace's version, restore and prune audit rows.
"""
from __future__ import annotations

import json
import threading
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from app.core.exceptions import ConflictError, ForbiddenError, InvalidInputError, NotFoundError
from app.core.rbac import Module
from app.services import history
from app.services.history import config, offpage, site_analysis, store
from tests.conftest import requires_db

KIT = {
    "name": "Main kit",
    "is_default": True,
    "logo_file_id": "aaaaaaaa-0000-4000-8000-000000000001",
    "primary_color": "#1F2937",
    "secondary_color": "#4b5563",
    "accent_color": "#2563eb",
    "text_color": "#111827",
    "background_color": "#ffffff",
    "heading_font": "inter",
    "body_font": "inter",
    "voice": "Warm and plain-spoken.",
    "banned_phrases": ["cheap"],
    "disclaimer": "",
}
KIT_ID = "cccccccc-0000-4000-8000-000000000001"


def _plain(value: Any) -> None:  # noqa: ANN401
    """A comparison holds only flat, plain values: never a nested document."""
    for group in ("new", "fixed", "worse", "changed"):
        for row in value[group]:
            assert set(row) == {"label", "where", "before", "after"}
            for field in row.values():
                assert field is None or isinstance(field, str)
                assert not (isinstance(field, str) and field.lstrip().startswith(("{", "[")))


# ── Retention arithmetic ────────────────────────────────────────────────────
NOW = datetime(2026, 10, 21, tzinfo=UTC)


def _rows(n: int, *, days_apart: int = 1) -> list[tuple[str, datetime]]:
    return [(f"v{n - i}", NOW - timedelta(days=i * days_apart)) for i in range(n)]


def test_retention_keeps_the_newest_versions() -> None:
    assert store.retention_doomed(_rows(8), keep_versions=5, keep_days=0, min_kept=5, now=NOW) == ["v3", "v2", "v1"]


def test_retention_never_keeps_fewer_than_the_minimum() -> None:
    # Everything is older than a day, and the policy asks for 1 version: the minimum still stands.
    doomed = store.retention_doomed(_rows(8, days_apart=10), keep_versions=1, keep_days=1, min_kept=5, now=NOW)
    assert doomed == ["v3", "v2", "v1"]


def test_retention_by_age_only_removes_old_versions_beyond_the_minimum() -> None:
    rows = _rows(10, days_apart=10)  # v10 today, v9 10 days ago, …
    doomed = store.retention_doomed(rows, keep_versions=365, keep_days=35, min_kept=2, now=NOW)
    assert doomed == ["v6", "v5", "v4", "v3", "v2", "v1"]
    assert store.retention_doomed(rows, keep_versions=365, keep_days=0, min_kept=2, now=NOW) == []


def test_retention_policy_reads_overrides_and_ignores_junk(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core.config import settings

    monkeypatch.setattr(settings, "history_min_versions_kept", 5)
    monkeypatch.setattr(settings, "history_keep_versions_default", 365)
    monkeypatch.setattr(settings, "history_keep_days_default", 0)
    assert store.retention_for({}, "brand_kit") == store.Retention(365, 0, False)
    custom = {"history_retention": {"brand_kit": {"keep_versions": 20, "keep_days": 90}}}
    assert store.retention_for(custom, "brand_kit") == store.Retention(20, 90, True)
    # Below the minimum is lifted to it; wrong types and out-of-range values fall back.
    assert store.retention_for({"history_retention": {"brand_kit": {"keep_versions": 2}}}, "brand_kit").keep_versions == 5
    junk = {"history_retention": {"brand_kit": {"keep_versions": "all", "keep_days": -4}}}
    assert store.retention_for(junk, "brand_kit") == store.Retention(365, 0, False)
    assert store.retention_for({"history_retention": "nonsense"}, "brand_kit").keep_versions == 365
    assert store.retention_for({"history_retention": {"brand_kit": {"keep_versions": True}}}, "brand_kit").custom is False


# ── Comparison shape ────────────────────────────────────────────────────────
def test_summary_is_one_plain_sentence() -> None:
    counts = {"new": 2, "fixed": 5, "worse": 1, "changed": 0, "unchanged": 40}
    assert store.summary_text(counts, noun=("issue", "issues")) == "2 new issues, 5 fixed and 1 got worse; 40 unchanged."
    assert store.summary_text({"new": 0, "unchanged": 3}, noun=("issue", "issues")) == (
        "Nothing changed between these two versions."
    )
    assert store.summary_text({"changed": 1}, noun=("setting", "settings")) == "1 setting changed."


def test_comparison_counts_everything_but_lists_a_bounded_number() -> None:
    rows = [store.change_row(f"Issue {i}") for i in range(store.COMPARE_LIST_LIMIT + 50)]
    out = store.comparison(noun=("issue", "issues"), new=rows)
    assert out["counts"]["new"] == store.COMPARE_LIST_LIMIT + 50
    assert len(out["new"]) == store.COMPARE_LIST_LIMIT


def test_measure_verdicts() -> None:
    assert store.measure("Open issues", 10, 7, better="lower")["verdict"] == "better"
    assert store.measure("Score", 70, 60, better="higher")["verdict"] == "worse"
    assert store.measure("Score", 70, 70, better="higher")["verdict"] == "same"
    assert store.measure("Score", None, 70, better="higher")["verdict"] is None


def test_change_rows_are_capped_strings() -> None:
    row = store.change_row("x" * 500, where="/p", before=3, after="y" * 900)
    assert len(row["label"]) == 200 and row["before"] == "3" and len(row["after"]) == 300


# ── Cursors and ids ─────────────────────────────────────────────────────────
def test_cursor_round_trips_and_refuses_forgeries() -> None:
    row = SimpleNamespace(created_at=datetime(2026, 10, 1, 9, tzinfo=UTC), id="11111111-1111-4111-8111-111111111111")
    assert store.decode_cursor(store.encode_cursor(row)) == (row.created_at, row.id)
    for bad in ("", "not-base64!!", "x" * 500, "MjAyNnxub3QtYS11dWlk"):
        with pytest.raises(InvalidInputError):
            store.decode_cursor(bad)


def test_a_malformed_version_id_is_not_found_without_a_query() -> None:
    class _NoDb:
        def scalars(self, *_a: Any) -> None:
            raise AssertionError("must not query")

    for bad in ("1 OR 1=1", "../../etc", "not-a-uuid"):
        with pytest.raises(NotFoundError):
            store.get(_NoDb(), "t1", "brand_kit", bad)


def test_lock_key_is_per_subject() -> None:
    assert store.lock_key("t1", "brand_kit", "a") != store.lock_key("t1", "brand_kit", "b")
    assert store.lock_key("t1", "brand_kit", "a") != store.lock_key("t2", "brand_kit", "a")


# ── Config-like kinds ───────────────────────────────────────────────────────
def test_config_snapshot_keeps_words_out_of_the_clear() -> None:
    snap = config.snapshot("brand_kit", KIT)
    assert snap.secret == KIT
    # Only structural fields are copied in the clear: no name, voice or phrases.
    assert "voice" not in snap.inputs and "name" not in snap.inputs and "banned_phrases" not in snap.inputs
    assert snap.inputs["primary_color"] == "#1f2937" and snap.inputs["heading_font"] == "inter"
    assert snap.items == {} and snap.metrics["fields_total"] == len(config.FIELDS["brand_kit"])


def test_config_snapshot_refuses_oversized_or_odd_documents() -> None:
    with pytest.raises(InvalidInputError):
        config.snapshot("brand_kit", {**KIT, "voice": "x" * (config.MAX_DOCUMENT_BYTES + 1)})
    with pytest.raises(InvalidInputError):
        config.snapshot("brand_kit", ["not", "a", "document"])


def test_brand_kit_compare_names_each_change_in_plain_words() -> None:
    after = {**KIT, "primary_color": "#ff0000", "heading_font": "playfair_display", "banned_phrases": ["cheap", "free"]}
    out = config.compare("brand_kit", KIT, after)
    _plain(out)
    changed = {r["label"]: r for r in out["changed"]}
    assert changed["Main colour"] == {"label": "Main colour", "where": None, "before": "#1F2937", "after": "#FF0000"}
    assert changed["Heading font"]["after"] == "Playfair Display"
    assert changed["Words to avoid"]["after"] == "cheap, free"
    assert out["counts"]["changed"] == 3 and out["counts"]["unchanged"] == len(config.FIELDS["brand_kit"]) - 3
    assert out["summary"] == f"3 settings changed; {out['counts']['unchanged']} unchanged."
    # Colour case alone is not a change.
    assert config.same("brand_kit", KIT, {**KIT, "primary_color": "#1f2937"})


def test_agent_settings_options_are_listed_one_by_one() -> None:
    before = {"schedule": "Daily", "settings": {"strict_model": True, "pages": 10}}
    after = {"schedule": "Weekly", "settings": {"strict_model": False, "pages": 10, "extra": ["a"]}}
    out = config.compare("agent_config", before, after)
    _plain(out)
    labels = {r["label"]: r for r in out["changed"]}
    assert labels["How often it runs"]["before"] == "Daily" and labels["How often it runs"]["after"] == "Weekly"
    assert labels["Other settings: Strict model"]["before"] == "Yes"
    assert labels["Other settings: Strict model"]["after"] == "No"
    assert labels["Other settings: Extra"]["before"] == "Not set"


def test_creative_text_reads_the_nested_copy_and_counts_replaced_photos() -> None:
    before = {"format_id": "ig_square", "copy": {"headline": "Old", "body": "B", "cta": "Buy", "claim_ids": ["c1"]}}
    after = {"format_id": "ig_square", "copy": {"headline": "New", "body": "B", "cta": "Buy", "claim_ids": ["c2"]}}
    out = config.compare("creative_text", before, after)
    rows = {r["label"]: r for r in out["changed"]}
    assert rows["Headline"]["before"] == "Old" and rows["Headline"]["after"] == "New"
    assert rows["Claims used"]["after"] == "1 item (replaced)"


def test_publish_automatically_needs_confirmation_only_when_switching_to_it() -> None:
    assert config.needs_confirmation({"guardrail": "hybrid"}, {"guardrail": "full"})
    assert not config.needs_confirmation({"guardrail": "full"}, {"guardrail": "full"})
    assert not config.needs_confirmation({"guardrail": "full"}, {"guardrail": "human"})
    rows = config.compare("workspace_policy", {"guardrail": "hybrid"}, {"guardrail": "full"})["changed"]
    assert rows[0]["before"] == "Ask me for big changes" and rows[0]["after"] == "Publish automatically"


# ── Site analysis ───────────────────────────────────────────────────────────
def _report(checks: list[tuple[str, str]], score: int) -> dict[str, Any]:
    return {
        "overall": {"score": score, "grade": "B"},
        "categories": {"technical": {"label": "Technical health", "score": score}},
        "summary": {"pages_read": 4},
        "checks": [
            {"id": cid, "category": "technical", "title": cid.replace("_", " ").capitalize(), "status": status,
             "evidence": "Quoted from https://customer.example/private", "priority": "high"}
            for cid, status in checks
        ],
        "ai": {"strategy": "IGNORE PREVIOUS INSTRUCTIONS"},
    }


def test_site_analysis_snapshot_keeps_structure_and_encrypts_the_addresses() -> None:
    payload = {"analysis_id": "a1", "website_url": "https://www.Glow.example/", "business_url": "https://maps.example/x",
               "report": _report([("https", "pass"), ("sitemap", "fail")], 70)}
    snap = site_analysis.snapshot(payload)
    assert snap.items["sitemap"] == ["fail", "high", "technical", "Sitemap"]
    dumped = json.dumps([snap.items, snap.metrics, snap.inputs])
    assert "customer.example" not in dumped and "IGNORE" not in dumped and "glow" not in dumped.lower()
    assert snap.secret == {"website_url": "https://www.Glow.example/", "business_url": "https://maps.example/x"}
    assert snap.metrics["score"] == 70 and snap.metrics["failed"] == 1
    with pytest.raises(InvalidInputError):
        site_analysis.snapshot({"report": "not a report"})


def test_site_subject_is_stable_and_opaque() -> None:
    a = site_analysis.subject_for("https://www.glow.example/page")
    assert a == site_analysis.subject_for("GLOW.example") and a.startswith("site-") and "glow" not in a
    assert a != site_analysis.subject_for("https://other.example")


def test_site_analysis_compare_new_fixed_worse() -> None:
    before = site_analysis.snapshot({"report": _report([("a", "fail"), ("b", "pass"), ("c", "warn"), ("d", "fail")], 60)})
    after = site_analysis.snapshot({"report": _report([("a", "pass"), ("b", "fail"), ("c", "fail"), ("d", "fail"),
                                                       ("e", "warn")], 55)})
    out = site_analysis.compare(before.metrics, before.items, after.metrics, after.items)
    _plain(out)
    assert out["counts"] == {"new": 2, "fixed": 1, "worse": 1, "changed": 0, "unchanged": 1}
    assert [r["label"] for r in out["new"]] == ["B", "E"]
    assert out["fixed"][0] == {"label": "A", "where": None, "before": "Failing", "after": "Passing"}
    assert out["measures"][0]["verdict"] == "worse"


# ── Off-page scans ──────────────────────────────────────────────────────────
def test_backlink_compare() -> None:
    bm, bi, _ = offpage.BacklinkScan.summarise(
        [("a.example", "Discovered", 40, "Guest Post"), ("b.example", "Won", 60, "Directory"),
         ("c.example", "Pitched", 30, "Guest Post"), ("d.example", "Won", 20, "Directory")]
    )
    am, ai, _ = offpage.BacklinkScan.summarise(
        [("a.example", "Won", 40, "Guest Post"), ("b.example", "Won", 60, "Directory"),
         ("c.example", "Rejected", 30, "Guest Post"), ("e.example", "Discovered", 50, "Resource Page")]
    )
    out = offpage.BacklinkScan.compare(am, ai, bm, bi)
    _plain(out)
    assert [r["label"] for r in out["new"]] == ["e.example"]
    assert [r["label"] for r in out["fixed"]] == ["a.example"]
    assert sorted(r["label"] for r in out["worse"]) == ["c.example", "d.example"]
    assert out["counts"]["unchanged"] == 1


def test_competitor_compare() -> None:
    bm, bi, _ = offpage.CompetitorScan.summarise([("1", "Rival", "news.example", 70, False)])
    am, ai, _ = offpage.CompetitorScan.summarise(
        [("1", "Rival", "news.example", 70, True), ("2", "Rival", "blog.example", 20, False)]
    )
    out = offpage.CompetitorScan.compare(am, ai, bm, bi)
    assert out["counts"]["new"] == 1 and out["counts"]["fixed"] == 1
    assert out["new"][0] == {"label": "Rival got a link", "where": "blog.example", "before": None,
                             "after": "Site strength 20"}


# ── Technical, scores, local ranks adapters ─────────────────────────────────
def test_technical_changes() -> None:
    from app.services.history import technical

    def issue(i: str, status: str = "open", severity: str = "high", kind: str = "thin_content") -> tuple:
        return (i, status, severity, kind, f"/p/{i}", "")

    bm, bi, _ = technical.summarise([issue("a", severity="low"), issue("b"), issue("c"), issue("z")], pages_audited=5)
    am, ai, _ = technical.summarise(
        [issue("a", severity="high"), issue("c", status="ignored"), issue("z"), issue("d", severity="medium")],
        pages_audited=5,
    )
    out = history.technical_changes(bm, bi, am, ai)
    _plain(out)
    assert out["counts"] == {"new": 1, "fixed": 1, "worse": 1, "changed": 0, "unchanged": 1}
    assert out["fixed"][0]["where"] == "/p/b" and out["fixed"][0]["label"] == "Thin content"
    assert out["worse"][0]["before"] == "Low priority" and out["worse"][0]["after"] == "High priority"


def test_score_changes() -> None:
    from app.services.history import seo_scores

    bm, bi, _ = seo_scores.summarise([("/a", "A", 60, "live", True), ("/b", "B", 20, "live", True)])
    am, ai, _ = seo_scores.summarise(
        [("/a", "A", 30, "live", True), ("/b", "B", 35, "live", True), ("/c", "C", 10, "live", True)]
    )
    out = history.score_changes(bm, bi, am, ai)
    assert out["counts"] == {"new": 1, "fixed": 1, "worse": 1, "changed": 0, "unchanged": 0}
    assert out["fixed"][0]["before"] == "Gap 60 of 100" and out["summary"].startswith("1 new page, 1 improved")


def test_local_changes() -> None:
    def check(phrase: str, position: int | None) -> SimpleNamespace:
        return SimpleNamespace(phrase=phrase, position=position, results_seen=20, top_results=[], place_id="me")

    before = [check("salon", 5), check("spa", 2), check("barber", None)]
    after = [check("salon", 3), check("spa", 6), check("barber", 9), check("nails", 1)]
    out = history.local_changes(before, after)
    _plain(out)
    assert out["counts"] == {"new": 1, "fixed": 2, "worse": 1, "changed": 0, "unchanged": 0}
    assert out["worse"][0] == {"label": "spa", "where": None, "before": "Position 2", "after": "Position 6"}
    assert "moved up" in out["summary"]


# ── Registry and RBAC ───────────────────────────────────────────────────────
def test_every_kind_has_the_right_module_and_restore_rule() -> None:
    expected = {
        "technical_seo": (Module.SEO, False), "seo_scores": (Module.SEO, False), "local_ranks": (Module.SEO, False),
        "site_analysis": (Module.SEO, False), "backlink_scan": (Module.OFFPAGE, False),
        "competitor_scan": (Module.OFFPAGE, False), "brand_profile": (Module.CONTENT, True),
        "brand_kit": (Module.BRAND_KIT, True), "product": (Module.BRAND_KIT, True),
        "agent_config": (Module.AGENTS, True), "workspace_policy": (Module.ADMIN, True),
        "creative_text": (Module.CONTENT, True),
    }
    assert {k: (s.module, s.restorable) for k, s in history.KINDS.items()} == expected
    assert history.AGENT_KINDS["backlink_node_discovery"] == "backlink_scan"
    assert history.AGENT_KINDS["competitor_link_monitor"] == "competitor_scan"


def test_only_restorable_kinds_take_an_applier() -> None:
    with pytest.raises(ValueError):
        history.register_restorer("technical_seo")
    with pytest.raises(ValueError):
        history.register_restorer("no_such_kind")


class _Denied:
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
    [("brand_kit", Module.BRAND_KIT), ("product", Module.BRAND_KIT), ("agent_config", Module.AGENTS),
     ("workspace_policy", Module.ADMIN), ("site_analysis", Module.SEO), ("backlink_scan", Module.OFFPAGE),
     ("creative_text", Module.CONTENT)],
)
def test_every_route_checks_the_kinds_module_before_the_database(kind: str, module: Module) -> None:
    from app.api.v1 import history as routes

    calls = [
        ("view", lambda c: routes.list_history(kind, c, None, 10)),
        ("view", lambda c: routes.read_history_entry(kind, "x", c, None)),
        ("view", lambda c: routes.compare_versions(kind, c, None, "x", "y")),
        ("write", lambda c: routes.restore_version(kind, "x", c, None, routes.RestoreIn(confirm=True))),
    ]
    for level, call in calls:
        caller = _Denied()
        with pytest.raises(ForbiddenError):
            call(caller)
        assert caller.checked == [(level, module)]


def test_retention_routes_need_admin() -> None:
    from app.api.v1 import history as routes

    caller = _Denied()
    with pytest.raises(ForbiddenError):
        routes.read_retention(caller, None)
    assert caller.checked == [("view", Module.ADMIN)]
    caller = _Denied()
    with pytest.raises(ForbiddenError):
        routes.update_retention(routes.RetentionIn(kinds={}), caller, None)
    assert caller.checked == [("write", Module.ADMIN)]


def test_retention_input_is_validated() -> None:
    from pydantic import ValidationError

    from app.api.v1 import history as routes

    for bad in ({"kinds": {"brand_kit": {"keep_versions": 0}}}, {"kinds": {"brand_kit": {"keep_days": -1}}},
                {"kinds": {"brand_kit": {"keep_versions": 10, "delete_all": True}}},
                {"kinds": {f"k{i}": {} for i in range(40)}}):
        with pytest.raises(ValidationError):
            routes.RetentionIn.model_validate(bad)


def test_routes_are_registered_in_a_safe_order() -> None:
    from app.api.v1 import history as routes

    order = [(r.path, tuple(sorted(r.methods))) for r in routes.router.routes]
    assert order.index(("/history/retention", ("GET",))) < order.index(("/history/{kind}", ("GET",)))
    assert order.index(("/history/{kind}/compare", ("GET",))) < order.index(("/history/{kind}/{entry_id}", ("GET",)))
    assert order.index(("/history/brand_profile/{entry_id}/restore", ("POST",))) < order.index(
        ("/history/{kind}/{entry_id}/restore", ("POST",))
    )
    assert ("/history/retention", ("PUT",)) in order


# ── record_version and restore, without a database ──────────────────────────
class _FakeCipher:
    def encrypt_json(self, document: Any, *, context: str) -> str:  # noqa: ANN401
        assert context == "audit_snapshot.payload"
        return "enc:" + json.dumps(document)

    def decrypt_json(self, token: str, *, context: str, default: Any = None) -> Any:  # noqa: ANN401
        assert context == "audit_snapshot.payload"
        return json.loads(token[4:]) if token.startswith("enc:") else default


@pytest.fixture
def fake_store(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    state: dict[str, Any] = {"recorded": [], "latest": None, "rows": {}, "audit": []}
    monkeypatch.setattr(history, "_cipher", lambda org: _FakeCipher())
    monkeypatch.setattr(store, "latest_of", lambda db, tenant, kind, subject: state["latest"])

    def record(db: Any, **kw: Any) -> SimpleNamespace:  # noqa: ANN401
        state["recorded"].append(kw)
        return SimpleNamespace(id="new", version=len(state["recorded"]) + 1, **{
            k: kw.get(k, "") for k in ("kind", "subject_id", "source", "label", "run_id", "created_by_name",
                                       "actor_type", "agent_slug", "model")
        }, created_at=NOW, items={}, item_count=0)

    def get(db: Any, tenant: str, kind: str, entry_id: str) -> Any:  # noqa: ANN401
        row = state["rows"].get(entry_id)
        if row is None or row.tenant_id != tenant or row.kind != kind:
            raise NotFoundError("That history entry was not found")
        return row

    monkeypatch.setattr(store, "record", record)
    monkeypatch.setattr(store, "get", get)
    from app.services import audit

    monkeypatch.setattr(audit, "record_user_action", lambda db, **kw: state["audit"].append(kw))
    return state


ORG = SimpleNamespace(id="t1", settings={})
USER = SimpleNamespace(id="u1", name="Asha", tenant_id="t1")


def _row(kind: str, document: dict[str, Any], *, version: int, subject: str = "", tenant: str = "t1") -> SimpleNamespace:
    return SimpleNamespace(id=f"r{version}", tenant_id=tenant, kind=kind, subject_id=subject, version=version,
                           payload_encrypted="enc:" + json.dumps(document), items={}, item_count=0,
                           created_at=NOW, source="saved", label="", created_by_name="", run_id="")


def test_record_version_encrypts_the_document_and_audits_by_actor(fake_store: dict[str, Any]) -> None:
    history.record_version(None, ORG, "brand_kit", KIT_ID, KIT, actor=USER)
    kw = fake_store["recorded"][0]
    assert kw["kind"] == "brand_kit" and kw["subject_id"] == KIT_ID and kw["actor_type"] == "user"
    assert kw["payload_encrypted"].startswith("enc:") and "Warm and plain-spoken" not in json.dumps(kw["inputs"])
    assert kw["label"] == "Brand kit saved" and kw["source"] == "saved" and kw["created_by_name"] == "Asha"

    history.record_version(None, ORG, "agent_config", "agent-1", {"schedule": "Daily"}, actor=None,
                           agent_slug="seo_assistant", model="model-x", summary="Changed by the assistant")
    kw = fake_store["recorded"][1]
    assert kw["actor_type"] == "agent" and kw["agent_slug"] == "seo_assistant" and kw["model"] == "model-x"
    history.record_version(None, ORG, "workspace_policy", "", {"guardrail": "human"}, actor=None)
    assert fake_store["recorded"][2]["actor_type"] == "system"


def test_record_version_skips_an_identical_save(fake_store: dict[str, Any]) -> None:
    fake_store["latest"] = _row("brand_kit", KIT, version=3, subject=KIT_ID)
    assert history.record_version(None, ORG, "brand_kit", KIT_ID, {**KIT, "primary_color": "#1f2937"}, actor=USER) is None
    assert history.record_version(None, ORG, "brand_kit", KIT_ID, {**KIT, "voice": "Bold"}, actor=USER) is not None


@pytest.mark.parametrize(
    ("kind", "subject", "slug"),
    [("technical_seo", "", None), ("brand_kit", "", None), ("workspace_policy", "x", None),
     ("brand_kit", "bad subject; drop", None), ("brand_kit", KIT_ID, "Not A Slug"), ("nope", "", None)],
)
def test_record_version_refuses_misuse(fake_store: dict[str, Any], kind: str, subject: str, slug: str | None) -> None:
    with pytest.raises(ValueError):
        history.record_version(None, ORG, kind, subject, KIT, actor=None, agent_slug=slug)
    assert fake_store["recorded"] == []


def test_an_analysis_cannot_be_restored(fake_store: dict[str, Any]) -> None:
    with pytest.raises(ConflictError) as caught:
        history.restore(None, ORG, "technical_seo", "r1", user=USER)
    assert caught.value.status_code == 409 and fake_store["audit"] == []


def test_policy_restore_to_publish_automatically_needs_confirm(
    fake_store: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    target = _row("workspace_policy", {"guardrail": "full", "autonomous": True}, version=1)
    fake_store["rows"]["r1"] = target
    fake_store["latest"] = _row("workspace_policy", {"guardrail": "hybrid", "autonomous": True}, version=2)
    monkeypatch.setattr(config, "current_policy", lambda db, org: {"guardrail": "hybrid", "autonomous": True})
    applied: list[dict[str, Any]] = []
    monkeypatch.setitem(history.RESTORERS, "workspace_policy",
                        lambda db, org, subject, document, *, user: applied.append(document) or None)
    db = SimpleNamespace(flush=lambda: None)

    with pytest.raises(InvalidInputError) as caught:
        history.restore(db, ORG, "workspace_policy", "r1", user=USER)
    assert caught.value.status_code == 422 and caught.value.details["needs_confirmation"] is True
    assert applied == [] and fake_store["recorded"] == []

    out = history.restore(db, ORG, "workspace_policy", "r1", user=USER, confirm=True)
    assert applied == [{"guardrail": "full", "autonomous": True}]
    assert fake_store["recorded"][0]["source"] == "restored" and fake_store["recorded"][0]["label"] == "Restored version 1"
    assert fake_store["audit"][0]["action"] == "restored version 1 of the workspace policy"
    assert out["restored_version"] == 1


def test_restore_refuses_the_current_version_and_a_missing_applier(
    fake_store: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_store["rows"]["r1"] = _row("brand_kit", KIT, version=1, subject=KIT_ID)
    fake_store["latest"] = _row("brand_kit", KIT, version=2, subject=KIT_ID)
    with pytest.raises(ConflictError, match="already the current"):
        history.restore(None, ORG, "brand_kit", "r1", user=USER)
    fake_store["latest"] = _row("brand_kit", {**KIT, "voice": "Bold"}, version=2, subject=KIT_ID)
    monkeypatch.delitem(history.RESTORERS, "brand_kit", raising=False)
    with pytest.raises(ConflictError, match="isn't available yet"):
        history.restore(None, ORG, "brand_kit", "r1", user=USER)
    assert fake_store["recorded"] == [] and fake_store["audit"] == []


def test_another_workspaces_version_is_not_found(fake_store: dict[str, Any]) -> None:
    fake_store["rows"]["r1"] = _row("brand_kit", KIT, version=1, subject=KIT_ID, tenant="someone-else")
    with pytest.raises(NotFoundError):
        history.restore(None, ORG, "brand_kit", "r1", user=USER)


def test_set_retention_validates_kinds_and_the_minimum(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core.config import settings
    from app.services import audit

    monkeypatch.setattr(settings, "history_min_versions_kept", 5)
    logged: list[dict[str, Any]] = []
    monkeypatch.setattr(audit, "record_user_action", lambda db, **kw: logged.append(kw))
    org = SimpleNamespace(id="t1", settings={"other": 1})
    db = SimpleNamespace(flush=lambda: None)
    with pytest.raises(InvalidInputError):
        history.set_retention(db, org, {"brand_kit": {"keep_versions": 2, "keep_days": None}}, user=USER)
    with pytest.raises(InvalidInputError):
        history.set_retention(db, org, {"local_ranks": {"keep_versions": 10, "keep_days": None}}, user=USER)
    out = history.set_retention(db, org, {"brand_kit": {"keep_versions": 20, "keep_days": 30}}, user=USER)
    assert org.settings == {"other": 1, "history_retention": {"brand_kit": {"keep_versions": 20, "keep_days": 30}}}
    kit = next(k for k in out["kinds"] if k["kind"] == "brand_kit")
    assert kit == {"kind": "brand_kit", "name": "Brand kit", "keep_versions": 20, "keep_days": 30, "custom": True}
    assert logged[-1]["context"] == {"kinds": ["brand_kit"]}
    history.set_retention(db, org, {"brand_kit": {"keep_versions": None, "keep_days": None}}, user=USER)
    assert org.settings["history_retention"] == {}


# ── Database ────────────────────────────────────────────────────────────────
def _audit_actions(db: Any, tenant_id: str) -> list[str]:  # noqa: ANN401
    from sqlalchemy import select

    from app.models.workspace import AuditLogEntry

    return list(db.scalars(select(AuditLogEntry.action).where(AuditLogEntry.tenant_id == tenant_id)))


@requires_db
def test_db_versions_are_numbered_without_gaps_under_concurrency(clean_db, org) -> None:  # noqa: ANN001
    from sqlalchemy import select

    from app.db.session import SessionLocal, bind_tenant
    from app.models.history import AuditSnapshot
    from app.models.workspace import Organization

    errors: list[BaseException] = []
    barrier = threading.Barrier(2)

    def write(voice: str) -> None:
        session = SessionLocal()
        try:
            bind_tenant(session, org.id)
            own = session.get(Organization, org.id)
            barrier.wait(timeout=10)
            for i in range(3):
                history.record_version(session, own, "brand_kit", KIT_ID, {**KIT, "voice": f"{voice} {i}"}, actor=None)
                session.commit()
        except BaseException as exc:  # noqa: BLE001 - reported below
            errors.append(exc)
            session.rollback()
        finally:
            session.close()

    threads = [threading.Thread(target=write, args=(name,)) for name in ("left", "right")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert errors == []
    bind_tenant(clean_db, org.id)
    versions = sorted(clean_db.scalars(
        select(AuditSnapshot.version).where(AuditSnapshot.tenant_id == org.id, AuditSnapshot.kind == "brand_kit")
    ))
    assert versions == [1, 2, 3, 4, 5, 6]
    assert sum(a.startswith("recorded version") for a in _audit_actions(clean_db, org.id)) == 6


@requires_db
def test_db_compare_restore_and_tenant_isolation(clean_db, org, second_org, monkeypatch) -> None:  # noqa: ANN001
    from app.db.session import bind_tenant
    from app.models.workspace import User

    bind_tenant(clean_db, org.id)
    user = clean_db.query(User).filter(User.tenant_id == org.id).first()
    first = history.record_version(clean_db, org, "brand_kit", KIT_ID, KIT, actor=user)
    second = history.record_version(
        clean_db, org, "brand_kit", KIT_ID, {**KIT, "primary_color": "#ff0000", "voice": "Bold"}, actor=user
    )
    clean_db.commit()
    assert (first.version, second.version) == (1, 2)
    # The customer's words are not stored in the clear.
    assert "Bold" not in second.payload_encrypted and "Bold" not in json.dumps(second.inputs)

    out = history.compare(clean_db, org, "brand_kit", second.id, first.id)
    assert out["before"]["version"] == 1 and out["after"]["version"] == 2
    assert out["counts"]["changed"] == 2 and {r["label"] for r in out["changed"]} == {"Main colour", "Brand voice"}

    # Analyses are read-only.
    technical_row = store.record(clean_db, tenant_id=org.id, kind="technical_seo", source="agent_run",
                                 metrics={"open": 1}, items={})
    clean_db.commit()
    with pytest.raises(ConflictError):
        history.restore(clean_db, org, "technical_seo", technical_row.id, user=user)

    # Restore through a registered applier records version 3 and audits it.
    applied: list[dict[str, Any]] = []
    monkeypatch.setitem(history.RESTORERS, "brand_kit",
                        lambda db, o, subject, document, *, user: applied.append(document) or None)
    result = history.restore(clean_db, org, "brand_kit", first.id, user=user)
    clean_db.commit()
    assert applied[0]["voice"] == KIT["voice"] and result["entry"]["version"] == 3
    actions = _audit_actions(clean_db, org.id)
    assert "restored version 1 of the brand kit" in actions and "recorded version 3 of the brand kit" in actions

    # Another workspace sees none of it.
    bind_tenant(clean_db, second_org.id)
    for call in (
        lambda: history.detail(clean_db, second_org, "brand_kit", first.id),
        lambda: history.compare(clean_db, second_org, "brand_kit", first.id, second.id),
        lambda: history.restore(clean_db, second_org, "brand_kit", first.id, user=user),
    ):
        with pytest.raises(NotFoundError):
            call()
    assert history.history(clean_db, second_org, "brand_kit", 10, subject=KIT_ID)["entries"] == []


@requires_db
def test_db_policy_restore_without_confirm_is_refused(clean_db, org) -> None:  # noqa: ANN001
    from app.db.session import bind_tenant
    from app.models.workspace import OnboardingState, User

    bind_tenant(clean_db, org.id)
    user = clean_db.query(User).filter(User.tenant_id == org.id).first()
    state = clean_db.query(OnboardingState).filter(OnboardingState.tenant_id == org.id).first()
    if state is None:
        clean_db.add(OnboardingState(tenant_id=org.id, guardrail="hybrid"))
    else:
        state.guardrail = "hybrid"
    full = history.record_version(clean_db, org, "workspace_policy", "", {"guardrail": "full", "autonomous": True},
                                  actor=user)
    history.record_version(clean_db, org, "workspace_policy", "", {"guardrail": "hybrid", "autonomous": True},
                           actor=user)
    clean_db.commit()
    with pytest.raises(InvalidInputError) as caught:
        history.restore(clean_db, org, "workspace_policy", full.id, user=user)
    assert caught.value.status_code == 422


@requires_db
def test_db_retention_prunes_but_keeps_the_minimum_and_audits(clean_db, org, monkeypatch) -> None:  # noqa: ANN001
    from sqlalchemy import select

    from app.core.config import settings
    from app.db.session import bind_tenant
    from app.models.history import AuditSnapshot

    monkeypatch.setattr(settings, "history_min_versions_kept", 2)
    bind_tenant(clean_db, org.id)
    org.settings = {**(org.settings or {}), "history_retention": {"product": {"keep_versions": 3}}}
    clean_db.flush()
    for i in range(5):
        history.record_version(clean_db, org, "product", "p-1", {"name": f"Plan {i}"}, actor=None)
    clean_db.commit()

    def versions() -> list[int]:
        return sorted(clean_db.scalars(select(AuditSnapshot.version).where(
            AuditSnapshot.tenant_id == org.id, AuditSnapshot.kind == "product")))

    assert versions() == [3, 4, 5]
    # Age-based: everything is "old" ten days from now, but the minimum stays.
    org.settings = {**org.settings, "history_retention": {"product": {"keep_versions": 3, "keep_days": 1}}}
    clean_db.flush()
    monkeypatch.setattr(store, "utcnow", lambda: datetime.now(UTC) + timedelta(days=10))
    assert store.apply_retention(clean_db, org.id, "product", "p-1") == 1
    clean_db.commit()
    assert versions() == [4, 5]
    prunes = [a for a in _audit_actions(clean_db, org.id) if "(history retention)" in a]
    assert len(prunes) == 3
