"""Off-page fixes: real backlink data, competitor gaps, discovery approvals,
publisher contact addresses and honest pitch delivery.

No database: agents run against a fake context, connectors against
``httpx.MockTransport``.
"""
from __future__ import annotations

import base64
import json
from datetime import timedelta
from types import SimpleNamespace

import httpx
import pytest
from pydantic import ValidationError

from app.agents.backlink_node_discovery import agent as discovery
from app.agents.competitor_link_monitor import agent as monitor
from app.agents.digital_pr_outreach import agent as pr
from app.agents.digital_pr_outreach.prompts import mask_email
from app.connectors.base import registry
from app.connectors.base.connector import Capability
from app.connectors.base.credentials import Credentials
from app.connectors.bing_webmaster.connector import BingWebmasterConnector
from app.connectors.dataforseo_backlinks.connector import (
    DataForSeoBacklinksConnector,
    normalise_target,
)
from app.connectors.search_console.connector import SearchConsoleConnector
from app.core.exceptions import ConnectorError, NotFoundError
from app.db.base import utcnow
from app.models.approval import ApprovalItem
from app.models.offpage import BacklinkStatus, BacklinkTarget, CompetitorAlert, OutreachPitch

BRAND = "Acme Motors"
BODY = (
    "Acme Motors has compiled twelve months of service-bay data across four cities, "
    "covering which repairs drivers put off longest and what that delay costs them. "
    "We would be glad to share the full dataset and two charts your readers could use "
    "directly, with no obligation and nothing to promote beyond the findings themselves."
)


# ── Fakes ──────────────────────────────────────────────────────────────────
class _Result:
    def __init__(self, rows) -> None:  # noqa: ANN001
        self.rows = list(rows)

    def __iter__(self):  # noqa: ANN204
        return iter(self.rows)

    def scalars(self):  # noqa: ANN201
        return iter(self.rows)

    def scalar_one(self):  # noqa: ANN201
        return self.rows[0]

    def scalar_one_or_none(self):  # noqa: ANN201
        return self.rows[0] if self.rows else None


class _Db:
    """Returns queued results in order; anything unqueued is empty."""

    def __init__(self, results: list | None = None) -> None:
        self.results = list(results or [])
        self.added: list = []
        self.objects: dict = {}
        self.committed = False

    def execute(self, statement):  # noqa: ANN001, ANN201
        return _Result(self.results.pop(0) if self.results else [])

    def add(self, row) -> None:  # noqa: ANN001
        if getattr(row, "id", None) is None:
            row.id = f"00000000-0000-0000-0000-{len(self.added):012d}"
        self.added.append(row)
        self.objects[(type(row), row.id)] = row

    def put(self, row):  # noqa: ANN001, ANN201
        self.objects[(type(row), row.id)] = row
        return row

    def get(self, cls, key):  # noqa: ANN001, ANN201
        return self.objects.get((cls, key))

    def flush(self) -> None:
        pass

    def commit(self) -> None:
        self.committed = True


class _Cipher:
    def __init__(self, payloads: dict | None = None) -> None:
        self.payloads = payloads or {}

    def encrypt(self, value, *, context):  # noqa: ANN001, ANN201
        return f"enc:{value}"

    def decrypt(self, token, *, context, default=""):  # noqa: ANN001, ANN201
        return (token or "").removeprefix("enc:") or default

    def decrypt_json(self, token, *, context, default=None):  # noqa: ANN001, ANN201
        return self.payloads.get(token, default)


class _Notifier:
    def __init__(self, capabilities: set[Capability], name: str = "Notifier") -> None:
        self.capabilities = capabilities
        self.name = name
        self.sent: list[dict] = []

    def supports(self, capability: Capability) -> bool:
        return capability in self.capabilities

    def notify(self, *, subject: str, message: str, channel: str = "") -> bool:
        self.sent.append({"subject": subject, "message": message, "channel": channel})
        return True


def _ctx(
    db: _Db,
    *,
    connectors: dict | None = None,
    scope: str = "",
    settings: dict | None = None,
    answers: list | None = None,
    primary_domain: str = "acme.com",
    cipher: _Cipher | None = None,
):  # noqa: ANN202
    connectors = connectors or {}
    answers = list(answers or [])
    settings = settings or {}
    asked: list[str] = []

    def ask_json(prompt, **kwargs):  # noqa: ANN001, ANN003, ANN202
        asked.append(prompt)
        return answers.pop(0)

    return SimpleNamespace(
        db=db,
        scope=scope,
        tenant_id="t1",
        industry="",
        org=SimpleNamespace(id="t1", name=BRAND, primary_domain=primary_domain),
        cipher=cipher or _Cipher(),
        setting=lambda key, default=None: settings.get(key, default),
        with_capability=lambda capability: connectors.get(capability),
        log_info=lambda *a, **k: None,
        log_warning=lambda *a, **k: None,
        ask_json=ask_json,
        asked=asked,
    )


