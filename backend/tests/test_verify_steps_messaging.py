"""Live checks (``verify_steps``) for the messaging group.

Mailchimp, Brevo, Klaviyo, Slack and Email / SMTP, each run against a
recording fake vendor (tests/support/verify_http), or a fake SMTP server.
"""
from __future__ import annotations

import pytest

from app.connectors.base import registry
from app.connectors.base import verify_helpers as vh
from app.connectors.base.credentials import Credentials
from app.core.exceptions import ConnectorConfigError, ConnectorError
from tests.support.verify_http import assert_no_secrets, install, run_all


def _connector(slug: str, values: dict):  # noqa: ANN202
    return registry.get_class(slug)(Credentials(values=values, oauth_completed=True))


def _steps(connector) -> dict:  # noqa: ANN001
    return {s.key: s for s in connector.verify_steps()}


# ── Mailchimp ──────────────────────────────────────────────────────────────
MC = {"apiKey": "0123456789abcdef" + "0123456789abcdef-us21", "audienceId": "aud123"}


def _mailchimp_routes(status: str = "save") -> list:
    return [
        ("GET", r"us21\.api\.mailchimp\.com/3\.0/ping$", {"health_status": "Everything's Chimpy!"}),
        (
            "GET",
            r"/3\.0/lists/aud123\?fields=id%2Cname%2Cstats",
            {"id": "aud123", "name": "Test", "stats": {"member_count": 3}},
        ),
        (
            "GET",
            r"/3\.0/lists/aud123\?fields=campaign_defaults",
            {"campaign_defaults": {"from_name": "Brand", "from_email": "hi@example.com"}},
        ),
        ("POST", r"/3\.0/campaigns$", {"id": "c1", "web_id": 9}),
        ("PUT", r"/3\.0/campaigns/c1/content$", {}),
        ("GET", r"/3\.0/campaigns/c1\?", {"id": "c1", "status": status}),
        ("DELETE", r"/3\.0/campaigns/c1$", (204, None)),
    ]


def test_mailchimp_reads_and_round_trips_a_draft():
    mc = _connector("mailchimp", MC)
    rec = install(mc, _mailchimp_routes())
    results = run_all(mc)
    assert set(results) == {"ping", "read_audience", "draft_campaign_roundtrip"}
    assert results["read_audience"][1] == {"audience_id": "aud123", "member_count": 3}
    body = rec.body("POST", r"/campaigns$")
    assert body["type"] == "regular" and body["recipients"] == {"list_id": "aud123"}
    assert body["settings"]["title"].startswith(vh.VERIFY_LABEL)
    assert rec.index("POST", r"/campaigns$") < rec.index("PUT", r"/content$") < rec.index(
        "DELETE", r"/campaigns/c1$"
    )
    assert not rec.find("POST", r"/actions/(send|schedule|test)")
    assert results["draft_campaign_roundtrip"][1] == {"created_id": "c1", "deleted": True}
    assert_no_secrets(mc, results)


def test_mailchimp_draft_is_deleted_even_when_it_is_not_a_draft():
    mc = _connector("mailchimp", MC)
    rec = install(mc, _mailchimp_routes(status="sent"))
    with pytest.raises(ConnectorError, match="reading it back failed"):
        _steps(mc)["draft_campaign_roundtrip"].run()
    assert rec.index("GET", r"/campaigns/c1\?") < rec.index("DELETE", r"/campaigns/c1$")


def test_mailchimp_draft_step_needs_no_test_account():
    step = _steps(_connector("mailchimp", MC))["draft_campaign_roundtrip"]
    assert step.kind == "write" and not step.requires_test_account


# ── Brevo ──────────────────────────────────────────────────────────────────
BREVO = {"apiKey": "xkeysib-abcdefghijklmnopqrstuvwxyz", "senderEmail": "Hi@Example.com", "listIds": "2, 7"}


