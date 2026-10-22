"""Connector verification framework: status and expiry, the runner, the
cassette scrubber and strict replay, the CLI, the API guards, the Beta label
and the hide-unverified filter. Pure: no database."""
from __future__ import annotations

import json
import logging
from datetime import timedelta

import httpx
import pytest

from app.connectors.base.cassettes import (
    REDACTED,
    CassetteError,
    CassetteMismatch,
    Recorder,
    Scrubber,
    assert_no_secrets,
    scrub_exchanges,
)
from app.connectors.base.connector import BaseConnector, ConnectorSpec
from app.connectors.base.credentials import Credentials, secret, text
from app.connectors.base.verification import VerifyStep
from app.core.exceptions import ConnectorError
from app.db.base import utcnow
from app.services import verification
from app.services.verification import LatestRun, Snapshot, execute, status_from

NOW = utcnow()


def _run(slug: str, *, ok: bool, days_ago: float, env: str = "test_account", steps=()):  # noqa: ANN001, ANN202
    at = NOW - timedelta(days=days_ago)
    return LatestRun(slug=slug, ok=ok, environment=env, finished_at=at, started_at=at, steps=tuple(steps))


def _install(snap: Snapshot) -> None:
    verification.set_loader(lambda: snap)


# ── status / is_verified ───────────────────────────────────────────────────
def test_never_run_is_unverified():
    st = status_from(Snapshot(), "wordpress", now=NOW)
    assert not st.verified and st.ok is None
    assert st.summary == "Not yet checked against a real account"


def test_a_recent_pass_is_verified():
    snap = Snapshot(latest={"wordpress": _run("wordpress", ok=True, days_ago=2)})
    st = status_from(snap, "wordpress", now=NOW)
    assert st.verified and st.environment == "test_account"
    assert st.summary.startswith("Verified on") and "Test account" in st.summary


def test_a_pass_expires_after_the_window():
    days = verification.VERIFIED_FOR_DAYS
    fresh = Snapshot(latest={"x": _run("x", ok=True, days_ago=days - 0.1)})
    stale = Snapshot(latest={"x": _run("x", ok=True, days_ago=days + 0.1)})
    assert status_from(fresh, "x", now=NOW).verified
    expired = status_from(stale, "x", now=NOW)
    assert not expired.verified and expired.ok is True
    assert "more than 30 days ago" in expired.summary


def test_a_later_failure_unverifies_at_once_and_says_why():
    failing = _run(
        "x",
        ok=False,
        days_ago=0,
        steps=[
            {"key": "a", "ok": True, "skipped": False, "detail": "fine"},
            {"key": "b", "ok": False, "skipped": False, "detail": "Those credentials were rejected."},
        ],
    )
    snap = Snapshot(latest={"x": failing}, last_ok={"x": NOW - timedelta(days=1)})
    st = status_from(snap, "x", now=NOW)
    assert not st.verified and st.ok is False
    assert st.reason == "Those credentials were rejected."
    assert st.last_ok_at is not None


def test_is_verified_reads_the_cached_snapshot():
    calls = []

    def loader() -> Snapshot:
        calls.append(1)
        return Snapshot(latest={"slack": _run("slack", ok=True, days_ago=1)})

    verification.set_loader(loader)
    assert verification.is_verified(None, "slack")
    assert verification.is_verified(object(), "slack")
    assert not verification.is_verified(None, "github")
    assert len(calls) == 1  # cached
    verification.invalidate_cache()
    verification.is_verified(None, "slack")
    assert len(calls) == 2


def test_a_missing_table_reads_unverified_and_warns_once(caplog):  # noqa: ANN001
    def broken() -> Snapshot:
        raise RuntimeError("relation connector_verifications does not exist")

    verification.set_loader(broken)
    verification._warned = False
    with caplog.at_level(logging.WARNING):
        assert not verification.is_verified(None, "slack")
        verification.invalidate_cache()
        assert not verification.is_verified(None, "slack")
    warnings = [r for r in caplog.records if "could not be read" in r.getMessage()]
    assert len(warnings) == 1


