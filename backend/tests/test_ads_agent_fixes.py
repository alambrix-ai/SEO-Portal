"""Ads and traffic-quality agents: approval appliers, honest metrics, safe pushes.

Unit tests with fakes only (no database): each fake records what the agent
asked of it, so the behaviour under test is what reaches the platforms and
what is written back.
"""
from __future__ import annotations

import uuid
from datetime import timedelta
from types import SimpleNamespace

import pytest

from app.agents.click_fraud_controller import agent as fraud_agent
from app.agents.click_fraud_controller import detectors
from app.agents.dynamic_creative_optimizer import agent as dco_agent
from app.agents.dynamic_creative_optimizer import prompts as dco_prompts
from app.agents.dynamic_creative_optimizer import specs as dco_specs
from app.agents.first_party_audience_modeler import agent as audience_agent
from app.agents.first_party_audience_modeler import vectors
from app.agents.predictive_budget_engine import agent as budget_agent
from app.agents.predictive_budget_engine import allocator
from app.agents.referral_spam_guard import agent as spam_agent
from app.agents.referral_spam_guard import rules
from app.connectors.base.connector import Capability
from app.connectors.base.interfaces import ChannelPerformance, TrafficSample
from app.core.exceptions import ConnectorError
from app.db.base import utcnow
from app.models.ads import AdCreative, AudienceCluster, BudgetAllocation, FraudEvent
from app.models.agent import AgentRecord
from app.models.seo import ReferralSpamEvent, SeoPage
from app.services.approvals import APPLIERS


# ── Fakes ────────────────────────────────────────────────────────────────────
class _Result:
    def __init__(self, rows: list) -> None:
        self.rows = rows

    def scalars(self):  # noqa: ANN201
        return iter(self.rows)

    def scalar_one(self):  # noqa: ANN201
        return self.rows[0]

    def scalar_one_or_none(self):  # noqa: ANN201
        return self.rows[0] if self.rows else None

    def __iter__(self):  # noqa: ANN204
        return iter(self.rows)


class _FakeDb:
    """Rows by model. Where clauses are not evaluated; tests seed what fits."""

    def __init__(self, *rows: object, count: int = 0) -> None:
        self.rows: dict[type, list] = {}
        self.count = count
        for row in rows:
            self.add(row)

    def of(self, model: type) -> list:
        return self.rows.get(model, [])

    def add(self, row: object) -> None:
        if getattr(row, "id", None) is None:
            row.id = str(uuid.uuid4())
        self.rows.setdefault(type(row), []).append(row)

    def flush(self) -> None:
        pass

    def get(self, model: type, row_id: str):  # noqa: ANN201
        return next((r for r in self.of(model) if r.id == row_id), None)

    def execute(self, stmt):  # noqa: ANN001, ANN201
        sql = str(stmt).lower()
        if "count(" in sql:
            return _Result([self.count])
        if "max(" in sql:
            latest: dict[str, object] = {}
            for row in self.of(AdCreative):
                if row.created_at and (
                    row.source_page_url not in latest or row.created_at > latest[row.source_page_url]
                ):
                    latest[row.source_page_url] = row.created_at
            return _Result(list(latest.items()))
        entity = stmt.column_descriptions[0].get("entity")
        return _Result(list(self.of(entity)))


class _Cipher:
    def encrypt(self, value: str, *, context: object) -> str:
        return f"enc:{value}"

    def decrypt(self, value: str, *, context: object) -> str:
        return value[4:] if value.startswith("enc:") else value


class _Ads:
    """An ad connector that records every call."""

    def __init__(self, slug: str, channel: str, caps: set, *, name: str = "", **behaviour) -> None:  # noqa: ANN003
        self.slug = slug
        self.channel = channel
        self.name = name or slug
        self.caps = caps
        self.behaviour = behaviour
        self.calls: list[tuple[str, dict]] = []

    def supports(self, capability: Capability) -> bool:
        return capability in self.caps

    def _do(self, name: str, default, **kwargs):  # noqa: ANN001, ANN003, ANN202
        self.calls.append((name, kwargs))
        outcome = self.behaviour.get(name, default)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome(**kwargs) if callable(outcome) else outcome

    def sample_traffic(self, *, limit: int = 50):  # noqa: ANN201
        return self._do("sample_traffic", [], limit=limit)

    def read_performance(self, *, days: int = 7):  # noqa: ANN201
        return self._do("read_performance", ChannelPerformance(channel=self.channel), days=days)

    def block_placement(self, *, source: str, reason: str) -> bool:
        return self._do("block_placement", True, source=source, reason=reason)

    def set_budget_share(self, *, percent: int, daily_budget: float | None = None) -> bool:
        return self._do("set_budget_share", True, percent=percent)

    def push_audience(self, *, label: str, size: int, signals: list[str]) -> str:
        return self._do("push_audience", f"{self.slug}-aud-{label}", label=label, size=size, signals=signals)

    def upload_creative(self, **kwargs) -> str:  # noqa: ANN003
        return self._do("upload_creative", f"{self.slug}-ad-1", **kwargs)

    def exclude_referrer(self, domain: str) -> bool:
        return self._do("exclude_referrer", True, domain=domain)


