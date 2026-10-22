"""On-demand agents, strict model choice in the runner, and run model recording.

Mostly pure unit tests: the runner is driven with a stand-in session and the
connector bundle, plan lookup and model provider replaced, so no database or
vendor is touched. The provisioning check against a real database is marked
``requires_db``.
"""
from __future__ import annotations

import logging
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from app.agents.base import registry
from app.agents.base.context import AgentContext, LLMOverride
from app.agents.base.contracts import AgentCategory, AgentResult, AgentSpec, BaseAgent
from app.llm.base import LLMError
from app.models.agent import RunStatus
from app.orchestration import runner
from tests.conftest import requires_db

ON_DEMAND_SLUG = "test_on_demand_writer"
FLEET_SLUG = "test_fleet_writer"
SECRET = "sk-test-SECRET-key-0123456789abcdef"
EMAIL = "jane.doe@customer.example"
POISON = f"vendor said: Authorization: Bearer {SECRET} for {EMAIL}"


class _Writer(BaseAgent):
    """Records the context it was handed; optionally fails with POISON."""

    def __init__(self, spec: AgentSpec, *, fail: bool = False) -> None:
        self.spec = spec
        self.fail = fail
        self.seen: list[AgentContext] = []
        super().__init__()

    def run(self, ctx: AgentContext) -> AgentResult:
        self.seen.append(ctx)
        if self.fail:
            raise RuntimeError(POISON)
        return AgentResult(summary="Drafted 2 variants")


def _spec(slug: str, *, on_demand: bool) -> AgentSpec:
    return AgentSpec(
        slug=slug,
        name=slug.replace("_", " ").title(),
        category=AgentCategory.CONTENT,
        description="test agent",
        default_interval=timedelta(days=1),
        requires_llm=True,
        on_demand=on_demand,
    )


