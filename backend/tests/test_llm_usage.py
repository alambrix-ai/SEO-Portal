"""LLM usage and estimated cost (app.llm.pricing, app.services.llm_usage).

The first half is pure: pricing precedence, the never-zero rule for an
unpriced model, unknown token counts, the range cap and the cursor. A
recording session checks that every statement is tenant-filtered and that a
page costs a fixed number of queries. The ``requires_db`` tests repeat the
grouping and the tenant isolation against real Postgres.
"""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy.dialects import postgresql

from app.core.config import settings
from app.core.exceptions import InvalidInputError
from app.llm import pricing
from app.services import llm_usage
from app.services.llm_usage import CostStatus, ModelBucket, UsageGroup
from tests.conftest import requires_db

TENANT = "11111111-1111-1111-1111-111111111111"
TABLE = {
    "openai:*": [1.0, 2.0],
    "openai:gpt-4o*": [2.5, 10.0],
    "openai:gpt-4o-mini*": [0.15, 0.6],
    "*:gpt-4o-mini": [9.0, 9.0],
    "anthropic:claude-*": [3.0, 15.0],
}


@pytest.fixture
def prices(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "llm_price_table", dict(TABLE))
    monkeypatch.setattr(settings, "llm_price_input_per_mtok", 0.0)
    monkeypatch.setattr(settings, "llm_price_output_per_mtok", 0.0)


# ── Pricing ────────────────────────────────────────────────────────────────
def test_the_most_specific_pattern_wins(prices: None) -> None:
    assert pricing.lookup("openai", "gpt-4o-mini-2024").source == "openai:gpt-4o-mini*"
    assert pricing.lookup("openai", "gpt-4o-2024").source == "openai:gpt-4o*"
    assert pricing.lookup("openai", "o3").source == "openai:*"
    assert pricing.lookup("anthropic", "claude-sonnet").source == "anthropic:claude-*"


def test_an_exact_model_beats_a_pattern_whatever_the_connector(prices: None) -> None:
    # "gpt-4o-mini" pins down more of the model than "gpt-4o-mini*" does
    # with the same literal text, because it has no wildcard.
    assert pricing.lookup("openai", "gpt-4o-mini").source == "*:gpt-4o-mini"


def test_precedence_does_not_depend_on_table_order(monkeypatch: pytest.MonkeyPatch) -> None:
    forward = {"openai:gpt-*": [1.0, 1.0], "openai:gpt-4*": [2.0, 2.0]}
    monkeypatch.setattr(settings, "llm_price_table", forward)
    first = pricing.lookup("openai", "gpt-4.1")
    monkeypatch.setattr(settings, "llm_price_table", dict(reversed(list(forward.items()))))
    assert pricing.lookup("openai", "gpt-4.1") == first
    assert first is not None and first.source == "openai:gpt-4*"


def test_matching_ignores_case_but_not_the_connector(prices: None) -> None:
    assert pricing.lookup("OpenAI", "GPT-4o").source == "openai:gpt-4o*"
    # A pattern for one connector never prices another's model.
    assert pricing.lookup("google_gemini", "claude-sonnet") is None


def test_estimate_prices_per_million_tokens(prices: None) -> None:
    cost, priced = pricing.estimate("anthropic", "claude-x", 1_000_000, 500_000)
    assert priced is True
    assert cost == pytest.approx(3.0 + 7.5)


def test_an_unpriced_model_costs_none_never_zero(prices: None) -> None:
    assert pricing.estimate("vllm", "llama-3", 1_000, 1_000) == (None, False)
    # Zero tokens on an unpriced model is still "No price set", not free.
    assert pricing.estimate("vllm", "llama-3", 0, 0) == (None, False)


def test_an_empty_table_and_zero_flat_rates_price_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "llm_price_table", {})
    monkeypatch.setattr(settings, "llm_price_input_per_mtok", 0.0)
    monkeypatch.setattr(settings, "llm_price_output_per_mtok", 0.0)
    assert pricing.estimate("openai", "gpt-4o", 10, 10) == (None, False)


def test_the_flat_rates_are_the_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "llm_price_table", {"openai:gpt-4o": [5.0, 5.0]})
    monkeypatch.setattr(settings, "llm_price_input_per_mtok", 1.0)
    monkeypatch.setattr(settings, "llm_price_output_per_mtok", 2.0)
    assert pricing.lookup("vllm", "llama").source == ""
    assert pricing.estimate("vllm", "llama", 1_000_000, 1_000_000) == (3.0, True)
    # A run with no recorded model cannot match a table key; only the flat
    # rates can price it.
    assert pricing.lookup("", "").source == ""