def _ctx(db: _FakeDb, connectors: list | None = None, **extra):  # noqa: ANN003, ANN202
    connectors = list(connectors or [])
    record = SimpleNamespace(settings=dict(extra.pop("settings", {})), slug="agent")
    answers = list(extra.pop("answers", []))
    ctx = SimpleNamespace(
        db=db,
        scope=extra.pop("scope", ""),
        tenant_id="t",
        org=SimpleNamespace(name="Acme", primary_domain=extra.pop("primary_domain", "acme.test")),
        record=record,
        cipher=_Cipher(),
        remaining_actions=extra.pop("remaining_actions", 20),
        warnings=[],
        connectors={c.slug: c for c in connectors},
    )
    ctx.log_warning = lambda message, *args: ctx.warnings.append(message % args if args else message)
    ctx.progress = lambda *a, **k: None
    ctx.with_capability = lambda cap: next((c for c in connectors if c.supports(cap)), None)
    ctx.all_with_capability = lambda cap: [c for c in connectors if c.supports(cap)]
    ctx.optional_connector = lambda slug: ctx.connectors.get(slug)
    ctx.has_connector = lambda slug: slug in ctx.connectors
    ctx.uses_connector = lambda slug, cap: any(c.slug == slug for c in ctx.all_with_capability(cap))
    ctx.connector_name = lambda slug: getattr(ctx.connectors.get(slug), "name", slug)
    ctx.setting = lambda key, default=None: record.settings.get(key, default)
    ctx.set_setting = lambda key, value: record.settings.__setitem__(key, value)

    def ask_json(*a, **k):  # noqa: ANN002, ANN003, ANN202
        return answers.pop(0) if answers else {}

    ctx.ask_json = ask_json
    for key, value in extra.items():
        setattr(ctx, key, value)
    return ctx


def _approve_with(monkeypatch, ctx, target_kind: str, payload: dict, db: _FakeDb):  # noqa: ANN001, ANN202
    import app.orchestration.runner as runner

    monkeypatch.setattr(runner, "build_context", lambda *a, **k: ctx)
    org = SimpleNamespace(id="t")
    item = SimpleNamespace(target_id=None, target_kind=target_kind)
    APPLIERS[target_kind](db, org, item, payload)


# ── Referral Spam Guard ─────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "domain",
    [
        "essex.ac.uk", "sussex.com", "middlesex.gov.uk", "cryptography.io",
        "skyscraper-news.com", "adultlearning.org", "spiderman-fans.net",
    ],
)
def test_spam_tokens_only_match_whole_words(domain: str):
    verdict, _ = rules.classify(
        TrafficSample(source=domain, referrer=domain, sessions=30, bounce_rate=0.4, avg_session_seconds=90)
    )
    assert verdict is rules.Verdict.CLEAN, domain


@pytest.mark.parametrize(
    ("domain", "expected"),
    [
        ("sex4u-cams.xyz", rules.Verdict.ADULT),
        ("freeporn.site", rules.Verdict.ADULT),
        ("best-webcams.net", rules.Verdict.ADULT),
        ("bitcoinprofit-now.com", rules.Verdict.CRYPTO),
        ("cheap-seo-links.com", rules.Verdict.LINK_DROP),
        ("mybacklinks.ru", rules.Verdict.LINK_DROP),
        ("trading-signals-pro.com", rules.Verdict.CRYPTO),
        ("scrapers-r-us.io", rules.Verdict.SCRAPER),
    ],
)
def test_spam_tokens_still_catch_spam(domain: str, expected: rules.Verdict):
    verdict, _ = rules.classify(TrafficSample(source=domain, referrer=domain))
    assert verdict is expected, domain