# ── The runner ─────────────────────────────────────────────────────────────
class _Fake(BaseConnector):
    spec = ConnectorSpec(
        slug="fake_verify",
        name="Fake",
        category="CMS",
        fields=(text("siteUrl", "Site"), secret("apiKey", "Key")),
    )

    def __init__(self, steps, **values) -> None:  # noqa: ANN001, ANN003
        super().__init__(Credentials(values={"siteUrl": "https://real.example.com", "apiKey": "sk-REAL-KEY-123", **values}))
        self._steps = steps

    def verify_steps(self) -> list:
        return self._steps


def _ok(detail="fine", evidence=None):  # noqa: ANN001, ANN202
    return lambda: (detail, evidence or {})


def _ticker():  # noqa: ANN202
    t = iter(range(1000))
    return lambda: float(next(t))


def test_steps_run_in_order_and_stop_at_the_first_failure():
    order: list[str] = []

    def step(key: str, fail: bool = False):  # noqa: ANN202
        def run():  # noqa: ANN202
            order.append(key)
            if fail:
                raise ConnectorError("Fake: Those credentials were rejected.")
            return f"{key} ok", {}

        return VerifyStep(key=key, label=key, kind="read", run=run)

    report = execute(_Fake([step("a"), step("b", fail=True), step("c")]), clock=_ticker())
    assert order == ["a", "b"]
    assert [(s.key, s.ok, s.skipped) for s in report.steps] == [
        ("a", True, False),
        ("b", False, False),
        ("c", False, True),
    ]
    assert report.steps[1].detail == "Fake: Those credentials were rejected."
    assert report.steps[2].detail == verification.SKIP_AFTER_FAILURE
    assert report.steps[0].seconds == 1.0
    assert not report.ok


def test_write_steps_need_allow_writes():
    write = VerifyStep(key="w", label="w", kind="write", run=_ok(), requires_test_account=False)
    read = VerifyStep(key="r", label="r", kind="read", run=_ok())
    report = execute(_Fake([read, write]))
    assert report.ok
    assert report.steps[1].skipped and report.steps[1].detail == verification.SKIP_NO_WRITES
    assert verification.SKIP_NO_WRITES in report.note
    assert not execute(_Fake([read, write]), allow_writes=True).steps[1].skipped


def test_test_account_writes_are_skipped_on_a_live_connection():
    write = VerifyStep(key="w", label="w", kind="write", run=_ok(), requires_test_account=True)
    live = execute(_Fake([write]), allow_writes=True)
    assert live.steps[0].skipped and live.steps[0].detail == verification.SKIP_LIVE_ACCOUNT
    assert not live.ok and "Nothing was checked" in live.note

    marked = execute(_Fake([write], accountEnvironment="sandbox"), allow_writes=True)
    assert marked.ok and marked.environment == "sandbox" and not marked.steps[0].skipped


def test_the_environment_override_gates_writes_and_reaches_the_connector():
    seen = {}

    def run():  # noqa: ANN202
        seen["env"] = connector.account_environment()
        return "made and removed", {}

    connector = _Fake([VerifyStep(key="w", label="w", kind="write", run=run, requires_test_account=True)])
    report = execute(connector, allow_writes=True, environment="test_account")
    assert report.ok and report.environment == "test_account"
    assert seen["env"] == "test_account"
    with pytest.raises(ValueError):
        execute(_Fake([]), environment="staging")


def test_details_and_evidence_never_carry_credential_values():
    step = VerifyStep(
        key="r",
        label="r",
        kind="read",
        run=_ok(
            "Read https://real.example.com with sk-REAL-KEY-123",
            {"echo": "sk-REAL-KEY-123", "nested": [{"site": "https://real.example.com/a"}]},
        ),
    )
    report = execute(_Fake([step]))
    blob = json.dumps([report.steps[0].detail, report.steps[0].evidence])
    assert "sk-REAL-KEY-123" not in blob and "real.example.com" not in blob
    assert "[hidden]" in blob and "[siteUrl]" in blob