def _brevo_routes(*, status: str = "draft", active: bool = True) -> list:
    return [
        ("GET", r"api\.brevo\.com/v3/account$", {"email": "owner@example.com", "plan": [{"type": "free"}]}),
        ("GET", r"/v3/senders$", {"senders": [{"id": 4, "email": "hi@example.com", "active": active}]}),
        ("GET", r"/v3/contacts/lists/(2|7)$", {"id": 2, "totalSubscribers": 5}),
        ("POST", r"/v3/emailCampaigns$", {"id": 31}),
        ("GET", r"/v3/emailCampaigns/31\?excludeHtmlContent=true", {"id": 31, "status": status}),
        ("DELETE", r"/v3/emailCampaigns/31$", (204, None)),
    ]


def test_brevo_reads_and_round_trips_a_draft():
    brevo = _connector("brevo", BREVO)
    rec = install(brevo, _brevo_routes())
    results = run_all(brevo)
    assert set(results) == {"account", "sender", "contact_lists", "draft_campaign_roundtrip"}
    assert results["account"][1] == {"plan_types": ["free"]}
    assert results["sender"][1] == {"sender_id": 4}
    assert results["contact_lists"][1] == {"list_ids": [2, 7], "subscribers": 10}
    body = rec.body("POST", r"/emailCampaigns$")
    assert body["name"].startswith(vh.VERIFY_LABEL)
    assert "scheduledAt" not in body and body["recipients"] == {"listIds": [2, 7]}
    assert rec.index("POST", r"/emailCampaigns$") < rec.index("DELETE", r"/emailCampaigns/31$")
    assert not rec.find("POST", r"/sendNow|/sendTest")
    assert results["draft_campaign_roundtrip"][1] == {"created_id": "31", "deleted": True}
    assert_no_secrets(brevo, results)
    # No email address in the evidence.
    assert "example.com" not in str([e for _, e in results.values()])


def test_brevo_draft_is_deleted_even_when_it_comes_back_scheduled():
    brevo = _connector("brevo", BREVO)
    rec = install(brevo, _brevo_routes(status="queued"))
    with pytest.raises(ConnectorError):
        _steps(brevo)["draft_campaign_roundtrip"].run()
    assert rec.find("DELETE", r"/emailCampaigns/31$")


def test_brevo_inactive_sender_fails_the_read():
    brevo = _connector("brevo", BREVO)
    install(brevo, _brevo_routes(active=False))
    with pytest.raises(ConnectorError, match="not yet verified"):
        _steps(brevo)["sender"].run()


def test_brevo_without_lists_reads_none():
    brevo = _connector("brevo", {**BREVO, "listIds": ""})
    rec = install(brevo, [])
    detail, evidence = _steps(brevo)["contact_lists"].run()
    assert evidence == {"list_ids": []} and not rec.calls


# ── Klaviyo ────────────────────────────────────────────────────────────────
KLAVIYO = {"apiKey": "pk_abcdefghijklmnopqrstuvwxyz"}


def _klaviyo_routes(read_back) -> list:  # noqa: ANN001
    return [
        ("GET", r"a\.klaviyo\.com/api/accounts$", {"data": [{"id": "ACC1", "attributes": {}}]}),
        ("POST", r"/api/templates$", {"data": {"id": "T1"}}),
        ("GET", r"/api/templates/T1$", read_back),
        ("DELETE", r"/api/templates/T1$", (204, None)),
    ]


