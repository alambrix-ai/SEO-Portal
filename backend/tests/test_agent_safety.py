"""Spending-agent safety: practice mode, limits, unverified connectors, stops.

Unit tests with fakes only (no database). The fakes record every call, so
what is under test is what would reach an ad platform and what is recorded.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app.agents.base import safety
from app.agents.base.contracts import AgentAction, Impact
from app.agents.base.policy import Decision, Guardrail, decide
from app.agents.base.safety import Caps, Ledger, Measure, SafetyGate
from app.agents.click_fraud_controller import agent as fraud_agent
from app.agents.predictive_budget_engine import agent as budget_agent
from app.connectors.base.connector import Capability
from app.connectors.base.interfaces import ChannelPerformance, TrafficSample
from app.core.exceptions import ConflictError, ForbiddenError, InvalidInputError
from app.models.ads import BudgetAllocation
from app.models.agent import AgentRecord, AgentStatus, RunStatus
from app.orchestration import runner
from app.services import agent_safety
from tests.test_ads_agent_fixes import _Ads, _ctx, _FakeDb

UNVERIFIED_GOOGLE = (
    "Google Ads hasn't been confirmed against a live account yet, so changes wait "
    "for your approval."
)


# ── Fakes ────────────────────────────────────────────────────────────────────
class _Sinks:
    """Stands in for the runner's database side effects, recording each."""

    def __init__(self, monkeypatch) -> None:  # noqa: ANN001
        self.practised: list[dict] = []
        self.queued: list[dict] = []
        self.audits: list[dict] = []
        self.metrics: list[dict] = []
        self.repeat = False
        monkeypatch.setattr(runner.agent_safety, "record_practice", self._practise)
        monkeypatch.setattr(runner, "queue_action", self._queue)
        monkeypatch.setattr(runner.audit, "record_agent_action", self._audit)
        monkeypatch.setattr(runner.metrics, "accumulate", self._metrics)

    def _practise(self, db, **kwargs):  # noqa: ANN001, ANN003, ANN202
        self.practised.append(kwargs)
        return SimpleNamespace(id=f"p{len(self.practised)}"), not self.repeat

    def _queue(self, db, **kwargs):  # noqa: ANN001, ANN003, ANN202
        self.queued.append(kwargs)
        return SimpleNamespace(id="q"), True

    def _audit(self, db, **kwargs):  # noqa: ANN001, ANN003, ANN202
        self.audits.append(kwargs)

    def _metrics(self, db, **kwargs):  # noqa: ANN001, ANN003, ANN202
        self.metrics.append(kwargs)


def _record(slug: str, *, autonomy: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        slug=slug,
        name=slug.replace("_", " ").title(),
        category="Ads",
        autonomy=autonomy,
        actions_today=0,
        last_error="",
        settings={},
    )


def _run_ctx(remaining: int = 20) -> SimpleNamespace:
    return SimpleNamespace(
        progress=lambda *a, **k: None,
        remaining_actions=remaining,
        cipher=object(),
        connector_name=lambda slug: {"google_ads": "Google Ads", "meta_ads": "Meta Ads"}.get(slug, slug),
    )


def _gate(**kwargs) -> SafetyGate:  # noqa: ANN003
    kwargs.setdefault("spending", True)
    kwargs.setdefault(
        "connector_name",
        lambda slug: {"google_ads": "Google Ads", "meta_ads": "Meta Ads"}.get(slug, slug),
    )
    return SafetyGate(**kwargs)


def _process(actions, *, record=None, gate=None, guardrail=Guardrail.FULL, remaining=20):  # noqa: ANN001, ANN202
    return runner._process_actions(
        db=None,
        ctx=_run_ctx(remaining),
        org=SimpleNamespace(id="t"),
        record=record or _record("predictive_budget_engine"),
        run_id="run-1",
        actions=list(actions),
        guardrail=guardrail,
        gate=gate or _gate(),
    )


def _forbidden_apply(ctx) -> None:  # noqa: ANN001
    raise AssertionError("apply() must not be called")


def _action(kind: str, payload: dict | None = None, *, writes_via=("google_ads",), apply=None, impact=Impact.LOW):  # noqa: ANN001, ANN202
    return AgentAction(
        kind=kind,
        title=f"{kind} title",
        impact=impact,
        target_kind=kind,
        payload=dict(payload or {}),
        apply=apply or _forbidden_apply,
        writes_via=tuple(writes_via),
    )


def _budget_run():  # noqa: ANN202
    """A real Predictive Budget Engine pass over two platforms that can take budget."""
    caps = {Capability.READ_AD_PERFORMANCE, Capability.WRITE_AD_BUDGET}
    google = _Ads(
        "google_ads", "google", caps,
        read_performance=ChannelPerformance(channel="google", spend=700.0, conversions=70),
    )
    meta = _Ads(
        "meta_ads", "meta", caps,
        read_performance=ChannelPerformance(channel="meta", spend=2100.0, conversions=70),
    )
    rows = [
        BudgetAllocation(channel="google", percent=50, cac=10.0, spend=700.0, conversions=70),
        BudgetAllocation(channel="meta", percent=50, cac=30.0, spend=2100.0, conversions=70),
    ]
    ctx = _ctx(_FakeDb(*rows), [google, meta])
    result = budget_agent.AGENT.run(ctx)
    assert len(result.actions) == 1
    return ctx, result.actions[0], google, meta