def _target(domain: str = "editor.com", email: str | None = None, **kw) -> BacklinkTarget:  # noqa: ANN003
    target = BacklinkTarget(
        tenant_id="t1",
        domain=domain,
        authority=kw.get("authority", 0),
        placement_type=kw.get("placement_type", "Guest Post"),
        status=BacklinkStatus.DISCOVERED.value,
        relevance=kw.get("relevance", 0.8),
        contact_email=email,
    )
    target.id = kw.get("id", f"target-{domain}")
    return target


def _pitch(target: BacklinkTarget, *, status: str = "drafted", pid: str = "p1") -> OutreachPitch:
    pitch = OutreachPitch(
        tenant_id="t1",
        target_id=target.id,
        target_domain=target.domain,
        subject_encrypted="enc:Service data for your readers",
        body_encrypted=f"enc:{BODY}",
        status=status,
        is_counter_pitch=False,
    )
    pitch.id = pid
    return pitch


# ── DataForSEO connector ───────────────────────────────────────────────────
def _dfs(handler) -> DataForSeoBacklinksConnector:  # noqa: ANN001
    connector = DataForSeoBacklinksConnector(
        Credentials(values={"login": "me@acme.com", "password": "pw"}), org_id="o"
    )
    connector._client = httpx.Client(
        base_url="https://api.dataforseo.com/v3", transport=httpx.MockTransport(handler)
    )
    return connector


def _envelope(result: dict | None, *, code: int = 20000, message: str = "Ok.") -> dict:
    return {
        "status_code": 20000,
        "status_message": "Ok.",
        "tasks": [
            {
                "status_code": code,
                "status_message": message,
                "result": [result] if result is not None else None,
            }
        ],
    }


def test_backlinks_request_is_the_documented_live_call_and_rows_are_normalised():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json=_envelope(
                {
                    "items": [
                        {
                            "domain_from": "www.News.example",
                            "url_from": "https://news.example/a",
                            "url_to": "https://rival.com/",
                            "anchor": "rival deals",
                            "dofollow": True,
                            "domain_from_rank": 437,
                            "first_seen": "2026-09-01 10:00:00 +00:00",
                        },
                        {"domain_from": "news.example", "domain_from_rank": 400},  # duplicate
                        {"domain_from": "rival.com"},  # the target itself
                        {"domain_from": "blog.other", "domain_from_rank": None},
                        "junk",
                    ]
                }
            ),
        )

    rows = _dfs(handler).read_backlinks(domain="https://www.Rival.com/path", limit=5000)
    request = seen[0]
    assert request.method == "POST"
    assert request.url.path == "/v3/backlinks/backlinks/live"
    expected = base64.b64encode(b"me@acme.com:pw").decode()
    assert request.headers["Authorization"] == f"Basic {expected}"
    assert json.loads(request.content) == [
        {"target": "rival.com", "limit": 1000, "mode": "one_per_domain"}
    ]
    assert [r["domain"] for r in rows] == ["news.example", "blog.other"]
    assert rows[0]["authority"] == 44  # 437 on the 0–1000 scale
    assert rows[0]["anchor"] == "rival deals" and rows[0]["dofollow"] is True
    assert rows[0]["url"] == "https://news.example/a"
    assert rows[1]["authority"] == 0  # not reported, not invented


def test_a_failed_task_is_a_clear_error_not_an_empty_list():
    connector = _dfs(
        lambda r: httpx.Response(200, json=_envelope(None, code=40200, message="Payment Required."))
    )
    with pytest.raises(ConnectorError, match="Payment Required.*40200"):
        connector.read_backlinks(domain="rival.com")
    assert "credit" in (connector.diagnose(ConnectorError("code 40200")) or "")

    whole = _dfs(
        lambda r: httpx.Response(200, json={"status_code": 40100, "status_message": "Not authorized."})
    )
    with pytest.raises(ConnectorError, match="Not authorized"):
        whole.read_backlinks(domain="rival.com")


