"""Email Marketer agent and the Mailchimp, Brevo and Klaviyo connectors."""
from __future__ import annotations

import base64
import email
import json
from datetime import date
from email import policy
from types import SimpleNamespace

import httpx
import pytest

from app.agents.email_marketer import GUIDE, RESULTS, publishing, render
from app.agents.email_marketer import agent as agent_module
from app.agents.email_marketer.agent import AGENT, WELCOME_HASH_SETTING, parse_programs, period_key
from app.connectors.base import registry
from app.connectors.base.connector import Capability
from app.connectors.base.credentials import Credentials
from app.connectors.brevo.connector import BrevoConnector
from app.connectors.klaviyo.connector import DEFAULT_REVISION, KlaviyoConnector
from app.connectors.mailchimp.connector import MailchimpConnector
from app.core.exceptions import ConflictError, ConnectorConfigError, ConnectorError, InvalidInputError
from app.models.content import AssetStatus, MarketingAsset
from app.services import assets

SITE = "https://geet.example"
PROFILE = {
    "business_name": "Geet Salon",
    "summary": "Haircuts and colour in Noida.",
    "industry": "hair salon",
    "language": "English",
    "products_services": ["Haircut", "Hair colour"],
    "value_propositions": ["Family run since 2015"],
    "call_to_action": "Book an appointment",
    "source_url": SITE,
    "pages_read": [SITE, f"{SITE}/services"],
}


def _answer(**overrides) -> dict:
    data = {
        "subject": "Welcome to Geet Salon",
        "preview_text": "Here is what to expect from us, and how to book your first visit.",
        "headline": "Thanks for joining us",
        "paragraphs": [
            "We are a family run salon in Noida, cutting and colouring hair since 2015.",
            "Every few days we will share ideas for your next look and what is new at the salon. "
            "When you are ready, booking takes a minute.",
        ],
        "cta_label": "Book an appointment",
        "cta_url": f"{SITE}/services",
        "segment_suggestion": "Subscribers who joined in the last 7 days",
        "image_prompt": "A bright salon interior",
        "image_alt": "A bright, modern salon with styling chairs",
    }
    data.update(overrides)
    return data


# ── Fakes ────────────────────────────────────────────────────────────────────
class _Db:
    def __init__(self) -> None:
        self.added: list = []

    def add(self, row) -> None:  # noqa: ANN001
        if getattr(row, "id", None) is None:
            row.id = f"asset{len(self.added)}"
        self.added.append(row)

    def flush(self) -> None:
        pass


class _Cipher:
    def encrypt(self, value, *, context):  # noqa: ANN001
        return value

    def decrypt(self, value, *, context, default=""):  # noqa: ANN001
        return value


def _ctx(*, answers=None, settings=None, profile=None, platform=None, remaining=10):  # noqa: ANN001, ANN202
    answers = answers if callable(answers) else (lambda prompt, _list=list(answers or []): _list.pop(0))
    record = SimpleNamespace(settings=dict(settings or {}))
    prompts_seen: list[str] = []

    def ask_json(prompt, **_):  # noqa: ANN001, ANN202
        prompts_seen.append(prompt)
        return answers(prompt)

    def set_setting(key, value):  # noqa: ANN001, ANN202
        record.settings = {**record.settings, key: value}

    ctx = SimpleNamespace(
        db=_Db(),
        tenant_id="t",
        run_id="run1",
        cipher=_Cipher(),
        record=record,
        org=SimpleNamespace(name="Geet", primary_domain="geet.example"),
        brand_profile=PROFILE if profile is None else profile,
        brand_brief="BUSINESS: Geet Salon\nMAIN CALL TO ACTION: Book an appointment",
        today=date(2026, 10, 5),
        remaining_actions=remaining,
        setting=lambda key, default=None: record.settings.get(key, default),
        set_setting=set_setting,
        all_with_capability=lambda capability: [platform] if platform is not None else [],
        candidates=lambda capability: [platform] if platform is not None else [],
        with_capability=lambda capability: None,
        ask_json=ask_json,
        progress=lambda *a, **k: None,
        log_warning=lambda *a, **k: None,
        prompts_seen=prompts_seen,
    )
    return ctx