# ── Practice mode ────────────────────────────────────────────────────────────
def test_budget_plan_carries_before_after_and_the_amount_moved():
    _, action, google, meta = _budget_run()
    payload = action.payload
    assert payload["before"] == {"google": 50, "meta": 50}
    assert payload["percents"] == {"google": 58, "meta": 42}
    assert payload["moved_points"] == 8
    # 2,800 spent over 7 days -> 400 a day; 8% of it changes channel.
    assert payload["budget_moved_per_day"] == 32.0
    assert set(action.writes_via) == {"google_ads", "meta_ads"}


def test_practice_mode_reads_but_never_writes_and_records_the_plan(monkeypatch):
    sinks = _Sinks(monkeypatch)
    ctx, action, google, meta = _budget_run()
    # The plan is real: the platforms were read.
    assert [c for c in google.calls if c[0] == "read_performance"]

    # Run the action through the runner the way a real pass would, but with
    # the agent's own apply wired to the fake platforms.
    real_apply = action.apply
    action.apply = lambda _ctx: real_apply(ctx)
    tally = _process([action], gate=_gate(practice="agent", is_verified=lambda s: True))

    writes = {"set_budget_share", "block_placement", "upload_creative", "push_audience"}
    assert not [c for c in google.calls + meta.calls if c[0] in writes]
    assert tally.practised == 1 and tally.applied == 0 and tally.queued == 0
    assert not sinks.queued
    recorded = sinks.practised[0]
    assert recorded["action"].payload["percents"] == {"google": 58, "meta": 42}
    assert recorded["action"].payload["before"] == {"google": 50, "meta": 50}
    assert recorded["would"] == "apply"
    assert recorded["measure"] == {"budget_points": 8.0, "budget_moved": 32.0}
    assert recorded["source"] == "agent"
    # The run timeline and the weekly summary read this entry.
    entry = sinks.audits[0]
    assert "practised" in entry["action"] and "not sent" in entry["action"]
    assert entry["context"]["practice"] is True
    assert entry["context"]["agent"] == "predictive_budget_engine"
    assert entry["context"]["run_id"] == "run-1"
    assert entry["context"]["safety"]["budget_moved"] == 32.0
    assert not sinks.metrics  # nothing went live, so nothing is counted


def test_practice_mode_never_blocks_a_placement(monkeypatch):
    sinks = _Sinks(monkeypatch)
    caps = {Capability.READ_TRAFFIC_QUALITY, Capability.BLOCK_PLACEMENT, Capability.READ_AD_PERFORMANCE}
    google = _Ads(
        "google_ads", "google", caps,
        sample_traffic=[TrafficSample(source="bad.example", sessions=10, signals={"is_datacenter": True})],
        read_performance=ChannelPerformance(channel="google", spend=100.0, clicks=200),
    )
    ctx = _ctx(_FakeDb(), [google])
    result = fraud_agent.AGENT.run(ctx)
    assert result.actions and result.actions[0].writes_via == ("google_ads",)
    for action in result.actions:
        real = action.apply
        action.apply = lambda _c, real=real: real(ctx)

    _process(result.actions, record=_record("click_fraud_controller"), gate=_gate(practice="workspace"))
    assert [c for c in google.calls if c[0] == "sample_traffic"]
    assert not [c for c in google.calls if c[0] == "block_placement"]
    assert len(sinks.practised) == len(result.actions)


@pytest.mark.parametrize(
    "slug,kind",
    [
        ("predictive_budget_engine", "rebalance_budget"),
        ("click_fraud_controller", "block_placement"),
        ("dynamic_creative_optimizer", "publish_creative"),
        ("search_campaign_builder", "create_search_campaign"),
        ("first_party_audience_modeler", "activate_audience"),
    ],
)
def test_practice_mode_covers_every_spending_agent(monkeypatch, slug: str, kind: str):
    sinks = _Sinks(monkeypatch)
    # Even an action that would wait for approval is practised, not queued:
    # a queued item could be approved and sent.
    actions = [_action(kind, impact=Impact.HIGH), _action(kind, {"n": 2})]
    tally = _process(actions, record=_record(slug), gate=_gate(practice="platform", is_verified=lambda s: False), guardrail=Guardrail.HYBRID)
    assert tally.practised == 2 and not sinks.queued
    assert [p["would"] for p in sinks.practised] == ["queue", "queue"]  # unverified: all wait


def test_the_same_change_practised_again_is_counted_not_recorded_twice(monkeypatch):
    sinks = _Sinks(monkeypatch)
    sinks.repeat = True
    tally = _process([_action("block_placement")], record=_record("click_fraud_controller"), gate=_gate(practice="agent"))
    assert tally.practised == 1 and tally.practised_again == 1
    assert not sinks.audits