def test_a_key_without_a_connector_prices_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "llm_price_table", {"gpt-*": [1.0, 1.0], ":gpt-4": [1.0, 1.0]})
    monkeypatch.setattr(settings, "llm_price_input_per_mtok", 0.0)
    monkeypatch.setattr(settings, "llm_price_output_per_mtok", 0.0)
    assert pricing.lookup("openai", "gpt-4") is None


def test_unknown_tokens_on_a_priced_model_cost_none(prices: None) -> None:
    assert pricing.estimate("openai", "gpt-4o", None, None) == (None, True)


# ── Roll-up ────────────────────────────────────────────────────────────────
def _b(connector: str, model: str, t_in: int | None, t_out: int | None, *, runs: int = 1,
       unknown: int = 0) -> ModelBucket:
    return ModelBucket(connector, model, runs, t_in, t_out, unknown)


def test_a_fully_priced_group_is_an_estimate(prices: None) -> None:
    rolled = llm_usage.roll_up(
        [_b("anthropic", "claude-a", 1_000_000, 0), _b("openai", "o3", 0, 1_000_000)]
    )
    assert rolled.status is CostStatus.ESTIMATE
    assert rolled.cost == pytest.approx(3.0 + 2.0)
    assert (rolled.tokens_in, rolled.tokens_out, rolled.runs) == (1_000_000, 1_000_000, 2)


def test_zero_token_runs_on_a_priced_model_are_fine(prices: None) -> None:
    rolled = llm_usage.roll_up([_b("openai", "o3", 0, 0, runs=3)])
    assert rolled.status is CostStatus.ESTIMATE
    assert rolled.cost == 0.0
    assert rolled.tokens_in == 0


def test_a_group_with_no_price_reads_no_price_set(prices: None) -> None:
    rolled = llm_usage.roll_up([_b("vllm", "llama", 500, 500)])
    assert rolled.status is CostStatus.NO_PRICE
    assert rolled.cost is None
    assert llm_usage.COST_LABELS[rolled.status] == "No price set"


def test_an_unpriced_model_beside_a_priced_one_makes_a_partial_estimate(prices: None) -> None:
    rolled = llm_usage.roll_up(
        [_b("anthropic", "claude-a", 1_000_000, 0), _b("vllm", "llama", 10, 10)]
    )
    assert rolled.status is CostStatus.PARTIAL
    assert rolled.cost == pytest.approx(3.0)


def test_unknown_token_counts_are_unknown_not_zero(prices: None) -> None:
    rolled = llm_usage.roll_up([_b("openai", "o3", None, None, runs=2, unknown=2)])
    assert rolled.status is CostStatus.UNKNOWN
    assert rolled.tokens_in is None and rolled.tokens_out is None
    assert rolled.cost is None
    assert rolled.unknown_runs == 2
    assert llm_usage.tokens_text(rolled.tokens_in) == "unknown"
    assert llm_usage.tokens_text(0) == "0"


def test_some_unknown_runs_make_the_estimate_partial(prices: None) -> None:
    rolled = llm_usage.roll_up([_b("openai", "o3", 1_000_000, 0, runs=3, unknown=1)])
    assert rolled.status is CostStatus.PARTIAL
    assert rolled.cost == pytest.approx(1.0)
    assert rolled.unknown_runs == 1


def test_a_run_without_a_recorded_model_is_labelled() -> None:
    assert llm_usage._model_label("", "") == llm_usage.MODEL_NOT_RECORDED
    assert llm_usage._model_label("openai", "o3") == "openai · o3"


# ── Range, limit and cursor ────────────────────────────────────────────────
class RecordingSession:
    """Records each statement; every query returns no rows."""

    def __init__(self) -> None:
        self.statements: list[Any] = []

    def execute(self, stmt: Any) -> RecordingSession:  # noqa: ANN401
        self.statements.append(stmt)
        return self

    def all(self) -> list[Any]:
        return []


def test_the_range_is_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "insights_max_range_days", 30)
    start = date(2026, 1, 1)
    llm_usage.usage_window(start, start + timedelta(days=29))
    db = RecordingSession()
    with pytest.raises(InvalidInputError):
        llm_usage.summary(db, TENANT, start, start + timedelta(days=30))  # type: ignore[arg-type]
    with pytest.raises(InvalidInputError):
        llm_usage.summary(db, TENANT, start, start - timedelta(days=1))  # type: ignore[arg-type]
    assert db.statements == []