def test_filters_are_passed_through_and_no_domain_is_refused():
    bodies: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json=_envelope({"items": None}))

    connector = _dfs(handler)
    assert connector.read_backlinks(domain="rival.com", filters=["dofollow", "=", True]) == []
    assert bodies[0][0]["filters"] == ["dofollow", "=", True]
    with pytest.raises(ConnectorError):
        connector.read_backlinks(domain="  ")


def test_domain_summary_reports_rank_and_whether_the_index_knows_it():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v3/backlinks/summary/live"
        target = json.loads(request.content)[0]["target"]
        if target == "real.example":
            return httpx.Response(
                200, json=_envelope({"rank": 512, "backlinks": 900, "referring_domains": 120})
            )
        return httpx.Response(200, json=_envelope({"rank": 0, "backlinks": 0, "referring_domains": 0}))

    connector = _dfs(handler)
    real = connector.read_domain_summary(domain="real.example")
    assert real == {
        "domain": "real.example",
        "authority": 51,
        "referring_domains": 120,
        "backlinks": 900,
        "found": True,
    }
    assert connector.read_domain_summary(domain="invented.example")["found"] is False


def test_health_probe_reads_the_account():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET" and request.url.path == "/v3/appendix/user_data"
        return httpx.Response(200, json=_envelope({"login": "me@acme.com", "money": {"balance": 12.5}}))

    report = _dfs(handler).check_health()
    assert report.ok and "me@acme.com" in report.detail and "$12.50" in report.detail

    bad = _dfs(lambda r: httpx.Response(401, json={})).check_health()
    assert bad.ok is False


def test_the_connector_is_registered_and_states_what_it_costs():
    spec = registry.get_spec("dataforseo_backlinks")
    assert spec is not None
    assert spec.capabilities == frozenset({Capability.READ_BACKLINKS})
    assert spec.category == "Analytics & Search"
    assert any("paid" in r.lower() and "charges" in r.lower() for r in spec.requirements)
    assert {f.key for f in spec.fields} == {"login", "password"}
    assert [f.key for f in spec.fields if f.is_secret] == ["password"]
    assert "dataforseo_backlinks" in registry.CATALOG_ORDER
    real = [s for s in registry.slugs_with_capability(Capability.READ_BACKLINKS) if not s.startswith("fake")]
    assert real == ["dataforseo_backlinks"]


def test_normalise_target():
    assert normalise_target("HTTPS://www.Rival.com:443/x?y#z") == "rival.com"
    assert normalise_target("") == ""


def test_search_console_and_bing_no_longer_claim_backlinks():
    for klass in (SearchConsoleConnector, BingWebmasterConnector):
        assert Capability.READ_BACKLINKS not in klass.spec.capabilities
        assert Capability.READ_SEARCH_PERFORMANCE in klass.spec.capabilities
        assert "read_backlinks" not in klass.__dict__
        with pytest.raises(NotImplementedError):
            klass(Credentials()).read_backlinks(domain="rival.com")


# ── Competitor Link Monitor ────────────────────────────────────────────────
@pytest.mark.parametrize("scope", ["rival.com", "rival.com, other.co.uk", ""])
def test_monitor_accepts_domains(scope):
    assert monitor.AGENT.validate_scope(scope) is None


@pytest.mark.parametrize("scope", ["Rival Motors", "rival.com, Other Co", "https://rival.com"])
def test_monitor_refuses_names_and_urls(scope):
    problem = monitor.AGENT.validate_scope(scope)
    assert problem is not None
    if "Rival Motors" in scope or "Other Co" in scope:
        assert "domain" in problem


def test_monitor_skips_with_a_clear_reason_when_competitors_are_names():
    ctx = _ctx(_Db(), settings={"competitors": ["Rival Motors", "Other Co"]})
    result = monitor.AGENT.run(ctx)
    assert result.skipped and "cannot be looked up" in result.skip_reason
    assert "Rival Motors" in result.skip_reason


def test_monitor_needs_the_customers_own_domain():
    result = monitor.AGENT.run(_ctx(_Db(), scope="rival.com", primary_domain=""))
    assert result.skipped and "domain" in result.skip_reason


class _Backlinks:
    name = "Fake backlinks"

    def __init__(self, links: dict[str, list[str]]) -> None:
        self.links = links
        self.calls: list[tuple[str, int]] = []

    def read_backlinks(self, *, domain: str, limit: int = 50) -> list[dict]:
        self.calls.append((domain, limit))
        return [{"domain": d, "authority": 40, "anchor": "a", "url": f"https://{d}/x"} for d in self.links.get(domain, [])]