class _PracticeDb:
    def __init__(self, existing=None) -> None:  # noqa: ANN001
        self.existing = existing
        self.added: list = []

    def execute(self, stmt):  # noqa: ANN001, ANN201
        found = self.existing
        return SimpleNamespace(scalars=lambda: SimpleNamespace(first=lambda: found))

    def add(self, row) -> None:  # noqa: ANN001
        self.added.append(row)

    def flush(self) -> None:
        pass


class _JsonCipher:
    def encrypt_json(self, value, *, context):  # noqa: ANN001, ANN201
        return json.dumps(value)


def test_a_practised_change_is_stored_with_its_full_plan_and_cannot_be_approved():
    from app.models.approval import ApprovalStatus

    db = _PracticeDb()
    action = _action(
        "rebalance_budget",
        {"percents": {"google": 58}, "before": {"google": 50}, "shifts": {"google": 8}},
    )
    action.metrics = {"x": 1}
    item, created = agent_safety.record_practice(
        db,
        org=SimpleNamespace(id="t"),
        record=SimpleNamespace(slug="predictive_budget_engine", name="Budget"),
        action=action,
        cipher=_JsonCipher(),
        would="queue",
        reason="r",
        notes=[UNVERIFIED_GOOGLE],
        measure={"budget_points": 8.0},
        source="agent",
    )
    assert created and db.added == [item]
    assert item.status == ApprovalStatus.PRACTISED.value
    assert item.decision_note == agent_safety.PRACTISED_LABEL == "Practised — not sent"
    stored = json.loads(item.payload_encrypted)
    assert stored["percents"] == {"google": 58} and stored["before"] == {"google": 50}
    assert stored["_practice"]["would"] == "queue"
    assert stored["_practice"]["notes"] == [UNVERIFIED_GOOGLE]
    assert stored["_practice"]["writes_via"] == ["google_ads"]

    again, created = agent_safety.record_practice(
        _PracticeDb(existing=item), org=SimpleNamespace(id="t"),
        record=SimpleNamespace(slug="predictive_budget_engine", name="Budget"),
        action=action, cipher=_JsonCipher(), would="queue", reason="", notes=[],
        measure={}, source="agent",
    )
    assert again is item and not created


def test_approving_a_practised_item_is_refused(monkeypatch):
    from app.models.approval import ApprovalStatus
    from app.services import approvals

    item = SimpleNamespace(status=ApprovalStatus.PRACTISED.value)
    monkeypatch.setattr(approvals, "get_item", lambda db, **k: item)
    with pytest.raises(ConflictError, match="practice mode never sends"):
        approvals.approve(None, org=SimpleNamespace(id="t"), actor=None, item_id="x")


def test_practice_summary_sentence():
    rows = [
        {"would": "apply", "safety": {"budget_moved": 32.0}},
        {"would": "queue", "safety": {"budget_moved": 18.0}},
        {"would": "apply", "repeated": True, "safety": {"budget_moved": 99.0}},
    ]
    out = agent_safety.summarise_practice(rows, money=lambda v: f"₹{v:,.0f}")
    assert out["changes"] == 2 and out["automatic"] == 1 and out["needs_approval"] == 1
    assert out["budget_moved"] == 50.0
    assert out["sentence"].startswith(
        "In practice mode this agent would have made 2 changes this week "
        "(total budget moved: ₹50 of daily spend)."
    )
    none = agent_safety.summarise_practice([])
    assert none["sentence"] == "In practice mode this agent would have made 0 changes this week."


def test_agent_practice_switch_defaults():
    assert not safety.agent_practice_on("predictive_budget_engine", {})  # existing records
    assert safety.agent_practice_on("predictive_budget_engine", {"practice_mode": True})
    assert not safety.agent_practice_on("seo_article_writer", {"practice_mode": True})

    fresh = SimpleNamespace(slug="click_fraud_controller", settings={})
    agent_safety.default_practice(fresh)
    assert fresh.settings == {"practice_mode": True}
    chosen = SimpleNamespace(slug="click_fraud_controller", settings={"practice_mode": False})
    agent_safety.default_practice(chosen)
    assert chosen.settings == {"practice_mode": False}
    other = SimpleNamespace(slug="seo_article_writer", settings={})
    agent_safety.default_practice(other)
    assert other.settings == {}


def test_new_workspaces_install_spending_agents_in_practice_mode():
    from app.agents.base.registry import all_agents
    from app.services import provisioning

    added: list = []
    db = SimpleNamespace(
        execute=lambda stmt: iter(()),
        add=added.append,
        flush=lambda: None,
    )
    import app.services.portal_features as pf

    original = pf.is_enabled
    pf.is_enabled = lambda *a, **k: True
    try:
        provisioning.install_agents(db, SimpleNamespace(id="t", global_autonomy=False))
    finally:
        pf.is_enabled = original
    by_slug = {row.slug: row for row in added}
    assert len(by_slug) == len(list(all_agents()))
    for slug in safety.SPENDING_AGENTS:
        assert by_slug[slug].settings == {"practice_mode": True}
    assert by_slug["seo_article_writer"].settings == {}