def test_a_failure_message_is_softened_and_scrubbed():
    def boom():  # noqa: ANN202
        raise RuntimeError("Traceback: HTTP/1.1 500 at 0xdeadbeef sk-REAL-KEY-123")

    report = execute(_Fake([VerifyStep(key="r", label="r", kind="read", run=boom)]))
    detail = report.steps[0].detail
    assert "sk-REAL-KEY-123" not in detail and "Traceback" not in detail
    assert detail


def test_no_steps_is_reported_plainly():
    report = execute(_Fake([]))
    assert not report.ok and not report.steps
    assert report.note == "No live check has been written for this connector yet."


def test_a_missing_opt_in_is_skipped_not_failed():
    from app.connectors.base.verification import VerifySkipped

    def paid():  # noqa: ANN202
        raise VerifySkipped("Posting costs money on X. Set verifyAllowPaidPost=yes to run it.")

    later = VerifyStep(key="c", label="c", kind="read", run=_ok("still ran"))
    report = execute(
        _Fake([VerifyStep(key="a", label="a", kind="read", run=_ok()),
               VerifyStep(key="p", label="p", kind="read", run=paid), later])
    )
    assert report.ok
    assert report.steps[1].skipped and not report.steps[1].ok
    assert "verifyAllowPaidPost" in report.steps[1].detail and "verifyAllowPaidPost" in report.note
    assert report.steps[2].ok and report.steps[2].detail == "still ran"


def test_run_only_options_reach_the_connector_in_memory():
    seen = {}

    def run():  # noqa: ANN202
        seen["img"] = connector.credentials.get("verifyImageUrl")
        return "ok", {}

    connector = _Fake([VerifyStep(key="r", label="r", kind="read", run=run)])
    report = execute(connector, options={"verifyImageUrl": " https://img.example.org/a.png "})
    assert report.ok and seen["img"] == "https://img.example.org/a.png"


@pytest.mark.parametrize("key", ["apiKey", "siteUrl", "verify", "verify-x", "", "accountEnvironment"])
def test_only_verify_options_are_accepted(key: str):
    with pytest.raises(ValueError):
        verification.clean_options({key: "x"})
    with pytest.raises(ValueError):
        execute(_Fake([]), options={key: "x"})


def test_option_values_are_bounded():
    assert verification.clean_options({"verifyAllowPaidPost": "yes"}) == {"verifyAllowPaidPost": "yes"}
    with pytest.raises(ValueError):
        verification.clean_options({"verifyImageUrl": "x" * 5000})


def test_site_crawler_has_a_working_read_step():
    from app.connectors.site_crawler.connector import SiteCrawlerConnector

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/"
        return httpx.Response(
            200,
            text="<html><head><title>Home</title></head><body><h1>Hi</h1>"
            + "<p>" + "word " * 200 + "</p><a href='/about'>About</a></body></html>",
            headers={"content-type": "text/html"},
        )

    crawler = SiteCrawlerConnector(Credentials(values={"siteUrl": "https://shop.example.org"}))
    crawler._client = httpx.Client(base_url=crawler.base_url, transport=httpx.MockTransport(handler))
    report = execute(crawler)
    assert report.ok, report
    assert report.steps[0].key == "fetch_home_page"
    assert "words" in report.steps[0].detail
    assert verification.has_steps("site_crawler")


# ── Cassettes: scrubbing ───────────────────────────────────────────────────
def test_credentials_become_consistent_fakes():
    s = Scrubber.for_credentials(
        {
            "siteUrl": "https://realco.com/shop",
            "apiKey": "sk-live-abcdef",
            "accountId": "123456789012",
            "fromEmail": "owner@realco.com",
            "discovery": "auto",
            "sitemapPath": "/sitemap.xml",
            "accountEnvironment": "test_account",
            "storeHost": "realco.myshopify.com",
        },
        secret_keys={"apiKey"},
    )
    fake = s.fake_credentials
    assert fake["apiKey"] == "test-apiKey"
    assert fake["siteUrl"] == "https://siteurl.example.test/shop"
    assert fake["accountId"].isdigit() and len(fake["accountId"]) == 12 and fake["accountId"] != "123456789012"
    assert fake["fromEmail"] == "fromemail@example.test"
    assert fake["storeHost"] == "storehost.example.test"
    # Not account data: kept.
    assert fake["discovery"] == "auto" and fake["sitemapPath"] == "/sitemap.xml"
    assert fake["accountEnvironment"] == "test_account"
    assert s.text("see realco.com and sk-live-abcdef") == "see siteurl.example.test and test-apiKey"