def test_klaviyo_reads_and_round_trips_a_template():
    import json

    kl = _connector("klaviyo", KLAVIYO)
    names: list[str] = []

    def created(request):  # noqa: ANN001, ANN202
        names.append(json.loads(request.content)["data"]["attributes"]["name"])
        return {"data": {"id": "T1"}}

    def read_back(request):  # noqa: ANN001, ANN202
        return {"data": {"id": "T1", "attributes": {"name": names[0], "editor_type": "CODE"}}}

    routes = _klaviyo_routes(read_back)
    routes[1] = ("POST", r"/api/templates$", created)
    rec = install(kl, routes)
    steps = _steps(kl)
    results = run_all(kl)
    assert results["account"][1] == {"account_id": "ACC1"}
    body = rec.body("POST", r"/api/templates$")["data"]
    assert body["type"] == "template" and body["attributes"]["editor_type"] == "CODE"
    assert names[0].startswith(vh.VERIFY_LABEL)
    assert all(c.headers["revision"] for c in rec.calls)
    assert rec.index("POST", r"/api/templates$") < rec.index("DELETE", r"/api/templates/T1$")
    assert not rec.find("POST", r"/api/campaign")
    assert results["template_roundtrip"][1] == {"created_id": "T1", "deleted": True}
    assert not steps["template_roundtrip"].requires_test_account
    assert_no_secrets(kl, results)


def test_klaviyo_template_is_deleted_when_it_cannot_be_read_back():
    kl = _connector("klaviyo", KLAVIYO)
    rec = install(kl, _klaviyo_routes((403, {"errors": []})))
    with pytest.raises(ConnectorError, match="read access to Templates"):
        _steps(kl)["template_roundtrip"].run()
    assert rec.index("GET", r"/api/templates/T1$") < rec.index("DELETE", r"/api/templates/T1$")


# ── Slack ──────────────────────────────────────────────────────────────────
SLACK = {"accessToken": "xoxb-test-token-value", "defaultChannel": "C123"}


def _slack_routes(delete_ok: bool = True) -> list:
    return [
        ("POST", r"slack\.com/api/auth\.test$", {"ok": True, "team": "T", "team_id": "T1", "bot_id": "B1"}),
        ("GET", r"/api/conversations\.info\?channel=C123$", {"ok": True, "channel": {"id": "C123"}}),
        ("POST", r"/api/chat\.postMessage$", {"ok": True, "channel": "C123", "ts": "1700000000.000100"}),
        ("POST", r"/api/chat\.delete$", {"ok": delete_ok, **({} if delete_ok else {"error": "cant_delete_message"})}),
    ]


def test_slack_reads_only_on_a_live_workspace():
    slack = _connector("slack", SLACK)
    rec = install(slack, _slack_routes())
    results = run_all(slack, kinds=("read",))
    assert results["auth_test"][1] == {"team_id": "T1", "bot_id": "B1"}
    assert results["read_channel"][1] == {"channel_id": "C123"}
    step = _steps(slack)["post_and_delete_message"]
    assert step.requires_test_account
    with pytest.raises(ConnectorConfigError):
        step.run()
    assert not rec.find("POST", r"chat\.postMessage")
    assert_no_secrets(slack, results)


def test_slack_posts_and_deletes_on_a_test_workspace():
    slack = _connector("slack", {**SLACK, "accountEnvironment": "Test account"})
    assert slack.account_environment() == vh.TEST_ACCOUNT
    rec = install(slack, _slack_routes())
    results = run_all(slack)
    posted = rec.body("POST", r"chat\.postMessage$")
    assert posted["channel"] == "C123"
    assert posted["text"] == "Synaprank.ai verification ping — safe to ignore"
    assert rec.body("POST", r"chat\.delete$") == {"channel": "C123", "ts": "1700000000.000100"}
    assert rec.index("POST", r"chat\.postMessage$") < rec.index("POST", r"chat\.delete$")
    assert results["post_and_delete_message"][1]["deleted"] is True
    assert_no_secrets(slack, results)


def test_slack_names_the_message_it_could_not_delete():
    slack = _connector("slack", {**SLACK, "accountEnvironment": "test"})
    install(slack, _slack_routes(delete_ok=False))
    with pytest.raises(ConnectorError, match="Delete it by hand"):
        _steps(slack)["post_and_delete_message"].run()