# ── Limits ───────────────────────────────────────────────────────────────────
def test_budget_measure_reads_the_engines_own_fields():
    _, action, _, _ = _budget_run()
    m = safety.measure(action)
    assert m.budget_points == 8.0 and m.budget_moved == 32.0
    # A payload queued before these fields existed: points from the shifts.
    older = _action("rebalance_budget", {"shifts": {"google": 12, "meta": -7, "dsp": -5}})
    assert safety.measure(older).budget_points == 12.0
    assert safety.measure(older).budget_moved is None


def test_budget_change_over_the_per_change_limit():
    m = Measure(budget_points=35)
    assert safety.cap_reason(m, Caps(), Ledger()) == "Moves 35% of budget; your limit is 20% per change."
    assert safety.cap_reason(Measure(budget_points=20), Caps(), Ledger()) == ""


def test_budget_moved_per_day_adds_up_across_changes():
    caps = Caps(max_budget_moved_per_day=5000)
    money = lambda v: f"₹{v:,.0f}"  # noqa: E731
    assert safety.cap_reason(Measure(budget_points=5, budget_moved=4000), caps, Ledger(), money=money) == ""
    reason = safety.cap_reason(
        Measure(budget_points=5, budget_moved=1500), caps, Ledger(budget_moved=4000), money=money
    )
    assert reason == (
        "Moves ₹1,500 of daily budget on top of ₹4,000 already moved today; your limit "
        "is ₹5,000 a day."
    )


def test_counted_limits_for_campaigns_placements_and_creatives():
    caps = Caps(max_campaigns_per_day=2, max_placements_blocked_per_day=100, max_creatives_uploaded_per_day=20)
    campaign = safety.measure(_action("create_search_campaign", {"export_only": False}))
    assert campaign.campaigns == 1
    assert safety.cap_reason(campaign, caps, Ledger(campaigns=1)) == ""
    assert safety.cap_reason(campaign, caps, Ledger(campaigns=2)) == (
        "Would be new campaign number 3 today; your limit is 2 new campaigns a day."
    )
    # A download file creates nothing in an ad account.
    assert safety.measure(_action("create_search_campaign", {"export_only": True})).campaigns == 0

    block = safety.measure(_action("block_placement"))
    assert safety.cap_reason(block, caps, Ledger(placements=99)) == ""
    assert "number 101" in safety.cap_reason(block, caps, Ledger(placements=100))

    creative = safety.measure(_action("publish_creative"))
    assert "your limit is 20 ads uploaded a day" in safety.cap_reason(creative, caps, Ledger(creatives=20))
    assert safety.cap_reason(creative, Caps(max_creatives_uploaded_per_day=0), Ledger()) == (
        "Your limit is 0 ads uploaded a day, so each one waits for your approval."
    )
    audience = safety.measure(_action("activate_audience", {"platforms": ["google_ads", "meta_ads"]}))
    assert audience.audiences == 2 and safety.cap_reason(audience, caps, Ledger()) == ""


def test_caps_from_saved_values_keep_defaults_for_anything_unusable():
    caps = Caps.from_dict({"max_budget_change_pct": "15", "max_campaigns_per_day": -1, "nope": 3,
                           "max_budget_moved_per_day": "abc", "max_creatives_uploaded_per_day": True})
    assert caps.max_budget_change_pct == 15 and isinstance(caps.max_budget_change_pct, int)
    assert caps.max_campaigns_per_day == Caps().max_campaigns_per_day
    assert caps.max_budget_moved_per_day == Caps().max_budget_moved_per_day
    assert caps.max_creatives_uploaded_per_day == Caps().max_creatives_uploaded_per_day


def test_today_ledger_adds_up_what_was_applied():
    ledger = Ledger.from_measures([
        {"budget_moved": 100.5, "budget_points": 4},
        {"placements": 1},
        {"campaigns": 1},
        None,
        {"creatives": 1},
        {"creatives": 1},
    ])
    assert (ledger.budget_moved, ledger.placements, ledger.campaigns, ledger.creatives) == (100.5, 1, 1, 2)


def test_over_a_limit_is_queued_with_the_reason_never_applied(monkeypatch):
    sinks = _Sinks(monkeypatch)
    action = _action("rebalance_budget", {"moved_points": 35, "shifts": {"google": 35, "meta": -35}})
    tally = _process([action], gate=_gate(is_verified=lambda s: True))
    assert tally.queued == 1 and tally.applied == 0
    assert sinks.queued[0]["why"] == ["Moves 35% of budget; your limit is 20% per change."]
    assert sinks.audits[0]["context"]["held"] == sinks.queued[0]["why"]