@pytest.fixture
def library(monkeypatch):
    """No stored emails and one recent article, unless a test says otherwise."""
    state = {
        "existing": set(),
        "articles": [{"title": "Five easy summer hairstyles", "summary": "Quick looks for hot days.",
                      "url": f"{SITE}/blog/summer-hair"}],
    }
    monkeypatch.setattr(agent_module, "_existing_keys", lambda ctx: state["existing"])
    monkeypatch.setattr(agent_module, "_recent_articles", lambda ctx, days: state["articles"])
    return state


def _emails(ctx) -> list[MarketingAsset]:  # noqa: ANN001
    return [row for row in ctx.db.added if isinstance(row, MarketingAsset)]


# ── Planning ─────────────────────────────────────────────────────────────────
def test_programs_and_periods():
    assert parse_programs("") == (["welcome", "newsletter"], [])
    assert parse_programs("Promo, win-back, bogus") == (["winback", "promo"], ["bogus"])
    assert period_key("newsletter", date(2026, 10, 5), "weekly") == "2026-W41"
    assert period_key("newsletter", date(2026, 10, 5), "monthly") == "2026-10"
    assert period_key("winback", date(2026, 10, 5), "weekly") == "2026-10"


def test_welcome_series_is_written_once_and_again_only_when_the_profile_changes(library):
    ctx = _ctx(answers=lambda prompt: _answer())
    result = AGENT.run(ctx)
    welcome = [a for a in _emails(ctx) if a.detail["program"] == "welcome"]
    assert [a.detail["send_day"] for a in welcome] == [0, 2, 5, 9]
    assert [a.detail["position"] for a in welcome] == [1, 2, 3, 4]
    assert all(a.kind == "email" and a.channel == "export" and a.detail["export_only"] for a in welcome)
    assert len(result.actions) == 5  # four welcome emails and this week's newsletter
    assert all(action.impact.value == "high" and action.target_kind == assets.TARGET_KIND for action in result.actions)
    assert result.actions[0].payload["sequence"] == "Welcome series"
    assert {"asset_id", "subject", "preview_text"} <= set(result.actions[0].payload)
    assert not any(action.metrics for action in result.actions)
    assert "ready to download" in result.summary
    assert all("BUSINESS: Geet Salon" in prompt for prompt in ctx.prompts_seen)
    newsletter = next(a for a in _emails(ctx) if a.detail["program"] == "newsletter")
    assert newsletter.detail["period"] == "2026-W41" and newsletter.detail["articles"] == 1
    assert "Five easy summer hairstyles" in ctx.prompts_seen[-1]

    # Same profile, newsletter already written this week: nothing to do.
    library["existing"] = {("newsletter", "2026-W41")}
    again = _ctx(answers=lambda prompt: _answer(), settings=ctx.record.settings)
    assert AGENT.run(again).skipped and _emails(again) == []

    # The profile changed: the series is written again.
    changed = _ctx(answers=lambda prompt: _answer(), settings=ctx.record.settings,
                   profile={**PROFILE, "summary": "Haircuts, colour and spa in Noida."})
    assert len(AGENT.run(changed).actions) == 4
    assert changed.record.settings[WELCOME_HASH_SETTING] != ctx.record.settings[WELCOME_HASH_SETTING]


def test_a_newsletter_needs_articles_and_a_welcome_series_needs_room_for_four(library):
    library["articles"] = []
    result = AGENT.run(_ctx(answers=lambda prompt: _answer(), remaining=3))
    assert result.skipped
    assert "needs 4 approvals" in result.summary and "no articles" in result.summary