def _spam_run(model_answers: list, *, remaining: int = 20):  # noqa: ANN202
    samples = [
        TrafficSample(source="a", referrer="https://www.freeporn.site/x", sessions=4),
        TrafficSample(source="a", referrer="freeporn.site/other", sessions=9),  # same domain
        TrafficSample(source="b", referrer="semalt.com", sessions=3),
        TrafficSample(source="c", referrer="essex.ac.uk", sessions=50, bounce_rate=0.3, avg_session_seconds=80),
        TrafficSample(source="d", referrer="odd-one.xyz", sessions=12, bounce_rate=0.9, avg_session_seconds=8),
    ]
    analytics = _Ads("ga4", "", {Capability.READ_REFERRERS}, read_referrers=None)
    analytics.read_referrers = lambda days, limit: samples
    db = _FakeDb(count=7)
    ctx = _ctx(db, [analytics], answers=model_answers, remaining_actions=remaining)
    return spam_agent.AGENT.run(ctx), ctx


def test_spam_detail_counts_are_right_when_the_model_declines():
    result, _ = _spam_run([{"is_spam": False}])
    assert result.detail["rule_matches"] == 2  # freeporn (once) + semalt
    assert result.detail["model_checks"] == 1
    assert result.detail["model_flagged"] == 0
    assert len(result.actions) == 2


def test_spam_metric_label_separates_blocked_from_proposed():
    result, _ = _spam_run([{"is_spam": True, "confidence": 0.9, "category": "Spam"}])
    assert result.metric_label == "7 spam referrers blocked today, 3 more proposed"
    assert result.metrics == {}
    assert all(a.metrics == {"referral_spam_blocked": 1} for a in result.actions)


def test_spam_proposals_have_stable_distinct_target_ids():
    first, _ = _spam_run([{"is_spam": False}])
    second, _ = _spam_run([{"is_spam": False}])
    ids = [a.target_id for a in first.actions]
    assert len(set(ids)) == len(ids) and all(ids)
    assert ids == [a.target_id for a in second.actions]
    uuid.UUID(ids[0])  # target_id is a UUID column
    assert ids[0] == spam_agent.target_id_for("freeporn.site")


def test_spam_model_checks_follow_the_remaining_allowance():
    assert spam_agent.llm_check_budget(500, 0) == spam_agent.MAX_LLM_CHECKS
    assert spam_agent.llm_check_budget(0, 0) == spam_agent.MIN_LLM_CHECKS
    assert spam_agent.llm_check_budget(9, 2) == 7


def test_approving_a_referral_block_does_what_autonomy_does(monkeypatch):
    analytics = _Ads("ga4", "", {Capability.READ_REFERRERS})
    db = _FakeDb(AgentRecord(slug="referral_spam_guard", name="x"))
    ctx = _ctx(db, [analytics])
    _approve_with(
        monkeypatch,
        ctx,
        "referral_spam",
        {"referrer": "freeporn.site", "category": "Adult / Porn spam", "confidence": 0.98, "sessions": 4, "_metrics": {}},
        db,
    )
    event = db.of(ReferralSpamEvent)[0]
    assert (event.referrer, event.sessions_affected, event.action) == ("freeporn.site", 4, "Blocked")
    assert analytics.calls == [("exclude_referrer", {"domain": "freeporn.site"})]


# ── Click-Fraud Controller ──────────────────────────────────────────────────
def test_missing_viewport_adds_no_suspicion():
    base = {"sessions": 5, "bounce_rate": 0.92, "avg_session_seconds": 2.0}
    absent = detectors.assess(TrafficSample(source="p", **base, signals={}))
    reported = detectors.assess(TrafficSample(source="p", **base, signals={"viewport_area": 0}))
    assert absent[0] is detectors.Verdict.CLEAN
    assert reported[0] is detectors.Verdict.UNCERTAIN


def _fraud_setup(google_perf=None):  # noqa: ANN001, ANN202
    caps = {
        Capability.READ_TRAFFIC_QUALITY,
        Capability.BLOCK_PLACEMENT,
        Capability.READ_AD_PERFORMANCE,
    }
    google = _Ads(
        "google_ads",
        "google",
        caps,
        sample_traffic=[
            TrafficSample(source="bad-site.com", sessions=10, signals={"is_datacenter": True}),
            TrafficSample(source="meh.com", sessions=6, bounce_rate=0.95, avg_session_seconds=2, signals={"fingerprint_repeat_count": 6}),
        ],
        read_performance=google_perf or ChannelPerformance(channel="google", spend=200.0, clicks=400),
    )
    meta = _Ads("meta_ads", "meta", caps, sample_traffic=[], read_performance=ChannelPerformance(channel="meta"))
    return google, meta