def test_limits_count_changes_applied_earlier_in_the_same_run(monkeypatch):
    sinks = _Sinks(monkeypatch)
    applied: list = []
    actions = [
        _action("create_search_campaign", {"n": i}, apply=lambda c, i=i: applied.append(i))
        for i in range(3)
    ]
    gate = _gate(is_verified=lambda s: True, caps=Caps(max_campaigns_per_day=2))
    tally = _process(actions, record=_record("search_campaign_builder"), gate=gate)
    assert applied == [0, 1] and tally.applied == 2 and tally.queued == 1
    assert "your limit is 2 new campaigns a day" in sinks.queued[0]["why"][0]
    # Applied changes are recorded so tomorrow's ledger… today's ledger sees them.
    contexts = [a["context"] for a in sinks.audits if a["context"].get("auto_applied")]
    assert [c["safety"] for c in contexts] == [{"campaigns": 1}, {"campaigns": 1}]
    assert gate.ledger.campaigns == 2


def test_non_spending_agents_are_untouched_by_the_rules(monkeypatch):
    _Sinks(monkeypatch)
    applied: list = []
    action = _action("rewrite_page", writes_via=("wordpress",), apply=lambda c: applied.append(1))
    tally = _process([action], record=_record("on_page_seo_sync"), gate=SafetyGate())
    assert tally.applied == 1 and applied == [1]


# ── Unverified connectors ────────────────────────────────────────────────────
def test_unverified_connector_overrides_full_autonomy():
    action = _action("block_placement")
    decision, reason = decide(
        action, guardrail=Guardrail.FULL, agent_autonomous=True, remaining_actions=10,
        holds=[UNVERIFIED_GOOGLE],
    )
    assert decision is Decision.QUEUE and reason == UNVERIFIED_GOOGLE
    decision, _ = decide(action, guardrail=Guardrail.FULL, agent_autonomous=True, remaining_actions=10)
    assert decision is Decision.APPLY


def test_unverified_write_is_queued_with_the_plain_reason(monkeypatch):
    sinks = _Sinks(monkeypatch)
    action = _action("block_placement", writes_via=("google_ads",))
    tally = _process(
        [action], record=_record("click_fraud_controller"),
        gate=_gate(is_verified=lambda slug: slug != "google_ads"),
    )
    assert tally.queued == 1 and tally.applied == 0
    assert sinks.queued[0]["why"] == [UNVERIFIED_GOOGLE]


def test_verified_write_within_limits_is_applied(monkeypatch):
    sinks = _Sinks(monkeypatch)
    applied: list = []
    action = _action("block_placement", apply=lambda c: applied.append(1))
    tally = _process([action], record=_record("click_fraud_controller"), gate=_gate(is_verified=lambda s: True))
    assert tally.applied == 1 and applied == [1]
    assert sinks.audits[0]["context"]["auto_applied"] is True
    assert sinks.audits[0]["context"]["safety"] == {"placements": 1}


def test_unverified_wording_and_setup_note():
    assert safety.unverified_reason(["Google Ads"]) == UNVERIFIED_GOOGLE
    assert safety.unverified_reason(["Google Ads", "Meta Ads", "Google Ads"]).startswith(
        "Google Ads and Meta Ads haven't been confirmed"
    )
    assert safety.automatic_note(["Google Ads"]) == "Automatic changes switch on once Google Ads is verified."
    assert safety.automatic_note(["Google Ads", "Meta Ads"]) == (
        "Automatic changes switch on once Google Ads and Meta Ads are verified."
    )
    assert safety.automatic_note([]) == ""


def test_every_spending_agent_says_which_connector_it_writes_through():
    """The unverified rule depends on it; an empty writes_via reaches no vendor."""
    from app.agents.first_party_audience_modeler import agent as audience_agent

    cluster = SimpleNamespace(
        id="c1", label="Loyal", size=500, cohesion=0.9, top_signals=["a"], activated_channels=[]
    )
    push = _Ads("meta_ads", "meta", {Capability.PUSH_AUDIENCE})
    actions = audience_agent.AGENT._activate(_ctx(_FakeDb(), [push]), [cluster])
    assert actions and actions[0].writes_via == ("meta_ads",)


def test_creative_writes_through_its_platforms_connector():
    from app.agents.dynamic_creative_optimizer import agent as dco_agent
    from app.agents.dynamic_creative_optimizer import specs as dco_specs

    platform, spec = next(iter(dco_specs.PLATFORMS.items()))
    connector = _Ads(spec.connector_slug, "x", {Capability.UPLOAD_CREATIVE})
    assert dco_agent._writer_for(_ctx(_FakeDb(), [connector]), platform) == (spec.connector_slug,)
    # Not connected: still the platform's connector, never "nothing".
    assert dco_agent._writer_for(_ctx(_FakeDb(), []), platform) == (spec.connector_slug,)