def test_detection_looks_up_each_competitor_and_reports_only_the_gap(monkeypatch):
    source = _Backlinks(
        {
            "acme.com": ["shared.example", "ours.example"],
            "rival.com": ["shared.example", "gap.example", "blog.rival.com", "acme.com", "seen.example"],
            "other.io": ["gap2.example"],
        }
    )
    monkeypatch.setattr(monitor.AGENT, "_known_pairs", lambda ctx: {("rival.com", "seen.example")})
    db = _Db()
    ctx = _ctx(db, connectors={Capability.READ_BACKLINKS: source})

    found, problem = monitor.AGENT._detect(ctx, ["rival.com", "other.io"], "acme.com")
    assert problem == ""
    assert [domain for domain, _ in source.calls] == ["acme.com", "rival.com", "other.io"]
    alerts = [row for row in db.added if isinstance(row, CompetitorAlert)]
    assert [(a.competitor, a.source_domain) for a in alerts] == [
        ("rival.com", "gap.example"),
        ("other.io", "gap2.example"),
    ]
    assert found == 2
    assert alerts[0].text == "gap.example links to rival.com but not to you (authority 40)."


def test_detection_records_nothing_when_own_links_cannot_be_read(monkeypatch):
    class Broken(_Backlinks):
        def read_backlinks(self, *, domain: str, limit: int = 50) -> list[dict]:
            raise ConnectorError("down")

    db = _Db()
    ctx = _ctx(db, connectors={Capability.READ_BACKLINKS: Broken({})})
    found, problem = monitor.AGENT._detect(ctx, ["rival.com"], "acme.com")
    assert found == 0 and "own backlinks" in problem and db.added == []


def _alert(**kw) -> CompetitorAlert:  # noqa: ANN003
    alert = CompetitorAlert(
        tenant_id="t1",
        competitor=kw.get("competitor", "rival.com"),
        source_domain=kw.get("domain", "gap.example"),
        authority=kw.get("authority", 40),
        text="x",
        detected_at=utcnow(),
        counter_launched=False,
        detail=kw.get("detail", {"anchor": "rival deals"}),
    )
    alert.id = kw.get("id", "alert1")
    alert.counter_pitch_id = kw.get("counter_pitch_id")
    return alert


def test_counter_pitch_uses_the_configured_angle_and_carries_the_email(monkeypatch):
    alert = _alert()
    monkeypatch.setattr(monitor.AGENT, "_alerts_to_answer", lambda ctx: [alert])
    db = _Db()
    team = _Notifier({Capability.SEND_NOTIFICATION})
    ctx = _ctx(
        db,
        scope="rival.com",
        settings={"counter_angle": "our winter tyre test"},
        connectors={Capability.SEND_NOTIFICATION: team},
        answers=[{"subject": "Winter tyre data", "body": BODY}],
    )
    actions = monitor.AGENT._counter_pitch(ctx)
    assert "PREFERRED_ANGLE: our winter tyre test" in ctx.asked[0]
    assert len(actions) == 1
    action = actions[0]
    assert action.payload["subject"] == "Winter tyre data"
    assert action.payload["body"] == BODY
    assert action.payload["delivery"] == pr.DELIVERY_TEAM
    assert "to your team" in action.title
    assert action.impact.value == "high"
    assert action.target_kind == "counter_pitch"

    # Approving delivers to the team as a draft and does not mark it launched.
    db.put(alert)
    action.apply(ctx)
    pitch = next(row for row in db.added if isinstance(row, OutreachPitch))
    assert pitch.status == pr.TEAM_DRAFT
    assert alert.counter_launched is False and alert.counter_pitch_id == pitch.id
    assert team.sent and team.sent[0]["channel"] == ""


def test_counter_pitch_to_a_publisher_with_an_address_is_launched(monkeypatch):
    target = _target("gap.example", email="editor@gap.example")
    alert = _alert()
    db = _Db()
    db.put(target)
    db.put(alert)
    mailer = _Notifier({Capability.SEND_EMAIL, Capability.SEND_NOTIFICATION})
    ctx = _ctx(db, connectors={Capability.SEND_EMAIL: mailer, Capability.SEND_NOTIFICATION: mailer})
    pitch = _pitch(target, pid="cp1")
    db.put(pitch)
    monitor._make_counter_sender(
        "cp1", alert.id, delivery=pr.DELIVERY_PUBLISHER, recipient="editor@gap.example"
    )(ctx)
    assert pitch.status == pr.SENT and alert.counter_launched is True
    assert mailer.sent[0]["channel"] == "editor@gap.example"


