"""Live checks (``verify_steps``) for the CRM and AI-model connectors.

Salesforce, HubSpot, OpenAI, Perplexity, Google Gemini and Anthropic Claude.
Every step here is a read: the CRMs are only ever read by this platform, and
the AI vendors get a model-list read plus one tiny completion.

The completion goes through the provider stack agents write with
(``app.llm.from_connector``), which keeps its own HTTP client; ``_llm_via``
points that client at the same recording fake vendor as the connector's.
"""
from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from app.connectors.base import registry
from app.connectors.base import verify_helpers as vh
from app.connectors.base.credentials import Credentials
from app.core.exceptions import ConnectorConfigError, ConnectorError
from app.llm.http_provider import HttpLLMProvider
from tests.support.verify_http import Recorder, assert_no_secrets, install, run_all


def _connector(slug: str, values: dict):  # noqa: ANN202
    return registry.get_class(slug)(Credentials(values=values, oauth_completed=True))


def _llm_via(monkeypatch: pytest.MonkeyPatch, rec: Recorder) -> None:
    """Send the model provider's calls to the recorder too."""

    def _http(self):  # noqa: ANN001, ANN202
        if self._client is None:
            self._client = httpx.Client(base_url=self.base_url, transport=httpx.MockTransport(rec))
        return self._client

    monkeypatch.setattr(HttpLLMProvider, "_http", _http)


def _query(request: httpx.Request) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(urlsplit(str(request.url)).query).items()}


# ── Salesforce ─────────────────────────────────────────────────────────────
SF = {"instanceUrl": "https://acme.my.salesforce.com", "accessToken": "00Dsecret!tokenvalue"}


def _sf_routes(task_rows: object = None) -> list:
    def query(request: httpx.Request):  # noqa: ANN202
        soql = _query(request)["q"]
        if "FROM Opportunity" in soql:
            return {"totalSize": 4, "done": True, "records": []}
        if task_rows is not None:
            return task_rows
        return {"totalSize": 2, "done": True, "records": [
            {"WhoId": "003A", "Type": "Call", "total": 3},
            {"WhoId": "003B", "Type": "Email", "total": 1},
        ]}

    return [
        ("GET", r"/services/data/v62\.0/limits$", {"DailyApiRequests": {"Max": 15000, "Remaining": 14990}}),
        ("GET", r"/services/data/v62\.0/query\?", query),
    ]


def test_salesforce_reads_limits_won_count_and_capped_signals():
    sf = _connector("salesforce", SF)
    rec = install(sf, _sf_routes())
    results = run_all(sf)
    assert list(results) == ["org_limits", "won_opportunities", "activity_totals"]
    assert all(s.kind == "read" for s in sf.verify_steps())
    assert all(c.url.host == "acme.my.salesforce.com" for c in rec.calls)
    assert all(c.method == "GET" for c in rec.calls)

    soqls = [_query(c)["q"] for c in rec.find("GET", r"/query\?")]
    assert soqls[0].startswith("SELECT COUNT() FROM Opportunity WHERE IsWon = true AND CloseDate >= ")
    assert "FROM Task" in soqls[1] and soqls[1].endswith("GROUP BY WhoId, Type LIMIT 5")
    assert results["won_opportunities"][1] == {"won_opportunities_30d": 4}
    assert results["activity_totals"][1] == {"rows": 2}
    assert results["org_limits"][1]["daily_api_remaining"] == 14990
    # No contact ids leave in the report.
    assert "003A" not in str(results)
    assert_no_secrets(sf, results)


def test_salesforce_signals_step_fails_loudly_when_the_query_is_refused():
    # read_first_party_signals swallows a refused query (and returns []); the
    # live check must not, or a missing permission would pass.
    sf = _connector("salesforce", SF)
    install(sf, _sf_routes(task_rows=(400, [{"errorCode": "INVALID_TYPE", "message": "no Task"}])))
    step = {s.key: s for s in sf.verify_steps()}["activity_totals"]
    with pytest.raises(ConnectorError):
        step.run()


def test_salesforce_signals_query_is_unchanged_for_the_agent():
    sf = _connector("salesforce", SF)
    rec = install(sf, _sf_routes())
    rows = sf.read_first_party_signals(limit=10)
    assert len(rows) == 2
    assert _query(rec.calls[0])["q"].endswith("GROUP BY WhoId, Type LIMIT 40")