def test_fraud_blocks_only_on_the_platform_the_traffic_came_from():
    google, meta = _fraud_setup()
    db = _FakeDb(count=2)
    ctx = _ctx(db, [google, meta], answers=[{"verdict": "clean"}])
    result = fraud_agent.AGENT.run(ctx)

    assert result.detail["heuristic_blocks"] == 1
    assert result.detail["model_checks"] == 1 and result.detail["model_flagged"] == 0
    assert result.metric_label == "2 blocked today, 1 more proposed"
    action = result.actions[0]
    assert action.payload["channel"] == "google"  # filled from the connector
    # Real CPC from spend / clicks: 200 / 400 = 0.5 per click, 10 clicks.
    assert action.payload["spend_saved"] == 5.0
    assert action.payload["spend_saved_basis"] == fraud_agent.CPC_MEASURED
    assert "est." not in result.summary
    assert action.metrics == {"fraud_blocked": 1, "spend_saved": 5.0}

    action.apply(ctx)
    assert [c for c in google.calls if c[0] == "block_placement"]
    assert not [c for c in meta.calls if c[0] == "block_placement"]
    assert db.of(FraudEvent)[0].channel == "google"


def test_fraud_savings_without_real_cpc_are_labelled_estimates():
    google, meta = _fraud_setup(google_perf=ChannelPerformance(channel="google"))
    db = _FakeDb(BudgetAllocation(channel="google", spend=1000.0, conversions=10))
    ctx = _ctx(db, [google, meta], answers=[{"verdict": "clean"}])
    result = fraud_agent.AGENT.run(ctx)
    action = result.actions[0]
    assert action.payload["spend_saved_basis"] == fraud_agent.CPC_ESTIMATED
    assert action.payload["spend_saved"] == 40.0  # CAC 100 x 0.04 x 10 clicks
    assert "est. saved" in result.summary
    assert result.detail["spend_saved_is_estimate"] is True


def test_fraud_proposals_have_distinct_target_ids_and_an_applier(monkeypatch):
    google, meta = _fraud_setup()
    ctx = _ctx(_FakeDb(), [google, meta], answers=[{"verdict": "fraudulent", "confidence": 0.95, "reason": "x"}])
    result = fraud_agent.AGENT.run(ctx)
    ids = {a.target_id for a in result.actions}
    assert len(ids) == 2 and None not in ids

    db = _FakeDb(AgentRecord(slug="click_fraud_controller", name="x"))
    ctx = _ctx(db, [google, meta])
    _approve_with(monkeypatch, ctx, "fraud_event", dict(result.actions[0].payload), db)
    assert db.of(FraudEvent)[0].source == "bad-site.com"
    assert ("block_placement", {"source": "bad-site.com", "reason": detectors.Reason.DATACENTER}) in google.calls
    assert not [c for c in meta.calls if c[0] == "block_placement"]


# ── Predictive Budget Engine ────────────────────────────────────────────────
def test_projected_cac_is_the_blend_at_those_shares():
    states = [
        allocator.ChannelState("a", 50, 10.0, spend=500, conversions=50),
        allocator.ChannelState("b", 50, 20.0, spend=500, conversions=25),
    ]
    # Same spend on both: 75 conversions for 1000 -> 13.33, not the mean 15.
    assert allocator.projected_cac(states, {"a": 50, "b": 50}) == 13.33
    assert allocator.projected_cac(states, allocator.current_shares(states)) == allocator.blended_cac(states)


def _budget_rows(scale: float = 1.0) -> list[BudgetAllocation]:
    return [
        BudgetAllocation(channel="google", percent=50, cac=10.0 * scale, spend=1000.0 * scale, conversions=100),
        BudgetAllocation(channel="meta", percent=50, cac=11.0 * scale, spend=1100.0 * scale, conversions=100),
        # Lots of spend, almost no conversions: inflates the measured blend,
        # but carries no credible CAC so it does not take part.
        BudgetAllocation(channel="dsp", percent=0, cac=5000.0 * scale, spend=5000.0 * scale, conversions=1),
    ]