def test_no_second_counter_pitch_while_one_is_pending_even_after_the_cooldown():
    old = utcnow() - timedelta(days=60)
    db = _Db(
        results=[
            [("pending.example", old, "drafted"), ("fresh.example", utcnow(), "sent"), ("old.example", old, "sent")],
            [
                _alert(domain="pending.example", id="a1"),
                _alert(domain="fresh.example", id="a2"),
                _alert(domain="old.example", id="a3"),
                _alert(domain="rejected.example", id="a4", detail={"counter_rejected": True}),
            ],
        ]
    )
    alerts = monitor.AGENT._alerts_to_answer(_ctx(db))
    assert [a.source_domain for a in alerts] == ["old.example"]


def test_rejecting_a_counter_pitch_closes_its_alert():
    from app.services.approvals import REJECTERS

    target = _target("gap.example")
    target.status = BacklinkStatus.PITCHED.value
    pitch = _pitch(target, pid="cp1")
    alert = _alert()
    db = _Db()
    for row in (target, pitch, alert):
        db.put(row)
    item = ApprovalItem(target_kind="counter_pitch", target_id="cp1")
    REJECTERS["counter_pitch"](db, SimpleNamespace(id="t1"), item, {"pitch_id": "cp1", "alert_id": "alert1"})
    assert pitch.status == pr.REJECTED
    assert alert.detail["counter_rejected"] is True
    assert target.status == BacklinkStatus.DISCOVERED.value


# ── Backlink Node Discovery ────────────────────────────────────────────────
def test_each_suggested_domain_gets_its_own_stable_approval_target():
    a = discovery.target_key("News.example")
    assert a == discovery.target_key("https://www.news.example/")
    assert a != discovery.target_key("other.example")
    import uuid

    uuid.UUID(a)  # fits the UUID column


def _suggest(*domains: str, relevance: float = 0.8) -> dict:
    return {
        "targets": [
            {"domain": d, "relevance": relevance, "placement_type": "Guest Post", "why": "covers EVs", "contact_route": "editorial form"}
            for d in domains
        ]
    }


def test_discovery_excludes_pending_suggestions_and_reports_honestly(monkeypatch):
    monkeypatch.setattr(discovery.AGENT, "_known_domains", lambda ctx: {"known.example"})
    monkeypatch.setattr(discovery.AGENT, "_proposed_domains", lambda ctx: {"pending.example"})
    monkeypatch.setattr(discovery.AGENT, "_target_count", lambda ctx: 7)
    ctx = _ctx(
        _Db(),
        scope="EV charging",
        answers=[_suggest("known.example", "pending.example", "new.example", "buy-links-cheap.com")],
    )
    result = discovery.AGENT.run(ctx)
    assert [a.payload["domain"] for a in result.actions] == ["new.example"]
    assert "pending.example" in ctx.asked[0]  # the model is told not to repeat it
    assert result.summary.startswith("1 new targets kept of 4 suggested")
    assert "2 already known or awaiting review" in result.summary
    assert "1 likely link sellers" in result.summary
    assert result.metric_label == "7 targets on file"
    action = result.actions[0]
    assert action.target_id == discovery.target_key("new.example")
    assert action.payload["authority"] == 0
    assert action.payload["authority_measured"] is False
    assert action.payload["estimates_unverified"] is True
    assert "authority" not in action.title


def test_discovery_measures_and_verifies_with_a_backlink_index(monkeypatch):
    class Index:
        name = "DataForSEO Backlinks"

        def supports(self, capability):  # noqa: ANN001, ANN201
            return capability is Capability.READ_BACKLINKS

        def read_domain_summary(self, *, domain: str) -> dict:
            if domain == "made-up.example":
                return {"found": False}
            return {"found": True, "authority": 63, "referring_domains": 410}

    monkeypatch.setattr(discovery.AGENT, "_known_domains", lambda ctx: set())
    monkeypatch.setattr(discovery.AGENT, "_proposed_domains", lambda ctx: set())
    monkeypatch.setattr(discovery.AGENT, "_target_count", lambda ctx: 0)
    ctx = _ctx(
        _Db(),
        scope="EV charging",
        connectors={Capability.READ_BACKLINKS: Index()},
        answers=[_suggest("real.example", "made-up.example")],
    )
    result = discovery.AGENT.run(ctx)
    assert [a.payload["domain"] for a in result.actions] == ["real.example"]
    payload = result.actions[0].payload
    assert payload["authority"] == 63 and payload["authority_measured"] is True
    assert payload["estimates_unverified"] is False
    assert "DataForSEO" in payload["discovered_via"]
    assert "1 not found in the backlink index" in result.summary
    assert "authority measured for 1" in result.summary