def test_secret_query_parameters_are_redacted():
    s = Scrubber.for_credentials({"apiKey": "AIzaREALKEY1"}, secret_keys={"apiKey"})
    url = s.url(
        "https://vendor.test/v1/x?key=AIzaREALKEY1&access_token=ya29.other&token=t0k"
        "&client_secret=shh&code=4/abc&page=2"
    )
    assert "AIzaREALKEY1" not in url and "ya29.other" not in url and "shh" not in url
    assert "key=test-apiKey" in url  # a credential's fake, so a replay matches
    assert f"access_token={REDACTED}" in url and f"token={REDACTED}" in url
    assert f"client_secret={REDACTED}" in url and f"code={REDACTED}" in url
    assert "page=2" in url


def test_json_secrets_emails_and_vendor_ids_are_scrubbed_consistently():
    s = Scrubber()
    s.collect_ids('{"id": "120330000123456"}')
    body = s.json(
        {
            "access_token": "ya29.real",
            "refreshToken": "1//real",
            "password": "hunter2",
            "user": {"email": "jane.doe@corp.com", "note": "mail jane.doe@corp.com"},
            "campaign": "act_120330000123456",
            "count": 123456789999,
        }
    )
    assert body["access_token"] == REDACTED and body["refreshToken"] == REDACTED
    assert body["password"] == REDACTED
    assert body["user"]["email"] == "user1@example.test"
    assert body["user"]["note"] == "mail user1@example.test"
    fake_id = body["campaign"].removeprefix("act_")
    assert fake_id != "120330000123456" and len(fake_id) == 15
    assert s.url("https://x.test/campaigns/120330000123456") == f"https://x.test/campaigns/{fake_id}"
    assert body["count"] == 123456789999  # a number the vendor did not send as an id string stays


def test_auth_headers_and_cookies_are_never_recorded():
    request = httpx.Request(
        "POST",
        "https://vendor.test/api?api_key=raw",
        headers={
            "Authorization": "Bearer sk-secret",
            "X-Api-Key": "sk-secret",
            "Cookie": "session=abc",
            "Content-Type": "application/json",
        },
        json={"name": "probe"},
    )
    response = httpx.Response(
        200,
        headers={"Set-Cookie": "session=xyz", "Content-Type": "application/json", "X-Token": "t"},
        json={"ok": True},
        request=request,
    )
    out = scrub_exchanges({"s": [(request, response)]}, Scrubber())
    blob = json.dumps(out)
    assert "sk-secret" not in blob and "session=" not in blob and "Bearer" not in blob
    assert out["s"][0]["request"]["headers"] == {"content-type": "application/json"}
    assert out["s"][0]["response"]["headers"] == {"content-type": "application/json"}
    assert "api_key=REDACTED" in out["s"][0]["request"]["url"]


def test_a_secret_left_behind_stops_the_write():
    s = Scrubber.for_credentials({"apiKey": "sk-live-abcdef"}, secret_keys={"apiKey"})
    with pytest.raises(CassetteError):
        assert_no_secrets('{"x": "sk-live-abcdef"}', s)


# ── Cassettes: record, then replay strictly ────────────────────────────────
VENDOR_CREDS = {
    "siteUrl": "https://realco.com",
    "apiKey": "sk-live-SECRET123",
    "accountId": "123456789012",
}