def test_budget_holds_when_like_for_like_gain_is_immaterial():
    ctx = _ctx(_FakeDb(*_budget_rows()))
    result = budget_agent.AGENT.run(ctx)
    # The old test compared ~35 (measured, dsp included) with ~10.46 and moved money.
    assert not result.actions
    assert result.summary.startswith("held allocation")
    assert result.detail["improvement_ratio"] < budget_agent.MIN_CAC_IMPROVEMENT_RATIO


@pytest.mark.parametrize("scale", [1.0, 83.0])
def test_budget_threshold_is_currency_blind(scale: float):
    rows = [
        BudgetAllocation(channel="google", percent=50, cac=10.0 * scale, spend=1000.0 * scale, conversions=100),
        BudgetAllocation(channel="meta", percent=50, cac=30.0 * scale, spend=3000.0 * scale, conversions=100),
    ]
    result = budget_agent.AGENT.run(_ctx(_FakeDb(*rows)))
    assert len(result.actions) == 1
    assert result.actions[0].payload["percents"] == {"google": 58, "meta": 42}


def test_budget_channels_that_just_moved_are_held_unless_data_changed():
    rows = [
        BudgetAllocation(channel="google", percent=50, cac=10.0, spend=1000.0, conversions=100),
        BudgetAllocation(channel="meta", percent=50, cac=30.0, spend=3000.0, conversions=100),
    ]
    recent = (utcnow() - timedelta(hours=2)).isoformat()
    old = (utcnow() - timedelta(hours=30)).isoformat()

    held = _ctx(_FakeDb(*rows), settings={"shift_history": {"google": {"at": recent, "cac": 10.2}}})
    assert budget_agent.AGENT._cooling_down(held, rows) == {"google"}
    result = budget_agent.AGENT.run(held)
    assert not result.actions and result.detail["cooling_down"] == ["google"]

    changed = _ctx(_FakeDb(*rows), settings={"shift_history": {"google": {"at": recent, "cac": 14.0}}})
    assert budget_agent.AGENT._cooling_down(changed, rows) == set()
    expired = _ctx(_FakeDb(*rows), settings={"shift_history": {"google": {"at": old, "cac": 10.0}}})
    assert budget_agent.AGENT._cooling_down(expired, rows) == set()


def test_budget_push_failures_are_raised_after_recording_the_rest():
    rows = [
        BudgetAllocation(channel="google", percent=50, cac=10.0),
        BudgetAllocation(channel="meta", percent=50, cac=30.0),
    ]
    caps = {Capability.WRITE_AD_BUDGET}
    google = _Ads("google_ads", "google", caps, set_budget_share=ConnectorError("quota exceeded"))
    meta = _Ads("meta_ads", "meta", caps)
    ctx = _ctx(_FakeDb(*rows), [google, meta])

    with pytest.raises(ConnectorError, match="Google Ads"):
        budget_agent._make_applier({"google": 58, "meta": 42}, {"google": 8, "meta": -8})(ctx)

    assert rows[0].percent == 50  # the platform still spends the old share
    assert rows[1].percent == 42 and rows[1].last_shift == -8
    history = ctx.record.settings["shift_history"]
    assert "meta" in history and "google" not in history


# ── First-Party Audience Modeler ────────────────────────────────────────────
def _cluster(label: str, size: int, cohesion: float = 0.6) -> vectors.Cluster:
    return vectors.Cluster(label=label, size=size, cohesion=cohesion, top_signals=[label])


def test_clusters_with_the_same_label_are_one_segment():
    merged = audience_agent.merge_by_label(
        [_cluster("Finance shoppers", 300, 0.6), _cluster("Finance shoppers", 100, 0.4), _cluster("Service", 200)]
    )
    assert [(c.label, c.size) for c in merged] == [("Finance shoppers", 400), ("Service", 200)]
    assert merged[0].cohesion == 0.55


def test_persist_upserts_by_label():
    existing = AudienceCluster(label="Service", size=10, is_live=False, activated_channels=[])
    db = _FakeDb(existing)
    ctx = _ctx(db)
    stored = audience_agent.AGENT._persist(
        ctx, audience_agent.merge_by_label([_cluster("Service", 150), _cluster("New", 120), _cluster("New", 30)])
    )
    assert len(db.of(AudienceCluster)) == 2
    assert stored[0] is existing and existing.size == 150