def test_relevance_floor_comes_from_the_setting(monkeypatch):
    monkeypatch.setattr(discovery.AGENT, "_known_domains", lambda ctx: set())
    monkeypatch.setattr(discovery.AGENT, "_proposed_domains", lambda ctx: set())
    monkeypatch.setattr(discovery.AGENT, "_target_count", lambda ctx: 0)
    ctx = _ctx(_Db(), scope="EV", settings={"min_relevance": 0.9}, answers=[_suggest("a.example", relevance=0.8)])
    result = discovery.AGENT.run(ctx)
    assert result.actions == [] and "1 below the relevance floor" in result.summary


def test_approving_a_suggestion_records_it_like_the_autonomous_path():
    from app.services.approvals import APPLIERS

    payload = {
        "domain": "new.example",
        "authority": 0,
        "relevance": 0.8,
        "placement_type": "Guest Post",
        "why": "covers EVs",
        "contact_route": "editorial form",
        "discovered_via": "suggested by the model",
    }
    approved = _Db()
    item = ApprovalItem(target_kind="backlink_target", target_id=discovery.target_key("new.example"))
    APPLIERS["backlink_target"](approved, SimpleNamespace(id="t1"), item, payload)

    autonomous = _Db()
    discovery._make_recorder(
        domain="new.example",
        authority=0,
        relevance=0.8,
        placement="Guest Post",
        via="suggested by the model",
        notes=discovery._notes("covers EVs", "editorial form"),
    )(SimpleNamespace(db=autonomous, tenant_id="t1"))

    def shape(db: _Db) -> tuple:
        (row,) = db.added
        return (row.tenant_id, row.domain, row.authority, row.relevance, row.placement_type,
                row.status, row.contact_email, row.discovered_via, row.notes)

    assert shape(approved) == shape(autonomous)
    assert shape(approved)[6] is None  # never a guessed address
    assert "editorial form" in shape(approved)[8]


def test_an_existing_target_is_not_inserted_twice():
    db = _Db(results=[[_target("new.example")]])
    from app.services.approvals import APPLIERS

    APPLIERS["backlink_target"](db, SimpleNamespace(id="t1"), ApprovalItem(), {"domain": "new.example"})
    assert db.added == []


def test_proposed_domains_are_read_from_pending_payloads():
    db = _Db(results=[[("tok1",), ("tok2",)]])
    cipher = _Cipher({"tok1": {"domain": "www.Pending.example"}, "tok2": {}})
    assert discovery.AGENT._proposed_domains(_ctx(db, cipher=cipher)) == {"pending.example"}


# ── Digital PR Outreach ────────────────────────────────────────────────────
def test_a_pitch_with_no_address_goes_to_the_team_and_is_not_recorded_as_sent():
    target = _target("editor.com")
    pitch = _pitch(target)
    db = _Db()
    db.put(target)
    db.put(pitch)
    slack = _Notifier({Capability.SEND_NOTIFICATION})
    ctx = _ctx(db, connectors={Capability.SEND_NOTIFICATION: slack})

    delivered = pr._make_sender("p1", delivery=pr.DELIVERY_TEAM, recipient="")(ctx)
    assert delivered == pr.DELIVERY_TEAM
    assert pitch.status == pr.TEAM_DRAFT and pitch.sent_at is None
    assert target.status == BacklinkStatus.DISCOVERED.value
    assert slack.sent[0]["channel"] == ""
    assert "not sent" in slack.sent[0]["subject"] and "DRAFT, not sent" in slack.sent[0]["message"]


def test_a_pitch_with_an_address_and_smtp_is_emailed_to_the_publisher():
    target = _target("editor.com", email="jane@editor.com")
    pitch = _pitch(target)
    db = _Db()
    db.put(target)
    db.put(pitch)
    smtp = _Notifier({Capability.SEND_EMAIL, Capability.SEND_NOTIFICATION})
    ctx = _ctx(db, connectors={Capability.SEND_EMAIL: smtp, Capability.SEND_NOTIFICATION: smtp})

    assert pr.plan_delivery(ctx, target) == (pr.DELIVERY_PUBLISHER, "jane@editor.com")
    pr._make_sender("p1", delivery=pr.DELIVERY_PUBLISHER, recipient="jane@editor.com")(ctx)
    assert pitch.status == pr.SENT and pitch.sent_at is not None
    assert target.status == BacklinkStatus.PITCHED.value
    assert smtp.sent[0]["channel"] == "jane@editor.com"
    assert smtp.sent[0]["message"].startswith("Hello Jane,")