def test_a_bad_group_or_page_size_is_refused() -> None:
    db = RecordingSession()
    day = date(2026, 1, 1)
    with pytest.raises(InvalidInputError):
        llm_usage.summary(db, TENANT, day, day, group="everything")  # type: ignore[arg-type]
    for limit in (0, llm_usage.MAX_PAGE_SIZE + 1):
        with pytest.raises(InvalidInputError):
            llm_usage.summary(db, TENANT, day, day, limit=limit)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        llm_usage.summary(db, "", day, day)  # type: ignore[arg-type]
    assert db.statements == []


@pytest.mark.parametrize(
    ("group", "parts"),
    [
        (UsageGroup.AGENT, ["seo_optimizer"]),
        (UsageGroup.MODEL, ["openai", "gpt-4o"]),
        (UsageGroup.DAY, ["2026-01-02"]),
        (UsageGroup.JOB, ["2026-01-02T10:00:00+00:00", "22222222-2222-2222-2222-222222222222"]),
    ],
)
def test_a_cursor_round_trips(group: UsageGroup, parts: list[str]) -> None:
    decoded = llm_usage.decode_cursor(llm_usage.encode_cursor(parts), group)
    assert len(decoded) == len(parts)


@pytest.mark.parametrize(
    ("group", "cursor"),
    [
        (UsageGroup.AGENT, "not base64 !!"),
        (UsageGroup.AGENT, llm_usage.encode_cursor(["a", "b"])),
        (UsageGroup.AGENT, llm_usage.encode_cursor(["x" * 65])),
        (UsageGroup.DAY, llm_usage.encode_cursor(["'; DROP TABLE agent_runs; --"])),
        (UsageGroup.JOB, llm_usage.encode_cursor(["2026-01-02T10:00:00", "not-a-uuid"])),
        (UsageGroup.JOB, llm_usage.encode_cursor(["2026-01-02T10:00:00", str(TENANT)])),
        (UsageGroup.MODEL, "A" * (llm_usage.CURSOR_MAX_LENGTH + 1)),
    ],
)
def test_a_forged_cursor_is_refused(group: UsageGroup, cursor: str) -> None:
    with pytest.raises(InvalidInputError):
        llm_usage.decode_cursor(cursor, group)


# ── Statements: tenant filter, fixed query count ───────────────────────────
@pytest.mark.parametrize("group", list(UsageGroup))
def test_every_query_is_tenant_filtered_and_bounded(group: UsageGroup) -> None:
    db = RecordingSession()
    day = date(2026, 1, 1)
    result = llm_usage.summary(db, TENANT, day, day, group=group)  # type: ignore[arg-type]
    assert result.lines == []
    assert result.next_cursor is None
    # No rows: the page query and the totals query, nothing per row.
    assert len(db.statements) == 2
    for stmt in db.statements:
        compiled = stmt.compile(dialect=postgresql.dialect())
        sql = str(compiled)
        assert "tenant_id = " in sql
        assert TENANT in compiled.params.values()
        assert ">=" in sql and "<" in sql
    if group is UsageGroup.JOB:
        assert result.note == llm_usage.IMAGE_COST_NOTE
        # The encrypted brief is never loaded for a usage report.
        assert "brief_encrypted" not in str(db.statements[0].compile(dialect=postgresql.dialect()))


def test_day_buckets_group_by_literal_sql() -> None:
    db = RecordingSession()
    day = date(2026, 1, 1)
    llm_usage.summary(db, TENANT, day, day, group=UsageGroup.DAY)  # type: ignore[arg-type]
    sql = str(db.statements[0].compile(dialect=postgresql.dialect()))
    assert "date_trunc('day', timezone('UTC', agent_runs.started_at))" in sql
    assert sql.count("date_trunc('day'") == 3  # select, group by, order by


# ── Against Postgres ───────────────────────────────────────────────────────
def _run(tenant_id: str, at: datetime, **kw: Any) -> Any:  # noqa: ANN401
    from app.models.agent import AgentRun

    values: dict[str, Any] = {
        "agent_slug": "seo_optimizer",
        "status": "succeeded",
        "tokens_in": 0,
        "tokens_out": 0,
        "llm_connector": "",
        "llm_model": "",
        "detail": {},
    }
    values.update(kw)
    return AgentRun(tenant_id=tenant_id, started_at=at, **values)