def test_invalid_output_is_retried_once_then_discarded(library):
    answers = [_answer(subject="AMAZING NEWS FOR YOU")] + [_answer()] * 4 + [_answer(cta_url="https://evil.example")] * 2
    ctx = _ctx(answers=answers)
    result = AGENT.run(ctx)
    assert len(result.actions) == 4  # welcome survived its retry; the newsletter did not
    assert "Your previous answer could not be used because the subject uses ALL CAPS" in ctx.prompts_seen[1]
    assert "newsletter email was not written" in result.summary


def test_a_failed_welcome_email_writes_none_of_the_series(library):
    library["articles"] = []
    answers = [_answer()] * 2 + [_answer(subject="")] * 2
    ctx = _ctx(answers=answers, settings={"programs": "welcome"})
    result = AGENT.run(ctx)
    assert result.skipped and _emails(ctx) == []
    assert WELCOME_HASH_SETTING not in ctx.record.settings
    assert "email 3" in result.summary


def test_no_brand_profile_or_website_is_a_skip(library):
    assert AGENT.run(_ctx(profile={})).skipped
    ctx = _ctx(profile={"business_name": "X"})
    ctx.org.primary_domain = ""
    assert "no website address" in AGENT.run(ctx).summary


def test_from_name_override_and_platform_channel(library):
    library["articles"] = []
    platform = SimpleNamespace(slug="mailchimp")
    ctx = _ctx(answers=lambda p: _answer(), platform=platform, settings={"programs": "promo", "from_name": "Geet Team"})
    result = AGENT.run(ctx)
    (asset,) = _emails(ctx)
    content = assets.content_of(asset, _Cipher())
    assert asset.channel == "mailchimp" and "export_only" not in asset.detail
    assert content["from_name"] == "Geet Team"
    assert "*|UNSUB|*" in content["html"]
    assert "draft campaign in Mailchimp" in result.summary and "nothing is sent" in result.summary


# ── Validation and sanitizing ────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"subject": "x" * 61}, "subject is 61"),
        ({"subject": "Big news!!!"}, "exclamation"),
        ({"subject": "HUGE SALE TODAY"}, "ALL CAPS"),
        ({"subject": "Free money for our readers"}, "spam"),
        ({"preview_text": "p" * 111}, "preview text is 111"),
        ({"cta_url": "https://elsewhere.example"}, "listed pages"),
        ({"paragraphs": ["Get 20% off every haircut this week, the best deal around for you and friends."
                         " Book soon to make sure you get a chair at the salon."]}, "20%"),
        ({"paragraphs": ["Use the coupon at the desk when you come in, for all hair services this month,"
                         " and tell your friends about the salon too."]}, "offer"),
        ({"headline": "Hello [First name]"}, "placeholder"),
        ({"headline": "<b>Hi</b>"}, "HTML"),
        ({"paragraphs": ["Read more at https://geet.example/blog today, where we post styling tips and"
                         " news from the salon every single week."]}, "web address"),
    ],
)
def test_validation_rejects(overrides, reason):
    _clean, problems = render.validate(_answer(**overrides), allowed_urls=[SITE, f"{SITE}/services"], facts=json.dumps(PROFILE))
    assert any(reason in problem for problem in problems), problems


def test_a_valid_email_passes_and_offers_in_the_profile_are_allowed():
    clean, problems = render.validate(_answer(), allowed_urls=[f"{SITE}/services/"], facts="")
    assert problems == [] and clean["cta_url"] == f"{SITE}/services"
    offer = _answer(paragraphs=["New clients get 20% off their first haircut, as always at the salon in Noida, "
                                "where we have been cutting and colouring hair for many happy years, and we would love to see you soon."])
    assert render.validate(offer, allowed_urls=[f"{SITE}/services"], facts="New clients get 20% off")[1] == []
    _clean, problems = render.validate(_answer(image_alt=""), allowed_urls=[f"{SITE}/services"], facts="", needs_alt=True)
    assert any("alt text" in p for p in problems)