@pytest.mark.parametrize(
    ("delivery", "recipient"),
    [
        (pr.DELIVERY_TEAM, ""),  # approved as a team draft; address added later
        (None, None),  # legacy approval with no recorded delivery
        (pr.DELIVERY_PUBLISHER, "old@editor.com"),  # address changed since approval
    ],
)
def test_a_send_never_goes_further_than_what_was_approved(delivery, recipient):
    target = _target("editor.com", email="jane@editor.com")
    pitch = _pitch(target)
    db = _Db()
    db.put(target)
    db.put(pitch)
    smtp = _Notifier({Capability.SEND_EMAIL, Capability.SEND_NOTIFICATION})
    ctx = _ctx(db, connectors={Capability.SEND_EMAIL: smtp, Capability.SEND_NOTIFICATION: smtp})
    pr._make_sender("p1", delivery=delivery, recipient=recipient)(ctx)
    assert pitch.status == pr.TEAM_DRAFT
    assert smtp.sent[0]["channel"] == ""


def test_an_address_without_a_mail_connector_is_still_a_team_draft():
    target = _target("editor.com", email="jane@editor.com")
    ctx = _ctx(_Db(), connectors={Capability.SEND_NOTIFICATION: _Notifier({Capability.SEND_NOTIFICATION})})
    assert pr.plan_delivery(ctx, target) == (pr.DELIVERY_TEAM, "")


def test_drafts_whose_approval_lapsed_are_released():
    t = _target()
    waiting, orphan = _pitch(t, pid="p-waiting"), _pitch(t, pid="p-orphan")
    db = _Db(results=[[("p-waiting",)], [waiting, orphan]])
    released = pr.release_stale_drafts(_ctx(db), counter=False)
    assert released == 1
    assert waiting.status == pr.DRAFTED and orphan.status == pr.EXPIRED


def test_team_drafted_targets_wait_for_an_address_before_being_pitched_again():
    no_address = _target("a.example", id="ta")
    with_address = _target("b.example", email="ed@b.example", id="tb")
    fresh = _target("c.example", id="tc")
    db = _Db(
        results=[
            [],  # recently contacted
            [("ta", pr.TEAM_DRAFT), ("tb", pr.TEAM_DRAFT)],
            [no_address, with_address, fresh],
        ]
    )
    smtp = _Notifier({Capability.SEND_EMAIL})
    ctx = _ctx(db, connectors={Capability.SEND_EMAIL: smtp})
    eligible = pr.AGENT._pitchable_targets(ctx)
    assert {t.domain for t in eligible} == {"b.example", "c.example"}


def test_angles_rotate_and_titles_say_where_the_pitch_goes(monkeypatch):
    targets = [
        _target("a.example", id="ta"),
        _target("b.example", email="ed@b.example", id="tb"),
        _target("c.example", id="tc"),
    ]
    monkeypatch.setattr(pr, "release_stale_drafts", lambda ctx, counter=None: 0)
    monkeypatch.setattr(pr.AGENT, "_pitchable_targets", lambda ctx: targets)
    monkeypatch.setattr(pr.AGENT, "_angle_offset", lambda ctx: 1)
    monkeypatch.setattr(pr.AGENT, "_open_draft_count", lambda ctx: 3)
    smtp = _Notifier({Capability.SEND_EMAIL, Capability.SEND_NOTIFICATION})
    ctx = _ctx(
        _Db(),
        scope="winter tyre study, service-cost index",
        connectors={Capability.SEND_EMAIL: smtp, Capability.SEND_NOTIFICATION: smtp},
        answers=[{"subject": f"Data for you {i}", "body": BODY} for i in range(3)],
    )
    result = pr.AGENT.run(ctx)
    assert [a.payload["angle"] for a in result.actions] == [
        "service-cost index",
        "winter tyre study",
        "service-cost index",
    ]
    assert "ANGLE: service-cost index\n" in ctx.asked[0]
    assert "winter tyre study, service-cost index" not in ctx.asked[0]
    team, publisher = result.actions[0], result.actions[1]
    assert team.payload["delivery"] == pr.DELIVERY_TEAM and "to your team" in team.title
    assert "nobody at the publication" in team.payload["delivery_note"]
    assert publisher.payload["delivery"] == pr.DELIVERY_PUBLISHER
    assert publisher.payload["recipient"] == "ed@b.example"
    assert "e***@b.example" in publisher.title
    assert all(a.impact.value == "high" for a in result.actions)
    assert "1 to publishers, 2 as drafts for your team" in result.summary