@pytest.fixture
def agents(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    registry._discover()
    on_demand = _Writer(_spec(ON_DEMAND_SLUG, on_demand=True))
    fleet = _Writer(_spec(FLEET_SLUG, on_demand=False))
    monkeypatch.setitem(registry._registry, ON_DEMAND_SLUG, on_demand)
    monkeypatch.setitem(registry._registry, FLEET_SLUG, fleet)
    return SimpleNamespace(on_demand=on_demand, fleet=fleet)


@pytest.fixture
def providers(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Connected: Anthropic Claude only. Records every provider built."""
    from app.llm import from_connector
    from app.services import entitlements

    built: list[dict[str, Any]] = []

    def fake_provider(slug: str, connectors: dict, *, names=None, model_override=None):  # noqa: ANN001, ANN202
        built.append({"slug": slug, "model_override": model_override})
        return SimpleNamespace(name=slug, model=model_override or "m-saved")

    bundle = SimpleNamespace(
        instances={"anthropic_claude": object()},
        names={"anthropic_claude": "Anthropic Claude", "openai": "OpenAI"},
        connected=frozenset({"anthropic_claude"}),
    )
    monkeypatch.setattr(runner, "build_connector_bundle", lambda db, org: bundle)
    monkeypatch.setattr(
        entitlements, "lookup", lambda db, org: lambda slug, cap: SimpleNamespace(permits=lambda s: True)
    )
    monkeypatch.setattr(runner, "OrgCipher", SimpleNamespace(for_org=lambda org: object()))
    monkeypatch.setattr(from_connector, "provider_from_connector", fake_provider)
    return built


class _Result:
    def scalar_one_or_none(self) -> None:
        return None


class _FakeSession:
    """Just enough of a Session for one run_agent pass."""

    def __init__(self) -> None:
        self.added: list[Any] = []
        self.commits = 0

    def add(self, obj: Any) -> None:  # noqa: ANN401
        self.added.append(obj)

    def flush(self) -> None:
        for obj in self.added:
            if getattr(obj, "id", None) is None:
                obj.id = f"run-{len(self.added)}"

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        pass

    def execute(self, *args: Any, **kwargs: Any) -> _Result:  # noqa: ANN401
        return _Result()


@pytest.fixture
def quiet_run(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Stop switches off, no progress writes, metrics and audit captured."""
    audits: list[dict[str, Any]] = []
    monkeypatch.setattr(runner.agent_safety, "halt_reason", lambda db, org: "")
    monkeypatch.setattr(
        runner.agent_safety,
        "gate_for",
        lambda db, org, record, **kw: SimpleNamespace(practice=None, halted=lambda: "", spending=False),
    )
    monkeypatch.setattr(runner.metrics, "accumulate", lambda db, **kw: None)
    monkeypatch.setattr(runner.audit, "record_agent_action", lambda db, **kw: audits.append(kw))
    monkeypatch.setattr(AgentContext, "progress", lambda self, *a, **k: None)
    return audits


def _org() -> SimpleNamespace:
    return SimpleNamespace(id="t1", name="Acme", slug="acme", settings={})


def _fleet_record(*, choice: str = "openai", strict: bool = False) -> SimpleNamespace:
    return SimpleNamespace(
        tenant_id="t1",
        slug=FLEET_SLUG,
        name="Fleet Writer",
        category="Content & Social",
        llm_connector=choice,
        settings={"strict_model": True} if strict else {},
        status="running",
        is_running=True,
        autonomy=False,
        scope="",
        schedule="Daily",
        notify_channel="None",
        max_actions_per_day=20,
        actions_today=0,
        actions_today_date="",
        consecutive_failures=0,
        last_error="",
        metric_label="",
        last_run_at=None,
        next_run_at=None,
    )


# ── Registry and provisioning ──────────────────────────────────────────────
def test_on_demand_agents_are_not_part_of_the_fleet(agents: SimpleNamespace) -> None:
    fleet_slugs = registry.agent_slugs()
    assert ON_DEMAND_SLUG not in fleet_slugs
    assert FLEET_SLUG in fleet_slugs
    assert agents.on_demand in registry.on_demand_agents()
    assert agents.fleet not in registry.on_demand_agents()
    # Still runnable by slug.
    assert registry.get_agent(ON_DEMAND_SLUG) is agents.on_demand


def test_provisioning_skips_on_demand_agents(
    agents: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.services import portal_features, provisioning

    class _Db(_FakeSession):
        def execute(self, *args: Any, **kwargs: Any) -> list:  # noqa: ANN401
            return []

    monkeypatch.setattr(portal_features, "is_enabled", lambda db, kind, slug: True)
    db = _Db()
    org = SimpleNamespace(id="t1", global_autonomy=False)
    provisioning.install_agents(db, org)  # type: ignore[arg-type]
    slugs = {getattr(row, "slug", None) for row in db.added}
    assert FLEET_SLUG in slugs
    assert ON_DEMAND_SLUG not in slugs


@requires_db
def test_provisioning_installs_no_row_for_an_on_demand_agent(clean_db, org, agents) -> None:  # noqa: ANN001
    from sqlalchemy import select

    from app.models.agent import AgentRecord
    from app.services import provisioning

    provisioning.install_agents(clean_db, org)
    clean_db.commit()
    rows = clean_db.execute(
        select(AgentRecord.slug).where(
            AgentRecord.tenant_id == org.id, AgentRecord.slug == ON_DEMAND_SLUG
        )
    ).all()
    assert rows == []


# ── build_context: strict and fallback ─────────────────────────────────────
def test_saved_choice_still_falls_back_when_not_strict(agents, providers) -> None:  # noqa: ANN001
    ctx = runner.build_context(None, _fleet_record(choice="openai"), _org())  # type: ignore[arg-type]
    assert providers == [{"slug": "anthropic_claude", "model_override": None}]
    assert ctx.strict_model is False
    assert ctx.llm_connector == "anthropic_claude"
    assert ctx.llm_model == "m-saved"
    assert ctx.job is None


def test_strict_model_setting_never_falls_back(agents, providers) -> None:  # noqa: ANN001
    with pytest.raises(LLMError) as raised:
        runner.build_context(None, _fleet_record(choice="openai", strict=True), _org())  # type: ignore[arg-type]
    assert providers == []
    assert "OpenAI" in str(raised.value)


def test_job_model_is_strict_and_used_exactly(agents, providers) -> None:  # noqa: ANN001
    record = runner._on_demand_record(agents.on_demand, _org())  # type: ignore[arg-type]
    job = {"job_id": "j1", "formats": ["square"]}
    ctx = runner.build_context(
        None,  # type: ignore[arg-type]
        record,
        _org(),  # type: ignore[arg-type]
        trigger="job",
        llm_override=LLMOverride(connector="anthropic_claude", model="m-chosen"),
        job=job,
    )
    assert providers == [{"slug": "anthropic_claude", "model_override": "m-chosen"}]
    assert ctx.strict_model is True
    assert (ctx.llm_connector, ctx.llm_model) == ("anthropic_claude", "m-chosen")
    assert ctx.job == job
    # The agent gets a read-only view, so it cannot change the caller's job.
    with pytest.raises(TypeError):
        ctx.job["job_id"] = "other"  # type: ignore[index]
    # And a job's choice replaces the saved-choice preflight.
    assert agents.on_demand.preflight(ctx) is None


def test_job_model_that_is_not_connected_never_falls_back(agents, providers) -> None:  # noqa: ANN001
    record = runner._on_demand_record(agents.on_demand, _org())  # type: ignore[arg-type]
    with pytest.raises(LLMError):
        runner.build_context(
            None,  # type: ignore[arg-type]
            record,
            _org(),  # type: ignore[arg-type]
            trigger="job",
            llm_override=LLMOverride(connector="openai"),
            job={},
        )
    assert providers == []


def test_unknown_job_connector_is_not_echoed(agents, providers) -> None:  # noqa: ANN001
    record = runner._on_demand_record(agents.on_demand, _org())  # type: ignore[arg-type]
    injected = "<img src=x onerror=alert(1)>"
    with pytest.raises(LLMError) as raised:
        runner.build_context(
            None,  # type: ignore[arg-type]
            record,
            _org(),  # type: ignore[arg-type]
            llm_override=LLMOverride(connector=injected),
            job={},
        )
    assert injected not in str(raised.value)
    assert providers == []


def test_job_must_be_a_mapping(agents, providers) -> None:  # noqa: ANN001
    record = runner._on_demand_record(agents.on_demand, _org())  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        runner.build_context(None, record, _org(), job=["not", "a", "job"])  # type: ignore[arg-type]


# ── run_agent / run_on_demand ──────────────────────────────────────────────
def test_strict_run_with_a_disconnected_model_is_skipped(agents, providers, quiet_run) -> None:  # noqa: ANN001
    db = _FakeSession()
    record = _fleet_record(choice="openai", strict=True)
    outcome = runner.run_agent(db, record=record, org=_org(), trigger="schedule")  # type: ignore[arg-type]
    assert outcome.status == RunStatus.SKIPPED.value
    assert "OpenAI" in outcome.summary
    assert agents.fleet.seen == []
    assert providers == []
    assert outcome.run.llm_connector in ("", None)


def test_on_demand_run_records_model_and_hands_over_the_job(agents, providers, quiet_run) -> None:  # noqa: ANN001
    db = _FakeSession()
    outcome = runner.run_on_demand(
        db,  # type: ignore[arg-type]
        org=_org(),  # type: ignore[arg-type]
        slug=ON_DEMAND_SLUG,
        job={"job_id": "j1"},
        llm_override=LLMOverride(connector="anthropic_claude", model="m-chosen"),
        triggered_by="user-1",
    )
    assert outcome.status == RunStatus.SUCCEEDED.value
    assert outcome.run.trigger == "job"
    assert outcome.run.llm_connector == "anthropic_claude"
    assert outcome.run.llm_model == "m-chosen"
    (ctx,) = agents.on_demand.seen
    assert ctx.job == {"job_id": "j1"}
    # The stand-in configuration row is never saved.
    assert all(getattr(obj, "slug", None) != ON_DEMAND_SLUG for obj in db.added)


def test_scheduled_run_records_the_fallback_model(agents, providers, quiet_run) -> None:  # noqa: ANN001
    db = _FakeSession()
    record = _fleet_record(choice="anthropic_claude")
    outcome = runner.run_agent(db, record=record, org=_org())  # type: ignore[arg-type]
    assert outcome.status == RunStatus.SUCCEEDED.value
    assert (outcome.run.llm_connector, outcome.run.llm_model) == ("anthropic_claude", "m-saved")


def test_on_demand_agent_without_a_job_does_not_run(agents, providers, quiet_run) -> None:  # noqa: ANN001
    db = _FakeSession()
    record = runner._on_demand_record(agents.on_demand, _org())  # type: ignore[arg-type]
    outcome = runner.run_agent(db, record=record, org=_org(), trigger="schedule")  # type: ignore[arg-type]
    assert outcome.status == RunStatus.SKIPPED.value
    assert agents.on_demand.seen == []


def test_run_on_demand_refuses_fleet_agents_and_bad_input(agents, providers, quiet_run) -> None:  # noqa: ANN001
    from app.core.exceptions import InvalidInputError, NotFoundError

    db = _FakeSession()
    with pytest.raises(NotFoundError):
        runner.run_on_demand(db, org=_org(), slug=FLEET_SLUG, job={})  # type: ignore[arg-type]
    with pytest.raises(NotFoundError):
        runner.run_on_demand(db, org=_org(), slug="no_such_agent", job={})  # type: ignore[arg-type]
    with pytest.raises(InvalidInputError):
        runner.run_on_demand(db, org=_org(), slug=ON_DEMAND_SLUG, job={}, trigger="schedule")  # type: ignore[arg-type]
    with pytest.raises(InvalidInputError):
        runner.run_on_demand(db, org=_org(), slug=ON_DEMAND_SLUG, job="x")  # type: ignore[arg-type]
    assert agents.fleet.seen == [] and agents.on_demand.seen == []


def test_failed_run_logs_and_stores_no_raw_exception_text(
    agents, providers, quiet_run, caplog: pytest.LogCaptureFixture  # noqa: ANN001
) -> None:
    agents.on_demand.fail = True
    caplog.set_level(logging.DEBUG)
    db = _FakeSession()
    outcome = runner.run_on_demand(
        db,  # type: ignore[arg-type]
        org=_org(),  # type: ignore[arg-type]
        slug=ON_DEMAND_SLUG,
        job={"job_id": "j1"},
        llm_override=LLMOverride(connector="anthropic_claude"),
    )
    assert outcome.status == RunStatus.FAILED.value
    # The model is recorded on failed runs too.
    assert outcome.run.llm_connector == "anthropic_claude"
    for text in (outcome.run.error, caplog.text):
        assert SECRET not in text
        assert EMAIL not in text
    assert "Traceback" not in caplog.text
    assert "agent.run_failed" in caplog.text


def test_failed_preflight_logs_no_raw_exception_text(
    agents, providers, quiet_run, monkeypatch, caplog: pytest.LogCaptureFixture  # noqa: ANN001
) -> None:
    def boom(ctx: AgentContext) -> None:
        raise RuntimeError(POISON)

    monkeypatch.setattr(agents.fleet, "preflight", boom)
    caplog.set_level(logging.DEBUG)
    record = _fleet_record(choice="anthropic_claude")
    outcome = runner.run_agent(_FakeSession(), record=record, org=_org())  # type: ignore[arg-type]
    assert outcome.status == RunStatus.FAILED.value
    for text in (record.last_error, outcome.run.error, caplog.text):
        assert SECRET not in text
        assert EMAIL not in text
    assert "Traceback" not in caplog.text
    assert "agent.preflight_failed" in caplog.text