@pytest.mark.parametrize(
    ("values", "expected"),
    [
        ({}, vh.LIVE),
        ({"loginUrl": "https://login.salesforce.com"}, vh.LIVE),
        # The sign-in's "This is a sandbox" toggle writes exactly this.
        ({"loginUrl": "https://test.salesforce.com"}, vh.SANDBOX),
        ({"loginUrl": "test.salesforce.com/"}, vh.SANDBOX),
        ({"loginUrl": "https://acme--uat.sandbox.my.salesforce.com"}, vh.SANDBOX),
        ({"instanceUrl": "https://acme--uat.sandbox.my.salesforce.com"}, vh.SANDBOX),
        ({"instanceUrl": "https://acme-dev-ed.develop.my.salesforce.com"}, vh.TEST_ACCOUNT),
        ({"instanceUrl": "https://acme.my.salesforce.com", "accountEnvironment": "Sandbox"}, vh.SANDBOX),
        ({"instanceUrl": "https://acme.my.salesforce.com", "accountEnvironment": "test"}, vh.TEST_ACCOUNT),
        # A look-alike host is not a sandbox.
        ({"loginUrl": "https://test.salesforce.com.evil.example"}, vh.LIVE),
    ],
)
def test_salesforce_environment_follows_the_sandbox_switch(values: dict, expected: str):
    assert _connector("salesforce", {"accessToken": "x", **values}).account_environment() == expected


def test_salesforce_sandbox_switch_still_points_sign_in_at_test_salesforce():
    sf = _connector("salesforce", {"loginUrl": "https://test.salesforce.com"})
    assert sf.token_url == "https://test.salesforce.com/services/oauth2/token"
    # No duplicate "Test account" field: the toggle is the switch.
    assert "accountEnvironment" not in {f.key for f in sf.spec.fields}


# ── HubSpot ────────────────────────────────────────────────────────────────
HS = {"accessToken": "pat-na1-secretvalue"}
PIPELINES = {"results": [
    {"id": "default", "label": "Sales", "stages": [
        {"id": "appointmentscheduled", "metadata": {"isClosed": "false", "probability": "0.2"}},
        {"id": "closedwon", "label": "Won", "metadata": {"isClosed": "true", "probability": "1.0"}},
        {"id": "closedlost", "metadata": {"isClosed": "true", "probability": "0.0"}},
    ]},
    {"id": "old", "archived": True, "stages": []},
]}


def test_hubspot_reads_pipelines_won_deals_and_one_contact():
    hs = _connector("hubspot", HS)
    rec = install(
        hs,
        [
            ("GET", r"/crm/v3/pipelines/deals$", PIPELINES),
            ("POST", r"/crm/v3/objects/deals/search$", {"total": 7, "results": [{"id": "1"}]}),
            ("GET", r"/crm/v3/objects/contacts\?", {"results": [{"id": "51", "properties": {}}]}),
        ],
    )
    results = run_all(hs)
    assert list(results) == ["deal_pipelines", "won_deals", "contact_signals"]
    assert all(s.kind == "read" for s in hs.verify_steps())
    assert all(c.url.host == "api.hubapi.com" for c in rec.calls)
    assert results["deal_pipelines"][1] == {"pipelines": 1, "won_stages": 1}
    assert results["won_deals"][1] == {"won_deals_30d": 7}

    search = rec.body("POST", r"/deals/search$")
    assert search["limit"] == 1
    stage, close = search["filterGroups"][0]["filters"]
    assert stage == {"propertyName": "dealstage", "operator": "EQ", "value": "closedwon"}
    assert close["propertyName"] == "closedate" and close["operator"] == "GTE"
    contacts = _query(rec.find("GET", r"/contacts\?")[0])
    assert contacts["limit"] == "1" and "num_conversion_events" in contacts["properties"]
    assert_no_secrets(hs, results)


def test_hubspot_without_any_won_stage_says_what_to_fill_in():
    hs = _connector("hubspot", HS)
    install(hs, [("GET", r"/crm/v3/pipelines/deals$", {"results": []})])
    with pytest.raises(ConnectorConfigError, match="Closed-won stage ID"):
        hs.verify_steps()[0].run()