def test_sanitizer_removes_scripts_forms_and_handlers_but_keeps_merge_tags():
    dirty = (
        '<!DOCTYPE html><html><body><script>alert(1)</script><form action="x"><input name="a"></form>'
        '<a href="javascript:alert(1)" onclick="x()">bad</a><img src="https://ok.example/a.png" onerror="x()" alt="A">'
        '<a href="*|UNSUB|*">u</a><a href="{{ unsubscribe }}">u</a> {% unsubscribe %}'
        '<iframe src="https://x"></iframe><td style="background:url(javascript:x)">t</td></body></html>'
    )
    clean = render.sanitize_html(dirty)
    for bad in ("<script", "alert", "<form", "<input", "onclick", "onerror", "javascript", "<iframe", "url("):
        assert bad not in clean, bad
    assert '<a href="*|UNSUB|*">' in clean and '<a href="{{ unsubscribe }}">' in clean and "{% unsubscribe %}" in clean
    assert '<img src="https://ok.example/a.png" alt="A">' in clean and clean.startswith("<!DOCTYPE html>")


def _content(**extra) -> dict:
    clean, _ = render.validate(_answer(), allowed_urls=[f"{SITE}/services"], facts="")
    return {**clean, "image_url": "", "image_alt": "", "from_name": "",
            "footer": {"business": "Geet Salon", "address": "", "language": "English"}, **extra}


@pytest.mark.parametrize(
    ("channel", "tag"),
    [("mailchimp", '<a href="*|UNSUB|*"'), ("brevo", '<a href="{{ unsubscribe }}"'),
     ("klaviyo", "{% unsubscribe %}"), ("export", "Unsubscribe: [UNSUBSCRIBE_LINK]")],
)
def test_each_platform_gets_its_own_unsubscribe_tag(channel, tag):
    html = render.render_html(_content(), channel=channel)
    assert tag in html
    assert render.render_text(_content(), channel=channel).rstrip().endswith(render.UNSUBSCRIBE[channel][1])
    others = [t for c, (_, t) in render.UNSUBSCRIBE.items() if c != channel]
    assert not any(other in html for other in others)


def test_html_is_responsive_accessible_and_escaped():
    html = render.render_html(
        _content(headline="Cuts & <colour>", image_url="https://cdn.example/h.png", image_alt="A salon chair"),
        channel="export",
    )
    assert 'role="presentation"' in html and "max-width:600px" in html and 'lang="en"' in html
    assert 'alt="A salon chair"' in html and f'href="{SITE}/services"' in html
    assert "Cuts &amp; &lt;colour&gt;" in html
    assert "LIST_ADDRESS" not in html  # no address invented
    with_address = render.render_html(_content(footer={"business": "Geet", "address": "1 Main St, Noida"}), channel="brevo")
    assert "1 Main St, Noida" in with_address
    assert "*|HTML:LIST_ADDRESS_HTML|*" in render.render_html(_content(), channel="mailchimp")
    assert "<img" not in render.render_html(_content(image_url="http://insecure.example/x.png"), channel="export")


# ── Exports ──────────────────────────────────────────────────────────────────
def _asset(channel: str = "export", **content) -> MarketingAsset:
    return MarketingAsset(id="e1", tenant_id="t", agent_slug="email_marketer", kind="email", channel=channel,
                          title="Welcome 1/4 (day 0): Welcome to Geet Salon", status=AssetStatus.DRAFT,
                          content_encrypted=json.dumps(_content(**content)), detail={}, image_ids=[],
                          remote_id="", remote_url="", error="")