def test_mask_email():
    assert mask_email("jane@editor.com") == "j***@editor.com"
    assert mask_email("") == "" and mask_email(None) == "" and mask_email("nope") == ""


# ── Off-Page API: contact address ──────────────────────────────────────────
def test_contact_body_validates_and_blank_clears():
    from app.api.v1.offpage import TargetContactIn

    assert TargetContactIn(email="jane@editor.com").email == "jane@editor.com"
    assert TargetContactIn(email="  ").email is None
    assert TargetContactIn(email=None).email is None
    assert TargetContactIn().email is None
    with pytest.raises(ValidationError):
        TargetContactIn(email="not-an-address")


def _current(*, can_write: bool = True):  # noqa: ANN202
    from app.core.exceptions import ForbiddenError

    def require_write(module):  # noqa: ANN001, ANN202
        if not can_write:
            raise ForbiddenError("read only")

    return SimpleNamespace(
        tenant_id="t1",
        user=SimpleNamespace(id="u1", name="Asha", tenant_id="t1"),
        require_write=require_write,
    )


def test_setting_and_clearing_a_contact_is_audited_without_the_address(monkeypatch):
    from app.api.v1 import offpage

    logged: list[dict] = []
    monkeypatch.setattr(offpage.audit, "record_user_action", lambda db, **kw: logged.append(kw))
    target = _target("editor.com", id="tid")
    db = _Db()
    db.put(target)

    out = offpage.set_target_contact(
        "tid", offpage.TargetContactIn(email="jane@editor.com"), _current(), db, "1.2.3.4"
    )
    assert target.contact_email == "jane@editor.com" and db.committed
    assert out.target.has_contact is True
    assert out.target.contact_email == "jane@editor.com"
    assert out.target.contact_email_masked == "j***@editor.com"
    assert "emailed" in out.toast.message
    assert logged[0]["action"] == "added a contact address for editor.com"
    assert "jane" not in json.dumps(logged[0]["context"])

    out = offpage.set_target_contact("tid", offpage.TargetContactIn(email=""), _current(), db, None)
    assert target.contact_email is None and out.target.has_contact is False
    assert logged[1]["action"] == "cleared the contact address for editor.com"


def test_contact_needs_write_access_and_a_target_in_this_workspace(monkeypatch):
    from app.api.v1 import offpage
    from app.core.exceptions import ForbiddenError

    monkeypatch.setattr(offpage.audit, "record_user_action", lambda db, **kw: None)
    foreign = _target("x.example", id="foreign")
    foreign.tenant_id = "someone-else"
    db = _Db()
    db.put(foreign)
    body = offpage.TargetContactIn(email="a@b.example")
    with pytest.raises(ForbiddenError):
        offpage.set_target_contact("foreign", body, _current(can_write=False), db, None)
    with pytest.raises(NotFoundError):
        offpage.set_target_contact("foreign", body, _current(), db, None)
    with pytest.raises(NotFoundError):
        offpage.set_target_contact("missing", body, _current(), db, None)
    assert foreign.contact_email is None


def test_backlink_view_shows_contact_presence_and_unmeasured_authority():
    from app.services.views import backlink_out

    target = _target("editor.com", email="jane@editor.com", authority=0)
    viewer = backlink_out(target, can_write=False)
    assert viewer.has_contact is True
    assert viewer.contact_email is None and viewer.contact_email_masked == "j***@editor.com"
    assert viewer.authority_measured is False
    editor = backlink_out(_target("b.example", authority=55), can_write=True)
    assert editor.has_contact is False and editor.contact_email is None
    assert editor.authority_measured is True


def test_a_counter_pitch_already_drafted_to_the_team_is_not_relaunched(monkeypatch):
    from app.api.v1 import offpage
    from app.core.exceptions import ConflictError

    alert = _alert(counter_pitch_id="cp1")
    db = _Db()
    db.put(alert)
    with pytest.raises(ConflictError, match="contact address"):
        offpage.launch_counter_pitch("alert1", _current(), db, None)