# ── Kill switch ──────────────────────────────────────────────────────────────
def test_a_stop_during_a_run_turns_pending_auto_applies_into_proposals(monkeypatch):
    sinks = _Sinks(monkeypatch)
    applied: list = []
    state = {"halted": ""}
    first = _action("block_placement", {"n": 1}, apply=lambda c: (applied.append(1), state.update(halted="stopped")))
    second = _action("block_placement", {"n": 2}, apply=lambda c: applied.append(2))
    gate = _gate(is_verified=lambda s: True, halted=lambda: state["halted"])
    tally = _process([first, second], record=_record("click_fraud_controller"), gate=gate)
    assert applied == [1]
    assert tally.applied == 1 and tally.queued == 1 and tally.stopped_mid_run
    assert sinks.queued[0]["why"] == [agent_safety.STOPPED_MID_RUN]


def test_the_runner_skips_everything_while_stopped(monkeypatch):
    ran: list = []
    fake_agent = SimpleNamespace(
        spec=SimpleNamespace(default_interval=None),
        preflight=lambda ctx: ran.append("preflight"),
        run=lambda ctx: ran.append("run"),
    )
    monkeypatch.setattr(runner, "get_agent", lambda slug: fake_agent)
    monkeypatch.setattr(runner.agent_safety, "halt_reason", lambda db, org: agent_safety.PLATFORM_STOP_REASON)
    monkeypatch.setattr(runner, "build_context", lambda *a, **k: ran.append("context"))

    class _Db:
        def add(self, row) -> None:  # noqa: ANN001
            row.id = "run-1"

        def flush(self) -> None:
            pass

        def execute(self, stmt):  # noqa: ANN001, ANN201
            return SimpleNamespace(scalar_one_or_none=lambda: None)

    record = AgentRecord(slug="predictive_budget_engine", name="Budget", status=AgentStatus.RUNNING.value)
    # force=True is what "Run now" passes; the stop wins over it.
    outcome = runner.run_agent(_Db(), record=record, org=SimpleNamespace(id="t", slug="t"), force=True)
    assert outcome.status == RunStatus.SKIPPED.value
    assert outcome.summary == agent_safety.PLATFORM_STOP_REASON
    assert ran == []


def test_the_scheduler_claims_nothing_during_an_emergency_stop(monkeypatch):
    from app.orchestration.scheduler import AgentScheduler

    scheduler = AgentScheduler(tick_seconds=1)
    for name in ("_maybe_sweep_health", "_maybe_daily_housekeeping"):
        monkeypatch.setattr(scheduler, name, lambda: None)

    def claim():  # noqa: ANN202
        raise AssertionError("nothing may be claimed")

    published: list = []
    monkeypatch.setattr(scheduler, "_maybe_publish_scheduled", lambda: published.append(1))
    monkeypatch.setattr(scheduler, "_claim_due", claim)
    monkeypatch.setattr(AgentScheduler, "_emergency_stop", staticmethod(lambda: True))
    assert scheduler.tick() == 0
    assert published == []  # approved content is not published either

    monkeypatch.setattr(AgentScheduler, "_emergency_stop", staticmethod(lambda: False))
    monkeypatch.setattr(scheduler, "_claim_due", lambda: [])
    assert scheduler.tick() == 0 and published == [1]


def test_a_stopped_workspace_is_skipped_by_the_scheduler_check():
    assert agent_safety.workspace_stopped({"agent_safety": {"stopped": {"at": "x"}}})
    assert not agent_safety.workspace_stopped({"agent_safety": {"practice_mode": True}})
    assert not agent_safety.workspace_stopped(None)


class _AgentsDb:
    def __init__(self, records: list) -> None:
        self.records = records
        self.added: list = []

    def execute(self, stmt):  # noqa: ANN001, ANN201
        rows = list(self.records)
        return SimpleNamespace(scalars=lambda: iter(rows))

    def add(self, row) -> None:  # noqa: ANN001
        self.added.append(row)

    def flush(self) -> None:
        pass


def test_stop_all_pauses_every_agent_and_resume_restarts_the_same_ones():
    running = AgentRecord(slug="a", name="A", status=AgentStatus.RUNNING.value, configured=True)
    paused = AgentRecord(slug="b", name="B", status=AgentStatus.PAUSED.value, configured=True)
    running.next_run_at = paused.next_run_at = None
    db = _AgentsDb([running, paused])
    org = SimpleNamespace(id="t", settings={})
    actor = SimpleNamespace(id="u1", name="Asha", tenant_id="t")

    assert agent_safety.stop_workspace(db, org=org, actor=actor, ip_address="1.2.3.4") == 1
    assert running.status == AgentStatus.PAUSED.value and running.next_run_at is None
    assert agent_safety.stopped_info(org)["resume"] == ["a"]
    assert agent_safety.workspace_stopped(org.settings)
    stop_entry = db.added[-1]
    assert stop_entry.action == "stopped all agents (1 were running)"
    assert stop_entry.context["kill_switch"] == "workspace" and stop_entry.context["on"] is True
    assert stop_entry.actor == "Asha" and stop_entry.tenant_id == "t"
    with pytest.raises(ConflictError):
        agent_safety.stop_workspace(db, org=org, actor=actor)

    assert agent_safety.resume_workspace(db, org=org, actor=actor) == 1
    assert running.status == AgentStatus.RUNNING.value and running.next_run_at is not None
    assert paused.status == AgentStatus.PAUSED.value  # was not running before the stop
    assert agent_safety.stopped_info(org) is None
    assert db.added[-1].action == "resumed agents after a stop (1 restarted)"