def test_exports_html_text_and_an_eml_draft_that_parses():
    assert assets.formats_for("email").keys() >= {"html", "txt", "eml", "json"}
    html = assets.export(_asset(channel="mailchimp"), _Cipher(), "html")
    assert html.filename.endswith(".html") and b"[UNSUBSCRIBE_LINK]" in html.data and b"*|UNSUB|*" not in html.data
    text = assets.export(_asset(), _Cipher(), "txt").data.decode()
    assert text.startswith("Subject: Welcome to Geet Salon") and "[UNSUBSCRIBE_LINK]" in text

    eml = assets.export(_asset(), _Cipher(), "eml")
    assert eml.mime == "message/rfc822" and eml.filename.endswith(".eml")
    message = email.message_from_bytes(eml.data, policy=policy.default)
    assert message["Subject"] == "Welcome to Geet Salon"
    assert message["To"] is None and message["Cc"] is None and message["Bcc"] is None
    assert message.get_content_type() == "multipart/alternative"
    parts = {part.get_content_type(): part.get_content() for part in message.iter_parts()}
    assert "Thanks for joining us" in parts["text/plain"]
    assert "<table" in parts["text/html"] and "[UNSUBSCRIBE_LINK]" in parts["text/html"]


# ── Publishing ───────────────────────────────────────────────────────────────
class _Platform:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def create_campaign_draft(self, **kw):  # noqa: ANN003, ANN201
        self.calls.append(kw)
        return "c-1", "https://us21.admin.mailchimp.com/campaigns/edit?id=9"


def test_the_publisher_routes_by_channel_and_records_the_draft(monkeypatch):
    platform = _Platform()
    closed: list[bool] = []
    monkeypatch.setattr(publishing, "_bundle", lambda db, org: SimpleNamespace(
        instances={"brevo": platform}, close=lambda: closed.append(True)))
    db, org = SimpleNamespace(flush=lambda: None), SimpleNamespace(id="t")
    asset = _asset(channel="brevo", from_name="Geet Team")
    assets.release(db, org, asset, _Cipher())
    assert asset.status == AssetStatus.PUBLISHED and asset.remote_id == "c-1" and closed == [True]
    call = platform.calls[0]
    assert call["subject"] == "Welcome to Geet Salon" and call["from_name"] == "Geet Team"
    assert "{{ unsubscribe }}" in call["html"] and "{{ unsubscribe }}" in call["text"]

    missing = _asset(channel="klaviyo")
    with pytest.raises(ConflictError, match="Klaviyo is not connected"):
        assets.publish(db, org, missing, _Cipher())
    assert missing.status == AssetStatus.FAILED


def test_download_only_emails_are_approved_not_published(monkeypatch):
    monkeypatch.setattr(publishing, "_bundle", lambda db, org: pytest.fail("no platform call"))
    db, org = SimpleNamespace(flush=lambda: None), SimpleNamespace(id="t")
    asset = _asset(channel="export")
    asset.detail = {"export_only": True}
    assets.release(db, org, asset, _Cipher())
    assert asset.status == AssetStatus.APPROVED


def test_edited_content_that_cannot_be_sent_is_refused():
    with pytest.raises(InvalidInputError):
        publishing.check_publishable({**_content(), "cta_url": "javascript:alert(1)"})
    with pytest.raises(InvalidInputError):
        publishing.check_publishable({**_content(), "subject": " "})


# ── Connectors ───────────────────────────────────────────────────────────────
def _make(cls, values: dict, handler):  # noqa: ANN001, ANN202
    connector = cls(Credentials(values=values), org_id="o")
    connector._client = httpx.Client(base_url=connector.base_url, transport=httpx.MockTransport(handler))
    return connector


MAILCHIMP = {"apiKey": "abc123-us21", "audienceId": "list1", "fromName": "Geet", "replyTo": "hi@geet.example"}