def _vendor(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if request.method == "GET" and path == "/accounts/123456789012":
        return httpx.Response(
            200,
            json={
                "id": "123456789012",
                "owner": "boss@realco.com",
                "access_token": "tok-session-1",
                "home": "https://realco.com/x",
            },
        )
    if request.method == "POST" and path == "/accounts/123456789012/campaigns":
        return httpx.Response(200, json={"id": "987654321000", "status": "PAUSED"})
    if request.method == "DELETE" and path == "/campaigns/987654321000":
        return httpx.Response(204)
    return httpx.Response(404, json={"error": "nope"})


class _Echo(BaseConnector):
    spec = ConnectorSpec(
        slug="echo_vendor",
        name="Echo",
        category="Ad Platforms",
        fields=(text("siteUrl", "Site"), secret("apiKey", "Key"), text("accountId", "Account")),
        base_url_field="siteUrl",
    )
    transport: httpx.BaseTransport | None = None
    campaign_name = "probe"
    extra_call = False

    def _http(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(base_url=self.base_url, transport=self.transport)
        return self._client

    def auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.credentials.get('apiKey')}"}

    def verify_steps(self) -> list:
        account = self.credentials.get("accountId")
        state: dict = {}

        def read() -> tuple[str, dict]:
            body = self.request("GET", f"/accounts/{account}", params={"key": self.credentials.get("apiKey")})
            state["token"] = body["access_token"]
            return f"Read account {body['id']}", {"id": body["id"]}

        def write() -> tuple[str, dict]:
            made = self.request(
                "POST",
                f"/accounts/{account}/campaigns",
                params={"access_token": state["token"]},
                json_body={"name": self.campaign_name},
            )
            self.request("DELETE", f"/campaigns/{made['id']}", expect_json=False)
            if self.extra_call:
                self.request("GET", "/surprise")
            return "Created and removed a paused campaign", {"campaign": made["id"]}

        return [
            VerifyStep(key="read_account", label="Read the account", kind="read", run=read),
            VerifyStep(
                key="paused_campaign", label="Create and remove a paused campaign",
                kind="write", run=write, requires_test_account=True,
            ),
        ]


def _record(tmp_path) -> list:  # noqa: ANN001
    class Live(_Echo):
        transport = httpx.MockTransport(_vendor)

    connector = Live(Credentials(values=dict(VENDOR_CREDS)))
    recorder = Recorder()
    with recorder.active():
        report = execute(connector, allow_writes=True, environment="test_account", recorder=recorder)
    assert report.ok, report
    return verification._write_cassettes(connector, report, recorder, tmp_path)


def test_a_recording_is_scrubbed(tmp_path):  # noqa: ANN001
    paths = _record(tmp_path)
    folder = tmp_path / "echo_vendor"
    assert {p.split("\\")[-1].split("/")[-1] for p in paths} == {
        "_meta.json", "read_account.json", "paused_campaign.json",
    }
    blob = "".join(p.read_text(encoding="utf-8") for p in folder.glob("*.json"))
    for real in (
        "sk-live-SECRET123", "realco.com", "boss@realco.com", "123456789012",
        "987654321000", "tok-session-1", "Bearer",
    ):
        assert real not in blob, real
    meta = json.loads((folder / "_meta.json").read_text(encoding="utf-8"))
    assert meta["steps"] == ["read_account", "paused_campaign"]
    assert meta["environment"] == "test_account"
    assert meta["credentials"]["apiKey"] == "test-apiKey"
    assert meta["credentials"]["siteUrl"] == "https://siteurl.example.test"
    step = json.loads((folder / "paused_campaign.json").read_text(encoding="utf-8"))
    created = step["interactions"][0]["response"]["body"]["json"]["id"]
    # The id the vendor returned is the same fake in the DELETE that follows.
    assert step["interactions"][1]["request"]["url"].endswith(f"/campaigns/{created}")


def test_a_recording_replays_offline(tmp_path):  # noqa: ANN001
    from tests.support.cassettes import replay

    _record(tmp_path)
    result = replay(tmp_path / "echo_vendor", connector_class=_Echo)
    result.assert_ok()
    assert [s.key for s in result.steps] == ["read_account", "paused_campaign"]


def test_replay_fails_on_a_changed_request(tmp_path):  # noqa: ANN001
    from tests.support.cassettes import replay

    _record(tmp_path)

    class Drifted(_Echo):
        campaign_name = "renamed"

    with pytest.raises(CassetteMismatch, match="differs from the recording"):
        replay(tmp_path / "echo_vendor", connector_class=Drifted)


def test_replay_fails_on_an_unexpected_request(tmp_path):  # noqa: ANN001
    from tests.support.cassettes import replay

    _record(tmp_path)

    class Extra(_Echo):
        extra_call = True

    with pytest.raises(CassetteMismatch, match="Unexpected request"):
        replay(tmp_path / "echo_vendor", connector_class=Extra)


def test_replay_fails_when_a_recorded_call_is_no_longer_made(tmp_path):  # noqa: ANN001
    from tests.support.cassettes import replay

    _record(tmp_path)
    step_file = tmp_path / "echo_vendor" / "read_account.json"
    data = json.loads(step_file.read_text(encoding="utf-8"))
    data["interactions"].append(data["interactions"][0])
    step_file.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(CassetteMismatch, match="stopped making a call"):
        replay(tmp_path / "echo_vendor", connector_class=_Echo)


def test_replay_never_reaches_the_network(tmp_path, monkeypatch):  # noqa: ANN001
    from tests.support.cassettes import replay

    _record(tmp_path)

    def no_network(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        raise AssertionError("network used during replay")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", no_network)
    replay(tmp_path / "echo_vendor", connector_class=_Echo).assert_ok()


# ── CLI ────────────────────────────────────────────────────────────────────
def _parse(*argv: str):  # noqa: ANN202
    import manage

    return manage.build_parser().parse_args(list(argv))


def test_cli_parses_verify():
    args = _parse("verify", "wordpress", "--workspace", "acme", "--allow-writes", "--record",
                  "--environment", "test_account")
    assert (args.slug, args.workspace, args.allow_writes, args.record, args.environment, args.all) == (
        "wordpress", "acme", True, True, "test_account", False,
    )
    opts = _parse("verify", "x_twitter", "--workspace", "a", "--set", "verifyAllowPaidPost=yes",
                  "--set", "verifyImageUrl=https://i.example.org/a.png?x=1")
    assert opts.set == ["verifyAllowPaidPost=yes", "verifyImageUrl=https://i.example.org/a.png?x=1"]
    every = _parse("verify", "--all", "--workspace", "acme")
    assert every.all and every.slug == "" and not every.allow_writes


def test_cli_requires_a_workspace_and_a_known_environment():
    with pytest.raises(SystemExit):
        _parse("verify", "wordpress")
    with pytest.raises(SystemExit):
        _parse("verify", "wordpress", "--workspace", "a", "--environment", "staging")


@pytest.mark.parametrize(
    "argv",
    [
        ("verify", "--workspace", "acme"),
        ("verify", "wordpress", "--all", "--workspace", "acme"),
        ("verify", "no_such_connector", "--workspace", "acme"),
        ("verify", "wordpress", "--workspace", "acme", "--set", "apiKey=steal"),
        ("verify", "wordpress", "--workspace", "acme", "--set", "verifyImageUrl"),
    ],
)
def test_cli_rejects_bad_targets_before_touching_the_database(argv, capsys):  # noqa: ANN001
    import manage

    args = _parse(*argv)
    assert manage.cmd_verify(args) == 2
    assert capsys.readouterr().err.strip()


# ── API guards ─────────────────────────────────────────────────────────────
def _dependency_calls(dependant) -> set:  # noqa: ANN001
    calls = set()
    for dep in dependant.dependencies:
        calls.add(dep.call)
        calls |= _dependency_calls(dep)
    return calls


def test_every_verification_route_is_portal_admin_only():
    from app.api.deps import require_portal_admin
    from app.api.v1 import verification as routes

    found = {(next(iter(r.methods)), r.path) for r in routes.router.routes}
    assert found == {
        ("GET", "/portal/verification"),
        ("GET", "/portal/verification/{slug}/workspaces"),
        ("POST", "/portal/verification/{slug}/run"),
        ("PUT", "/portal/verification/settings"),
    }
    for route in routes.router.routes:
        assert require_portal_admin in _dependency_calls(route.dependant), route.path


def test_the_list_row_states():
    from app.api.v1.verification import _row
    from app.connectors.base import registry

    spec = registry.get_spec("site_crawler")
    never = _row(spec, Snapshot(), {})
    assert (never.state, never.summary, never.has_steps) == ("never", "Never checked", True)
    passed = _row(spec, Snapshot(latest={"site_crawler": _run("site_crawler", ok=True, days_ago=1)}), {})
    assert passed.state == "verified" and passed.verified and passed.latest is not None
    failed = _row(spec, Snapshot(latest={"site_crawler": _run("site_crawler", ok=False, days_ago=1)}), {})
    assert failed.state == "failed" and not failed.verified


# ── Beta label ─────────────────────────────────────────────────────────────
def _connector_out(slug: str):  # noqa: ANN202
    from app.models.connector import ConnectorRecord
    from app.services.views import connector_out

    record = ConnectorRecord(slug=slug, name=slug, category="CMS", credential_hints={}, connected=False, health="unknown", last_error="")
    return connector_out(record, can_write=True)


def test_unverified_connectors_are_labelled_beta():
    out = _connector_out("site_crawler")
    assert out.verification.verified is False
    assert out.verification.label == "Beta"
    assert out.verification.tooltip == (
        "Not yet confirmed against a live account. It may not work as expected."
    )


def test_verified_connectors_show_nothing_extra():
    _install(Snapshot(latest={"site_crawler": _run("site_crawler", ok=True, days_ago=1)}))
    out = _connector_out("site_crawler")
    assert out.verification.verified is True and out.verification.label == ""


# ── Hide unverified ────────────────────────────────────────────────────────
@pytest.fixture
def no_flags(monkeypatch):  # noqa: ANN001, ANN201
    from app.services import portal_features

    monkeypatch.setattr(portal_features, "_load_disabled", lambda db: {})
    portal_features.invalidate_cache()
    yield
    portal_features.invalidate_cache()


def test_nothing_is_hidden_by_default(no_flags):  # noqa: ANN001
    from app.connectors.base import registry
    from app.services import portal_features

    assert portal_features.hidden_connector_slugs(None) == frozenset()
    assert portal_features.offered_connector_slugs(None) == frozenset(
        s.slug for s in registry.all_specs()
    )


def test_hiding_unverified_keeps_verified_and_connected_ones(no_flags):  # noqa: ANN001
    from app.services import portal_features

    _install(
        Snapshot(
            latest={"site_crawler": _run("site_crawler", ok=True, days_ago=1)},
            settings={verification.HIDE_UNVERIFIED_KEY: True},
        )
    )
    hidden = portal_features.hidden_connector_slugs(None)
    assert "site_crawler" not in hidden and "wordpress" in hidden
    offered = portal_features.offered_connector_slugs(None, connected={"wordpress"})
    assert "site_crawler" in offered and "wordpress" in offered
    assert "shopify" not in offered


def test_hidden_connectors_leave_the_workspace_list_unless_connected(no_flags):  # noqa: ANN001
    from types import SimpleNamespace

    from app.services import connectors as connector_service

    _install(Snapshot(settings={verification.HIDE_UNVERIFIED_KEY: True}))
    rows = [
        SimpleNamespace(slug="wordpress", connected=True, health="ok", name="WordPress"),
        SimpleNamespace(slug="shopify", connected=False, health="unknown", name="Shopify"),
    ]

    class DB:
        def execute(self, *_):  # noqa: ANN002, ANN202
            return SimpleNamespace(scalars=lambda: iter(rows))

    kept = connector_service.list_records(DB(), tenant_id="t")
    assert [r.slug for r in kept] == ["wordpress"]