def test_halt_reason_prefers_the_platform_stop(monkeypatch):
    org = SimpleNamespace(id="t", settings={"agent_safety": {"stopped": {"at": "x"}}})
    monkeypatch.setattr(agent_safety, "_fresh_settings", lambda db, o: o.settings)
    monkeypatch.setattr(agent_safety, "platform_switch", lambda db, name: name == agent_safety.EMERGENCY_STOP)
    assert agent_safety.halt_reason(None, org) == agent_safety.PLATFORM_STOP_REASON
    monkeypatch.setattr(agent_safety, "platform_switch", lambda db, name: False)
    assert agent_safety.halt_reason(None, org) == agent_safety.WORKSPACE_STOP_REASON
    assert agent_safety.halt_reason(None, SimpleNamespace(id="t", settings={})) == ""


def test_approvals_wait_during_an_emergency_stop(monkeypatch):
    from app.models.approval import ApprovalStatus
    from app.services import approvals

    monkeypatch.setattr(approvals, "get_item", lambda db, **k: SimpleNamespace(status=ApprovalStatus.PENDING.value))
    monkeypatch.setattr(agent_safety, "platform_switch", lambda db, name: name == agent_safety.EMERGENCY_STOP)
    with pytest.raises(ConflictError, match="Emergency stop"):
        approvals.approve(None, org=SimpleNamespace(id="t"), actor=None, item_id="x")


# ── API guards ───────────────────────────────────────────────────────────────
def _current(role: str, *, email: str = "person@example.com", settings: dict | None = None):  # noqa: ANN202
    from app.api.deps import CurrentUser

    user = SimpleNamespace(id="u1", name="Person", email=email, role=role, tenant_id="t")
    org = SimpleNamespace(id="t", name="Acme", settings=dict(settings or {}))
    return CurrentUser(user=user, organization=org, claims={})


class _CommitDb:
    def __init__(self) -> None:
        self.commits = 0
        self.added: list = []

    def commit(self) -> None:
        self.commits += 1

    def add(self, row) -> None:  # noqa: ANN001
        self.added.append(row)

    def flush(self) -> None:
        pass


@pytest.mark.parametrize("role", ["manager", "seo", "ads", "approver", "client"])
def test_only_workspace_admins_can_stop_change_limits_or_practice(role: str):
    from app.api.v1 import safety as safety_api
    from app.schemas.safety import CapsRequest, SwitchRequest

    current = _current(role)
    db = _CommitDb()
    with pytest.raises(ForbiddenError):
        safety_api.stop_all(current, db, "ip")
    with pytest.raises(ForbiddenError):
        safety_api.resume_all(current, db, "ip")
    with pytest.raises(ForbiddenError):
        safety_api.save_caps(CapsRequest(caps={"max_campaigns_per_day": 5}), current, db, "ip")
    with pytest.raises(ForbiddenError):
        safety_api.set_practice(SwitchRequest(on=True), current, db, "ip")
    assert db.commits == 0


def test_a_workspace_admin_can_stop_and_the_status_says_so(monkeypatch):
    from app.api.v1 import safety as safety_api

    current = _current("admin")
    stopped: list = []
    monkeypatch.setattr(
        safety_api.agent_safety, "stop_workspace",
        lambda db, **k: stopped.append(k["actor"].id) or 3,
    )
    monkeypatch.setattr(
        safety_api.agent_safety, "platform_info",
        lambda db: {name: {"on": False, "by": "", "at": ""} for name in agent_safety.PLATFORM_SWITCHES},
    )
    out = safety_api.stop_all(current, _CommitDb(), "ip")
    assert stopped == ["u1"]
    assert out.toast.message == "All agents stopped (3 agents were running)"
    assert out.status.can_manage is True


def test_limits_are_validated_before_saving():
    current = _current("admin")
    with pytest.raises(InvalidInputError, match="cannot be negative"):
        agent_safety.save_caps(_CommitDb(), org=current.organization, actor=current.user,
                               values={"max_campaigns_per_day": -2})
    with pytest.raises(InvalidInputError, match="no limit called"):
        agent_safety.save_caps(_CommitDb(), org=current.organization, actor=current.user,
                               values={"max_rockets": 2})
    with pytest.raises(InvalidInputError, match="more than 100%"):
        agent_safety.save_caps(_CommitDb(), org=current.organization, actor=current.user,
                               values={"max_budget_change_pct": 150})
    db = _CommitDb()
    caps = agent_safety.save_caps(db, org=current.organization, actor=current.user,
                                  values={"max_budget_change_pct": 10, "max_budget_moved_per_day": 250.5})
    assert caps.max_budget_change_pct == 10 and caps.max_budget_moved_per_day == 250.5
    assert agent_safety.caps_for(current.organization).max_budget_change_pct == 10
    assert db.added[-1].action == "changed the limits on spending agents"