def test_hubspot_test_account_switch():
    spec = registry.get_class("hubspot").spec
    keys = [f.key for f in spec.fields]
    # Added after the existing settings, before the pasted token; nothing reordered.
    assert keys == ["hubId", "wonStageId", "accountEnvironment", "accessToken"]
    field = next(f for f in spec.fields if f.key == "accountEnvironment")
    assert field.label == "Test account" and not field.required
    assert "developer test account" in field.help_text
    assert _connector("hubspot", HS).account_environment() == vh.LIVE
    test = _connector("hubspot", {**HS, "accountEnvironment": "Test account"})
    assert test.account_environment() == vh.TEST_ACCOUNT
    # HubSpot offers no sandbox here; a typed "sandbox" is read as live.
    assert _connector("hubspot", {**HS, "accountEnvironment": "sandbox"}).account_environment() == vh.LIVE
    # Same API host either way, and the sign-in form still hides the token.
    assert test.base_url == "https://api.hubapi.com"
    assert "accountEnvironment" not in spec.oauth.token_fields


# ── AI models ──────────────────────────────────────────────────────────────
def test_openai_lists_models_then_one_tiny_completion(monkeypatch: pytest.MonkeyPatch):
    ai = _connector("openai", {"apiKey": "sk-test-secret-123456789012345", "model": "gpt-x"})
    rec = install(
        ai,
        [
            ("GET", r"^https://api\.openai\.com/v1/models$", {"data": [{"id": "gpt-x", "created": 1}, {"id": "gpt-y"}]}),
            ("POST", r"^https://api\.openai\.com/v1/chat/completions$", {
                "model": "gpt-x-2026", "choices": [{"message": {"content": "OK"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 9, "completion_tokens": 1},
            }),
        ],
    )
    _llm_via(monkeypatch, rec)
    results = run_all(ai)
    assert list(results) == ["list_models", "tiny_completion"]
    assert all(s.kind == "read" for s in ai.verify_steps())
    assert results["list_models"][1] == {"models": 2, "model": "gpt-x"}
    body = rec.body("POST", r"/chat/completions$")
    assert body["model"] == "gpt-x" and body["max_completion_tokens"] == 16
    assert body["messages"] == [{"role": "user", "content": "Reply with OK"}]
    assert "tools" not in body  # no paid web search
    assert results["tiny_completion"][1] == {
        "model": "gpt-x-2026", "input_tokens": 9, "output_tokens": 1, "text_returned": True,
    }
    assert_no_secrets(ai, results)


def test_openai_model_gone_from_the_list_fails_before_any_completion():
    ai = _connector("openai", {"apiKey": "sk-test-secret-123456789012345", "model": "gpt-old", "imageModel": ""})
    rec = install(ai, [("GET", r"/v1/models$", {"data": [{"id": "gpt-new"}]})])
    with pytest.raises(ConnectorError, match="no longer offered"):
        ai.verify_steps()[0].run()
    assert not rec.find("POST", r".")


def test_a_reasoning_model_that_spends_the_tiny_cap_on_thinking_still_passes(monkeypatch: pytest.MonkeyPatch):
    ai = _connector("openai", {"apiKey": "sk-test-secret-123456789012345", "model": "o-x"})
    rec = install(ai, [
        ("POST", r"/chat/completions$", {"choices": [{"message": {"content": ""}, "finish_reason": "length"}]}),
    ])
    _llm_via(monkeypatch, rec)
    detail, evidence = {s.key: s for s in ai.verify_steps()}["tiny_completion"].run()
    assert evidence == {"model": "o-x", "text_returned": False}
    assert "thinking" in detail


def test_a_refused_completion_fails(monkeypatch: pytest.MonkeyPatch):
    ai = _connector("openai", {"apiKey": "sk-test-secret-123456789012345", "model": "gpt-x"})
    rec = install(ai, [("POST", r"/chat/completions$", (401, {"error": {"message": "bad key"}}))])
    _llm_via(monkeypatch, rec)
    with pytest.raises(ConnectorError):
        {s.key: s for s in ai.verify_steps()}["tiny_completion"].run()


def test_gemini_lists_models_then_one_tiny_generate_content(monkeypatch: pytest.MonkeyPatch):
    key = "AIzaSyTESTsecretkeyvalue0123456789abc"
    ai = _connector("google_gemini", {"apiKey": key, "model": "gemini-x", "imageModel": "gemini-x-image"})
    rec = install(
        ai,
        [
            ("GET", r"^https://generativelanguage\.googleapis\.com/v1beta/models\?pageSize=1000$", {"models": [
                {"name": "models/gemini-x", "supportedGenerationMethods": ["generateContent"]},
                {"name": "models/gemini-x-image", "supportedGenerationMethods": ["generateContent"]},
                {"name": "models/embed", "supportedGenerationMethods": ["embedContent"]},
            ]}),
            ("POST", r"^https://generativelanguage\.googleapis\.com/v1beta/models/gemini-x:generateContent", {
                "candidates": [{"content": {"parts": [{"text": "OK"}]}, "finishReason": "STOP"}],
                "usageMetadata": {"promptTokenCount": 4, "candidatesTokenCount": 1},
            }),
        ],
    )
    _llm_via(monkeypatch, rec)
    results = run_all(ai)
    assert results["list_models"][1] == {"models": 2, "model": "gemini-x", "image_model": "gemini-x-image"}
    body = rec.body("POST", r":generateContent")
    assert body["generationConfig"]["maxOutputTokens"] == 16
    assert "tools" not in body  # no grounded (search) call
    assert results["tiny_completion"][1]["text_returned"] is True
    assert_no_secrets(ai, results)


def test_gemini_blocked_prompt_is_a_failure_not_thinking(monkeypatch: pytest.MonkeyPatch):
    ai = _connector("google_gemini", {"apiKey": "AIzaSyTESTsecretkeyvalue0123456789abc", "model": "gemini-x"})
    rec = install(ai, [("POST", r":generateContent", {"promptFeedback": {"blockReason": "OTHER"}})])
    _llm_via(monkeypatch, rec)
    with pytest.raises(ConnectorError, match="blocked"):
        {s.key: s for s in ai.verify_steps()}["tiny_completion"].run()


def test_anthropic_lists_models_then_one_tiny_message(monkeypatch: pytest.MonkeyPatch):
    ai = _connector("anthropic_claude", {"apiKey": "sk-ant-verify-steps-secret-0001", "model": "claude-x"})
    rec = install(
        ai,
        [
            ("GET", r"^https://api\.anthropic\.com/v1/models\?limit=1000$", {
                "data": [{"id": "claude-x", "display_name": "Claude X", "created_at": "2026-01-01T00:00:00Z"}],
                "has_more": False,
            }),
            # The provider reads the model's capabilities before writing.
            ("GET", r"^https://api\.anthropic\.com/v1/models/claude-x$", {"id": "claude-x", "capabilities": {}}),
            ("POST", r"^https://api\.anthropic\.com/v1/messages$", {
                "model": "claude-x", "stop_reason": "end_turn",
                "content": [{"type": "text", "text": "OK"}],
                "usage": {"input_tokens": 10, "output_tokens": 2},
            }),
        ],
    )
    _llm_via(monkeypatch, rec)
    results = run_all(ai)
    assert list(results) == ["list_models", "tiny_completion"]
    body = rec.body("POST", r"/v1/messages$")
    assert body["max_tokens"] == 16 and body["model"] == "claude-x"
    assert "tools" not in body  # no paid web search
    assert results["tiny_completion"][1]["output_tokens"] == 2
    assert_no_secrets(ai, results)


def test_perplexity_has_no_model_list_and_one_tiny_completion(monkeypatch: pytest.MonkeyPatch):
    ai = _connector("perplexity", {"apiKey": "pplx-verify-steps-secret-000001", "model": "sonar"})
    rec = install(ai, [
        ("POST", r"^https://api\.perplexity\.ai/chat/completions$", {
            "model": "sonar", "choices": [{"message": {"content": "OK"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 1},
        }),
    ])
    _llm_via(monkeypatch, rec)
    steps = ai.verify_steps()
    assert [s.key for s in steps] == ["tiny_completion"]
    assert "cent" in steps[0].label
    results = run_all(ai)
    body = rec.body("POST", r"/chat/completions$")
    assert body["max_tokens"] == 16 and body["model"] == "sonar"
    assert len(rec.calls) == 1
    assert_no_secrets(ai, results)


@pytest.mark.parametrize("slug", ["openai", "google_gemini", "anthropic_claude", "perplexity"])
def test_ai_steps_need_a_model_first(slug: str):
    ai = _connector(slug, {"apiKey": "k-verify-steps-secret"})
    for step in ai.verify_steps():
        with pytest.raises(ConnectorConfigError):
            step.run()


@pytest.mark.parametrize(
    "slug", ["salesforce", "hubspot", "openai", "google_gemini", "anthropic_claude", "perplexity"]
)
def test_building_the_steps_makes_no_calls(slug: str):
    c = _connector(slug, {**SF, **HS, "apiKey": "k-verify", "model": "m"})
    rec = install(c, [])
    assert c.verify_steps()
    assert rec.calls == []