@requires_db
def test_grouping_and_cross_tenant_isolation(
    clean_db, org, second_org, prices: None  # noqa: ANN001
) -> None:
    from app.db.session import bind_tenant

    day1 = datetime(2026, 3, 1, 10, tzinfo=UTC)
    day2 = datetime(2026, 3, 2, 10, tzinfo=UTC)
    bind_tenant(clean_db, org.id)
    clean_db.add_all(
        [
            _run(org.id, day1, llm_connector="anthropic", llm_model="claude-a",
                 tokens_in=1_000_000, tokens_out=0),
            _run(org.id, day2, llm_connector="anthropic", llm_model="claude-a",
                 tokens_in=0, tokens_out=1_000_000),
            _run(org.id, day2, agent_slug="content_writer", llm_connector="vllm",
                 llm_model="llama", tokens_in=100, tokens_out=100),
            # A self-hosted run that reported no usage.
            _run(org.id, day2, agent_slug="content_writer", llm_connector="vllm",
                 llm_model="llama", detail={llm_usage.TOKENS_UNKNOWN_DETAIL_KEY: True}),
            # From before models were recorded.
            _run(org.id, day1, agent_slug="legacy_agent", tokens_in=50, tokens_out=50),
        ]
    )
    clean_db.flush()
    bind_tenant(clean_db, second_org.id)
    clean_db.add(
        _run(second_org.id, day1, llm_connector="anthropic", llm_model="claude-a",
             tokens_in=9_000_000, tokens_out=9_000_000)
    )
    clean_db.flush()
    bind_tenant(clean_db, org.id)

    by_model = llm_usage.summary(clean_db, org.id, date(2026, 3, 1), date(2026, 3, 2), "model")
    lines = {line.key: line for line in by_model.lines}
    claude = lines["anthropic:claude-a"]
    assert (claude.runs, claude.tokens_in, claude.tokens_out) == (2, 1_000_000, 1_000_000)
    assert claude.cost == pytest.approx(3.0 + 15.0)
    assert claude.cost_status is CostStatus.ESTIMATE
    llama = lines["vllm:llama"]
    assert llama.cost is None and llama.cost_label == "No price set"
    assert llama.unknown_token_runs == 1 and llama.tokens_in == 100
    assert lines[":"].label == llm_usage.MODEL_NOT_RECORDED
    # The other workspace's 9M tokens are nowhere.
    assert by_model.totals.runs == 5
    assert by_model.totals.tokens_in == 1_000_150
    assert by_model.totals.cost_status is CostStatus.PARTIAL

    by_day = llm_usage.summary(clean_db, org.id, date(2026, 3, 1), date(2026, 3, 2), "day")
    assert [line.key for line in by_day.lines] == ["2026-03-01", "2026-03-02"]
    assert by_day.lines[0].runs == 2

    page = llm_usage.summary(
        clean_db, org.id, date(2026, 3, 1), date(2026, 3, 2), "agent", limit=1
    )
    assert len(page.lines) == 1 and page.next_cursor
    rest = llm_usage.summary(
        clean_db, org.id, date(2026, 3, 1), date(2026, 3, 2), "agent",
        cursor=page.next_cursor, limit=10,
    )
    keys = [page.lines[0].key, *(line.key for line in rest.lines)]
    assert keys == sorted({"seo_optimizer", "content_writer", "legacy_agent"})

    # Asked for its own tenant, the second workspace sees only its own run.
    bind_tenant(clean_db, second_org.id)
    theirs = llm_usage.summary(
        clean_db, second_org.id, date(2026, 3, 1), date(2026, 3, 2), "provider"
    )
    assert theirs.totals.runs == 1
    assert theirs.totals.tokens_in == 9_000_000


@requires_db
def test_job_usage_is_per_job_and_per_tenant(
    clean_db, org, second_org, prices: None  # noqa: ANN001
) -> None:
    from app.db.session import bind_tenant
    from app.models.creative import CreativeJob

    at = datetime(2026, 4, 1, 9, tzinfo=UTC)
    bind_tenant(clean_db, org.id)
    clean_db.add_all(
        [
            CreativeJob(tenant_id=org.id, created_at=at, text_connector="anthropic",
                        text_model="claude-a", tokens_in=1_000_000, tokens_out=0,
                        images_generated=2),
            CreativeJob(tenant_id=org.id, created_at=at + timedelta(hours=1),
                        text_connector="vllm", text_model="llama", tokens_in=10,
                        tokens_out=10, images_generated=1),
        ]
    )
    clean_db.flush()
    bind_tenant(clean_db, second_org.id)
    clean_db.add(
        CreativeJob(tenant_id=second_org.id, created_at=at, text_connector="anthropic",
                    text_model="claude-a", tokens_in=5, tokens_out=5, images_generated=9)
    )
    clean_db.flush()
    bind_tenant(clean_db, org.id)

    result = llm_usage.summary(clean_db, org.id, date(2026, 4, 1), date(2026, 4, 1), "job")
    assert len(result.lines) == 2
    newest, oldest = result.lines
    assert newest.text_model == "llama" and newest.cost is None
    assert oldest.cost == pytest.approx(3.0) and oldest.images_generated == 2
    assert result.totals.images_generated == 3
    assert result.totals.runs == 2