def test_only_portal_admins_can_use_the_platform_switches(monkeypatch):
    from app.api.v1 import safety as safety_api
    from app.core.config import settings
    from app.schemas.safety import SwitchRequest

    monkeypatch.setattr(
        type(settings), "is_portal_admin_email", lambda self, email: email == "ops@platform.test"
    )
    with pytest.raises(ForbiddenError):
        safety_api.platform_emergency_stop(SwitchRequest(on=True), _current("admin"), _CommitDb(), "ip")
    with pytest.raises(ForbiddenError):
        safety_api.platform_practice(SwitchRequest(on=True), _current("admin"), _CommitDb(), "ip")

    switched: list = []
    monkeypatch.setattr(
        safety_api.agent_safety, "set_platform_switch",
        lambda db, **k: switched.append((k["name"], k["on"])) or True,
    )
    monkeypatch.setattr(
        safety_api.agent_safety, "platform_info",
        lambda db: {
            agent_safety.EMERGENCY_STOP: {"on": True, "by": "ops@platform.test", "at": ""},
            agent_safety.PLATFORM_PRACTICE: {"on": False, "by": "", "at": ""},
        },
    )
    db = _CommitDb()
    admin = _current("client", email="ops@platform.test")
    out = safety_api.platform_emergency_stop(SwitchRequest(on=True), admin, db, "ip")
    assert switched == [(agent_safety.EMERGENCY_STOP, True)]
    assert out.status.emergency_stop.on is True
    entry = db.added[-1]
    assert entry.module == "portal" and entry.context == {"platform_switch": "emergency_stop", "on": True}
    assert "emergency stop" in entry.action


def test_run_now_and_resume_are_refused_while_stopped(monkeypatch):
    from fastapi import BackgroundTasks

    from app.api.v1 import agents as agents_api

    record = SimpleNamespace(slug="click_fraud_controller", name="Click-Fraud Controller", configured=True)
    monkeypatch.setattr(agents_api, "_record", lambda db, tenant_id, slug: record)
    monkeypatch.setattr(agent_safety, "halt_reason", lambda db, org: agent_safety.WORKSPACE_STOP_REASON)
    background = BackgroundTasks()
    with pytest.raises(ConflictError, match="stopped"):
        agents_api.run_now("click_fraud_controller", background, _current("admin"), _CommitDb(), "ip")
    assert not background.tasks
    with pytest.raises(ConflictError, match="stopped"):
        agents_api.resume("click_fraud_controller", _current("admin"), _CommitDb(), "ip")


def test_practice_switch_is_only_for_spending_agents(monkeypatch):
    from app.api.v1 import agents as agents_api
    from app.schemas.safety import PracticeRequest

    record = SimpleNamespace(slug="seo_article_writer", name="SEO Article Writer", settings={})
    monkeypatch.setattr(agents_api, "_record", lambda db, tenant_id, slug: record)
    with pytest.raises(InvalidInputError, match="no practice mode"):
        agents_api.set_practice("seo_article_writer", PracticeRequest(on=True), _current("admin"), _CommitDb(), "ip")
    with pytest.raises(ForbiddenError):
        agents_api.set_practice("click_fraud_controller", PracticeRequest(on=True), _current("client"), _CommitDb(), "ip")


def test_configuring_a_spending_agent_for_the_first_time_turns_practice_on():
    from app.api.v1 import agents as agents_api

    new = SimpleNamespace(slug="predictive_budget_engine", settings={})
    agents_api._apply_practice_choice(new, None, first=True)
    assert new.settings == {"practice_mode": True}

    existing = SimpleNamespace(slug="predictive_budget_engine", settings={})
    agents_api._apply_practice_choice(existing, None, first=False)
    assert existing.settings == {}  # keeps its current behaviour

    chosen = SimpleNamespace(slug="predictive_budget_engine", settings={"x": 1})
    agents_api._apply_practice_choice(chosen, False, first=True)
    assert chosen.settings == {"x": 1, "practice_mode": False}


def test_approval_cards_show_why_a_change_waits():
    from datetime import datetime, timezone

    from app.models.approval import ApprovalStatus
    from app.services import views

    now = datetime.now(timezone.utc)
    item = SimpleNamespace(
        id="1", type="Traffic Block", title="t", agent_name="A", agent_slug="click_fraud_controller",
        meta="m", target_kind="fraud_event", submitted_at=now, status=ApprovalStatus.PENDING.value,
    )
    out = views.approval_out(item, {"source": "x", "_why_waiting": [UNVERIFIED_GOOGLE]})
    assert out.why == [UNVERIFIED_GOOGLE] and not out.practice
    item.status = ApprovalStatus.PRACTISED.value
    out = views.approval_out(item, {"_practice": {"would": "apply", "notes": []}})
    assert out.practice and out.would == "apply" and out.status == "practised"