def _audience_ctx(cluster: AudienceCluster, *connectors, **extra):  # noqa: ANN002, ANN003, ANN202
    return _ctx(_FakeDb(cluster), list(connectors), **extra)


def test_activation_records_remote_ids_and_never_repushes():
    cluster = AudienceCluster(
        label="Finance shoppers", size=400, cohesion=0.6, top_signals=["finance_calculator"],
        activated_channels=[], is_live=False, refreshed_at=utcnow() - timedelta(days=3),
    )
    meta = _Ads("meta_ads", "meta", {Capability.PUSH_AUDIENCE}, name="Meta Ads")
    ctx = _audience_ctx(cluster, meta)

    actions = audience_agent.AGENT._activate(ctx, [cluster])
    assert len(actions) == 1
    actions[0].apply(ctx)
    assert ctx.record.settings["remote_audience_ids"][cluster.id] == {"meta_ads": "meta_ads-aud-Finance shoppers"}
    assert cluster.activated_channels == ["meta_ads"] and cluster.is_live

    # Stale or not, an activated segment is not pushed again: that would
    # create a second audience on the platform.
    assert audience_agent.AGENT._activate(ctx, [cluster]) == []
    cluster.activated_channels = []  # e.g. retired and re-emerged
    assert audience_agent.AGENT._activate(ctx, [cluster]) == []
    assert len([c for c in meta.calls if c[0] == "push_audience"]) == 1


def test_activation_failures_are_surfaced_after_recording_successes():
    cluster = AudienceCluster(label="Service", size=400, cohesion=0.6, top_signals=[], activated_channels=[], is_live=False)
    meta = _Ads("meta_ads", "meta", {Capability.PUSH_AUDIENCE}, name="Meta Ads")
    tiktok = _Ads(
        "tiktok_ads", "tiktok", {Capability.PUSH_AUDIENCE}, name="TikTok Ads",
        push_audience=ConnectorError("audience too small"),
    )
    ctx = _audience_ctx(cluster, meta, tiktok)
    with pytest.raises(ConnectorError, match="TikTok Ads"):
        audience_agent._make_activator(cluster.id, ["meta_ads", "tiktok_ads"])(ctx)
    assert cluster.activated_channels == ["meta_ads"] and cluster.is_live
    assert "meta_ads" in ctx.record.settings["remote_audience_ids"][cluster.id]


def test_retired_segment_that_reemerges_is_live_on_its_existing_audiences():
    cluster = AudienceCluster(label="Service", size=10, is_live=False, activated_channels=[])
    cluster.id = "c1"
    ctx = _ctx(_FakeDb(cluster), settings={"remote_audience_ids": {"c1": {"meta_ads": "m1"}}})
    audience_agent.AGENT._persist(ctx, [_cluster("Service", 150)])
    assert cluster.is_live and cluster.activated_channels == ["meta_ads"]


# ── Dynamic Creative Optimizer ──────────────────────────────────────────────
def test_pages_per_run_scales_with_the_allowance_within_bounds():
    assert dco_agent.pages_per_run(0, 2) == dco_agent.PAGES_PER_RUN
    assert dco_agent.pages_per_run(12, 2) == 3  # 4 variants a page
    assert dco_agent.pages_per_run(500, 2) == dco_agent.MAX_PAGES_PER_RUN


def _page(url: str, updated_hours_ago: float, title: str = "New model price") -> SeoPage:
    page = SeoPage(url=url, title=title, body_encrypted="enc:<p>$25,000</p>")
    page.id = url
    page.updated_at = utcnow() - timedelta(hours=updated_hours_ago)
    return page


def _creative(page_url: str, hours_ago: float, **kw) -> AdCreative:  # noqa: ANN003
    creative = AdCreative(
        platform=kw.get("platform", "Google Ads"),
        dimensions="300x250",
        headline_encrypted=f"enc:{kw.get('headline', 'Head')}",
        body_copy_encrypted=f"enc:{kw.get('body', 'Body')}",
        call_to_action="See offers",
        source_page_url=page_url,
        status=kw.get("status", "generated"),
        detail={"batch": kw.get("batch", "b1")},
        tenant_id="t",
    )
    creative.created_at = utcnow() - timedelta(hours=hours_ago)
    return creative