def test_slack_channel_typed_by_name_is_looked_up():
    slack = _connector("slack", {**SLACK, "defaultChannel": "#marketing"})
    rec = install(
        slack,
        [
            ("GET", r"/api/conversations\.list", {"ok": True, "channels": [{"id": "C9", "name": "marketing"}]}),
            ("GET", r"/api/conversations\.info\?channel=C9$", {"ok": True, "channel": {"id": "C9"}}),
        ],
    )
    assert _steps(slack)["read_channel"].run()[1] == {"channel_id": "C9"}
    assert rec.find("GET", r"conversations\.info")


def test_slack_test_account_field_is_optional_and_last():
    fields = registry.get_class("slack").spec.fields
    assert fields[-1].key == "accountEnvironment" and not fields[-1].required
    assert fields[0].key == "defaultChannel"


# ── Email / SMTP ───────────────────────────────────────────────────────────
SMTP = {
    "host": "smtp.example.com",
    "port": "587",
    "user": "bot@example.com",
    "password": "hunter2-secret",
    "notifyTo": "team@example.com",
}


class _FakeSMTP:
    log: list[str] = []

    def __init__(self, host, port, context=None, timeout=None):  # noqa: ANN001
        self.log.append(f"connect {host}:{port}{' tls' if context else ''}")

    def __enter__(self):  # noqa: ANN204
        return self

    def __exit__(self, *exc):  # noqa: ANN002, ANN204
        self.log.append("quit")
        return False

    def starttls(self, context=None):  # noqa: ANN001, ANN201
        self.log.append("starttls")

    def login(self, user, password):  # noqa: ANN001, ANN201
        self.log.append("login")

    def noop(self):  # noqa: ANN201
        self.log.append("noop")
        return 250, b"OK"

    def send_message(self, *a, **k):  # noqa: ANN002, ANN003, ANN201
        self.log.append("SEND")


@pytest.fixture
def fake_smtp(monkeypatch):  # noqa: ANN001, ANN201
    from app.connectors.smtp_email import connector as module

    _FakeSMTP.log = []
    monkeypatch.setattr(module.smtplib, "SMTP", _FakeSMTP)
    monkeypatch.setattr(module.smtplib, "SMTP_SSL", _FakeSMTP)
    return _FakeSMTP


def test_smtp_signs_in_with_starttls_and_never_sends(fake_smtp):  # noqa: ANN001
    smtp = _connector("smtp_email", SMTP)
    steps = smtp.verify_steps()
    assert [s.kind for s in steps] == ["read"]
    results = run_all(smtp)
    assert fake_smtp.log == ["connect smtp.example.com:587", "starttls", "login", "noop", "quit"]
    assert results["smtp_sign_in"][1] == {"port": 587, "tls": "STARTTLS", "noop_code": 250}
    assert_no_secrets(smtp, results)


def test_smtp_uses_implicit_tls_on_465(fake_smtp):  # noqa: ANN001
    smtp = _connector("smtp_email", {**SMTP, "port": "465"})
    run_all(smtp)
    assert fake_smtp.log == ["connect smtp.example.com:465 tls", "login", "noop", "quit"]


def test_smtp_send_shares_the_same_session(fake_smtp):  # noqa: ANN001
    smtp = _connector("smtp_email", SMTP)
    assert smtp.notify(subject="Hi", message="Body")
    assert fake_smtp.log == ["connect smtp.example.com:587", "starttls", "login", "SEND", "quit"]


def test_smtp_rejected_login_is_reported_plainly(monkeypatch, fake_smtp):  # noqa: ANN001
    import smtplib

    def refuse(self, user, password):  # noqa: ANN001, ANN202
        raise smtplib.SMTPAuthenticationError(535, b"bad")

    monkeypatch.setattr(_FakeSMTP, "login", refuse)
    with pytest.raises(ConnectorError, match="rejected those credentials"):
        _steps(_connector("smtp_email", SMTP))["smtp_sign_in"].run()