def test_mailchimp_creates_a_draft_then_uploads_its_content():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.method == "POST":
            return httpx.Response(200, json={"id": "cmp1", "web_id": 42})
        return httpx.Response(200, json={})

    mailchimp = _make(MailchimpConnector, MAILCHIMP, handler)
    remote_id, url = mailchimp.create_campaign_draft(
        name="Welcome 1/4", subject="Hi", preview_text="Pre", html="<p>x</p>", text="x", from_name="", reply_to="")
    assert (remote_id, url) == ("cmp1", "https://us21.admin.mailchimp.com/campaigns/edit?id=42")
    create, content = seen
    assert str(create.url) == "https://us21.api.mailchimp.com/3.0/campaigns"
    assert create.headers["Authorization"] == "Basic " + base64.b64encode(b"anystring:abc123-us21").decode()
    assert json.loads(create.content) == {
        "type": "regular",
        "recipients": {"list_id": "list1"},
        "settings": {"subject_line": "Hi", "preview_text": "Pre", "title": "Welcome 1/4",
                     "from_name": "Geet", "reply_to": "hi@geet.example"},
    }
    assert content.method == "PUT" and content.url.path == "/3.0/campaigns/cmp1/content"
    assert json.loads(content.content) == {"html": "<p>x</p>", "plain_text": "x"}
    assert not any("send" in r.url.path or "schedule" in r.url.path for r in seen)


def test_mailchimp_removes_a_half_made_draft_and_checks_the_key():
    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        if request.method == "POST":
            return httpx.Response(200, json={"id": "cmp1"})
        if request.method == "PUT":
            return httpx.Response(400, json={"detail": "bad html"})
        return httpx.Response(204)

    with pytest.raises(ConnectorError):
        _make(MailchimpConnector, MAILCHIMP, handler).create_campaign_draft(
            name="n", subject="s", preview_text="p", html="h", text="t", from_name="", reply_to="")
    assert seen[-1] == ("DELETE", "/3.0/campaigns/cmp1")

    for bad in ("nodash", "abc-evil.example/x", "abc-"):
        with pytest.raises(ConnectorConfigError, match="server code"):
            _ = MailchimpConnector(Credentials(values={**MAILCHIMP, "apiKey": bad})).base_url


def test_mailchimp_health_pings_and_reads_the_audience():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/3.0/ping":
            return httpx.Response(200, json={"health_status": "Everything's Chimpy!"})
        assert request.url.path == "/3.0/lists/list1"
        return httpx.Response(200, json={"id": "list1", "name": "Customers"})

    report = _make(MailchimpConnector, MAILCHIMP, handler).check_health()
    assert report.ok and "Customers" in report.detail
    bad = _make(MailchimpConnector, MAILCHIMP, lambda r: httpx.Response(401, json={})).check_health()
    assert not bad.ok and "API key" in bad.detail
    missing = _make(MailchimpConnector, MAILCHIMP, lambda r: httpx.Response(404, json={}) if "lists" in r.url.path
                    else httpx.Response(200, json={})).check_health()
    assert not missing.ok and "Audience ID" in missing.detail


BREVO = {"apiKey": "xkeysib-1", "senderName": "Geet", "senderEmail": "hi@geet.example", "listIds": "2, 7"}


def test_brevo_creates_a_draft_campaign():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(201, json={"id": 77})

    brevo = _make(BrevoConnector, BREVO, handler)
    assert brevo.create_campaign_draft(name="N", subject="S", preview_text="P", html="<p>h</p>", text="t",
                                       from_name="Geet Team", reply_to="") == ("77", "")
    (request,) = seen
    assert str(request.url) == "https://api.brevo.com/v3/emailCampaigns"
    assert request.headers["api-key"] == "xkeysib-1"
    body = json.loads(request.content)
    assert body == {
        "name": "N", "subject": "S", "sender": {"name": "Geet Team", "email": "hi@geet.example"},
        "htmlContent": "<p>h</p>", "previewText": "P", "replyTo": "hi@geet.example",
        "recipients": {"listIds": [2, 7]},
    }
    assert "scheduledAt" not in body