def test_pages_with_current_creative_are_skipped_and_changed_ones_redone():
    covered = _page("/covered", updated_hours_ago=10)
    changed = _page("/changed", updated_hours_ago=1)  # price changed after its ads
    fresh = _page("/fresh", updated_hours_ago=5)
    db = _FakeDb(covered, changed, fresh, _creative("/covered", 2), _creative("/changed", 3, status="live"))
    ctx = _ctx(db)
    picked = dco_agent.AGENT._pick_pages(ctx, [covered, changed, fresh], limit=5)
    assert {p.url for p in picked} == {"/changed", "/fresh"}
    # Never-covered pages come before re-dos at the same commercial score.
    assert [p.url for p in picked] == ["/fresh", "/changed"]


def test_no_audiences_means_no_invented_segment():
    ctx = _ctx(_FakeDb())
    assert dco_agent.AGENT._segments(ctx) == []
    text = dco_prompts.creative_variants(
        product=dco_specs.Product(name="X", url="/x"),
        platforms=["Google Ads"], brand="Acme", segments=[], page_url="/x",
    )
    assert "in-market shoppers" not in text
    assert "SEGMENTS: none defined" in text
    assert "at least 3 variants" in text  # Google combines them into one ad


def test_run_skips_when_every_page_is_current_and_clears_unknown_segments():
    google = _Ads("google_ads", "google", {Capability.UPLOAD_CREATIVE}, name="Google Ads")
    page = _page("/models/a", updated_hours_ago=10)
    db = _FakeDb(page, _creative("/models/a", 1))
    assert dco_agent.AGENT.run(_ctx(db, [google])).skipped

    db = _FakeDb(page)
    answer = {"variants": [
        {"platform": "Google Ads", "headline": f"Headline {i}", "body_copy": f"Body {i}.", "audience_segment": "in-market shoppers"}
        for i in range(3)
    ]}
    result = dco_agent.AGENT.run(_ctx(db, [google], answers=[answer]))
    creatives = db.of(AdCreative)
    assert len(creatives) == 3 and len(result.actions) == 3
    assert all(c.audience_segment == "" for c in creatives)
    assert len({c.detail["batch"] for c in creatives}) == 1


def test_google_publish_combines_siblings_and_marks_them_live():
    google = _Ads("google_ads", "google", {Capability.UPLOAD_CREATIVE}, name="Google Ads")
    main = _creative("/models/a", 1, headline="H1", body="D1")
    same_batch = [_creative("/models/a", 1, headline="H2", body="D2"), _creative("/models/a", 1, headline="H3", body="D3")]
    other_batch = _creative("/models/a", 1, headline="Old", body="Old", batch="b0")
    rejected = _creative("/models/a", 1, headline="No", body="No", status="rejected")
    db = _FakeDb(main, *same_batch, other_batch, rejected)
    ctx = _ctx(db, [google], primary_domain="acme.test")

    dco_agent._make_publisher(main.id)(ctx)
    name, kwargs = google.calls[0]
    assert kwargs["final_url"] == "https://acme.test/models/a"
    assert kwargs["headline"] == "H1"
    assert sorted(kwargs["extra_headlines"]) == ["H2", "H3"]
    assert sorted(kwargs["extra_descriptions"]) == ["D2", "D3"]
    assert all(s.status == "live" and s.detail["remote_id"] == "google_ads-ad-1" for s in same_batch)
    assert other_batch.status == "generated" and rejected.status == "rejected"

    # Approving a sibling later uploads nothing more.
    dco_agent._make_publisher(same_batch[0].id)(ctx)
    assert len(google.calls) == 1


def test_non_combining_platform_gets_its_landing_page_but_no_siblings():
    meta = _Ads("meta_ads", "meta", {Capability.UPLOAD_CREATIVE}, name="Meta Ads")
    main = _creative("https://acme.test/p", 1, platform="Meta Ads")
    sibling = _creative("https://acme.test/p", 1, platform="Meta Ads", headline="Other")
    ctx = _ctx(_FakeDb(main, sibling), [meta])
    dco_agent._make_publisher(main.id)(ctx)
    kwargs = meta.calls[0][1]
    assert kwargs["final_url"] == "https://acme.test/p"
    assert kwargs["extra_headlines"] == () and sibling.status == "generated"


def test_landing_url_needs_a_domain_for_relative_paths():
    assert dco_agent.landing_url(_ctx(_FakeDb(), primary_domain=""), "/x") == ""
    assert dco_agent.landing_url(_ctx(_FakeDb(), primary_domain="https://a.test/"), "x") == "https://a.test/x"