def test_brevo_list_ids_and_health():
    without = _make(BrevoConnector, {**BREVO, "listIds": ""}, lambda r: httpx.Response(201, json={"id": 1}))
    assert without.list_ids == []
    with pytest.raises(ConnectorConfigError):
        _ = BrevoConnector(Credentials(values={**BREVO, "listIds": "2, news"})).list_ids
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return httpx.Response(200, json={"companyName": "Geet Salon", "email": "a@b.c"})

    report = _make(BrevoConnector, BREVO, handler).check_health()
    assert report.ok and "Geet Salon" in report.detail
    assert paths == ["/v3/account", "/v3/contacts/lists/2", "/v3/contacts/lists/7"]
    assert "SMTP key" in _make(BrevoConnector, BREVO, lambda r: httpx.Response(401, json={})).check_health().detail


KLAVIYO = {"apiKey": "pk_live", "revision": ""}


def test_klaviyo_saves_a_template_and_creates_no_campaign():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(201, json={"data": {"type": "template", "id": "TPL1"}})

    klaviyo = _make(KlaviyoConnector, KLAVIYO, handler)
    assert klaviyo.create_campaign_draft(name="N", subject="S", preview_text="P", html="<p>h</p>", text="t",
                                         from_name="", reply_to="") == ("TPL1", "")
    (request,) = seen
    assert str(request.url) == "https://a.klaviyo.com/api/templates"
    assert request.headers["Authorization"] == "Klaviyo-API-Key pk_live"
    assert request.headers["revision"] == DEFAULT_REVISION
    assert request.headers["Content-Type"] == "application/vnd.api+json"
    assert json.loads(request.content) == {
        "data": {"type": "template", "attributes": {"name": "N", "editor_type": "CODE", "html": "<p>h</p>", "text": "t"}}
    }


def test_klaviyo_health_and_revision_field():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/accounts" and request.headers["revision"] == "2026-01-15"
        return httpx.Response(200, json={"data": [{"attributes": {"contact_information": {"organization_name": "Geet"}}}]})

    report = _make(KlaviyoConnector, {**KLAVIYO, "revision": "2026-01-15"}, handler).check_health()
    assert report.ok and "Geet" in report.detail
    assert "pk_" in _make(KlaviyoConnector, KLAVIYO, lambda r: httpx.Response(401, json={})).check_health().detail


# ── Wiring ───────────────────────────────────────────────────────────────────
def test_connectors_are_registered_in_the_email_marketing_category():
    for slug in ("mailchimp", "brevo", "klaviyo"):
        spec = registry.get_spec(slug)
        assert spec is not None and spec.category == "Email marketing"
        assert spec.capabilities == frozenset({Capability.CREATE_EMAIL_CAMPAIGN})
        assert spec.requirements and next(f for f in spec.fields if f.key == "apiKey").is_secret
    assert registry.CATEGORY_ORDER.index("Email marketing") == registry.CATEGORY_ORDER.index("CRM") + 1
    order = registry.connector_slugs()
    assert order.index("zapier_webhooks") < order.index("mailchimp") < order.index("brevo") < order.index("klaviyo")


def test_agent_declarations():
    spec = AGENT.spec
    assert spec.slug == "email_marketer" and spec.requires_llm and spec.max_impact.value == "high"
    assert spec.default_schedule == "Weekly" and spec.category.value == "Content & Social"
    assert {o.capability for o in GUIDE.optional} == {Capability.CREATE_EMAIL_CAMPAIGN, Capability.GENERATE_IMAGE}
    assert {f.key for f in GUIDE.settings} == {"programs", "newsletter_frequency", "from_name"}
    assert "Never sends" in GUIDE.effect and "draft" in GUIDE.effect
    assert callable(RESULTS)
    no_model = SimpleNamespace(with_capability=lambda capability: None, scope="")
    assert "Waiting on an AI model" in AGENT.preflight(no_model)
