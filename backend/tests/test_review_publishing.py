"""Review fixes (gate 4, group G3a): approvals, designs and social publishing.

* A design records the render and preview that were approved; sharing uses
  only those, and refuses a design that has changed since.
* Approving obeys the workspace stop as well as the platform's.
* Approving a due social post goes through the scheduled publisher (stops,
  daily limit, waits), which reads the stop again before every post and
  re-reads each claimed row under a lock before sending it.
* Waits the platform asked for, retries and our own spacing are counted apart.
* Rejecting looks the asset up only in the caller's workspace.
* Facebook and Instagram insights stop on 429 and 5xx instead of skipping.
* The social publisher closes every connector it opened.
* The content library hands the browser only https post links.

Everything here runs on stand-ins; no database is needed.
"""
from __future__ import annotations

import hashlib
import json
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from app.core.exceptions import ConflictError, ConnectorConfigError, ConnectorError
from app.db.base import utcnow
from app.models.content import AssetKind, AssetStatus, MarketingAsset
from app.models.creative import FileKind, FileSource, StoredFile
from app.services import agent_safety, assets, brand_kits, social_schedule
from tests.conftest import requires_db
from tests.test_social_schedule import ORG, _Cipher, _due, _params, _PubSession, _sql, _Store

TENANT = "00000000-0000-4000-8000-00000000000a"
OTHER = "00000000-0000-4000-8000-00000000000b"
RENDER = "aaaaaaaa-0000-4000-8000-000000000001"
PREVIEW = "aaaaaaaa-0000-4000-8000-000000000002"
NEWER = "aaaaaaaa-0000-4000-8000-000000000003"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
PNG_SHA = hashlib.sha256(PNG).hexdigest()


def _file(file_id: str, *, mime: str = "image/png", sha: str = PNG_SHA, kind: str = FileKind.RENDER) -> StoredFile:
    return StoredFile(
        id=file_id, tenant_id=TENANT, kind=kind, source=FileSource.RENDERED, mime=mime,
        size_bytes=len(PNG), width=1080, height=1080, sha256=sha,
    )


def _design(status: str = AssetStatus.DRAFT, **detail: Any) -> MarketingAsset:  # noqa: ANN401
    return MarketingAsset(
        id="cccccccc-0000-4000-8000-000000000001", tenant_id=TENANT, agent_slug="creative_studio",
        kind=AssetKind.CREATIVE, channel="", title="Spring sale", content_encrypted="{}", status=status,
        detail={"render_file_id": RENDER, "preview_file_id": PREVIEW, **detail}, image_ids=[],
    )


def _files(monkeypatch: pytest.MonkeyPatch, files: dict[str, StoredFile]) -> list[tuple]:
    """``brand_kits.get_file`` answering only for this workspace's files."""
    seen: list[tuple] = []

    def get_file(_db: Any, tenant_id: str, file_id: str, kinds: Any = None) -> StoredFile | None:  # noqa: ANN401
        seen.append((tenant_id, file_id))
        return files.get(file_id) if tenant_id == TENANT else None

    monkeypatch.setattr(brand_kits, "get_file", get_file)
    return seen


_FLUSH_DB = SimpleNamespace(flush=lambda: None)


# ── Approving a design records what was approved ───────────────────────────
def test_approving_a_design_records_its_render_preview_and_digest(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _files(monkeypatch, {RENDER: _file(RENDER)})
    design = _design()
    assets.release(_FLUSH_DB, SimpleNamespace(id=TENANT), design, _Cipher())
    assert design.status == AssetStatus.APPROVED
    assert design.detail["approved_render_file_id"] == RENDER  # the key creative downloads read
    assert design.detail[assets.APPROVED_PREVIEW_KEY] == PREVIEW
    assert design.detail[assets.APPROVED_RENDER_SHA_KEY] == PNG_SHA
    assert seen == [(TENANT, RENDER)]  # looked up in the approving workspace only


def test_a_render_that_is_not_this_workspaces_is_not_recorded_as_approved(monkeypatch: pytest.MonkeyPatch) -> None:
    _files(monkeypatch, {})  # the id names another workspace's file, or none
    design = _design()
    assets.release(_FLUSH_DB, SimpleNamespace(id=TENANT), design, _Cipher())
    assert design.status == AssetStatus.APPROVED
    assert design.detail[assets.APPROVED_RENDER_KEY] is None
    with pytest.raises(ConflictError, match="approved version"):
        social_schedule._design_file(_FLUSH_DB, TENANT, design)


# ── Sharing uses only the approved picture ─────────────────────────────────
def _approved(**detail: Any) -> MarketingAsset:  # noqa: ANN401
    base = {
        assets.APPROVED_RENDER_KEY: RENDER,
        assets.APPROVED_PREVIEW_KEY: PREVIEW,
        assets.APPROVED_RENDER_SHA_KEY: PNG_SHA,
    }
    return _design(AssetStatus.APPROVED, **{**base, **detail})


def test_the_approved_render_is_the_one_shared(monkeypatch: pytest.MonkeyPatch) -> None:
    _files(monkeypatch, {RENDER: _file(RENDER), PREVIEW: _file(PREVIEW, kind=FileKind.PREVIEW)})
    assert social_schedule._design_file(_FLUSH_DB, TENANT, _approved()).id == RENDER


def test_a_render_swapped_after_approval_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    # A restore of an older version on an approved design changes render_file_id.
    _files(monkeypatch, {RENDER: _file(RENDER), NEWER: _file(NEWER)})
    design = _approved(render_file_id=NEWER)
    with pytest.raises(ConflictError, match="changed since it was approved"):
        social_schedule._design_file(_FLUSH_DB, TENANT, design)


def test_a_render_whose_bytes_differ_from_the_approved_digest_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    _files(monkeypatch, {RENDER: _file(RENDER, sha="0" * 64)})
    with pytest.raises(ConflictError, match="changed since it was approved"):
        social_schedule._design_file(_FLUSH_DB, TENANT, _approved())


def test_a_design_with_no_recorded_approval_is_refused_even_with_a_picture(monkeypatch: pytest.MonkeyPatch) -> None:
    _files(monkeypatch, {RENDER: _file(RENDER), PREVIEW: _file(PREVIEW)})
    with pytest.raises(ConflictError, match="approved version"):
        social_schedule._design_file(_FLUSH_DB, TENANT, _design(AssetStatus.APPROVED))


def test_a_print_design_shares_its_approved_preview_but_not_a_changed_one(monkeypatch: pytest.MonkeyPatch) -> None:
    pdf = _file(RENDER, mime="application/pdf")
    _files(monkeypatch, {RENDER: pdf, PREVIEW: _file(PREVIEW, kind=FileKind.PREVIEW), NEWER: _file(NEWER)})
    assert social_schedule._design_file(_FLUSH_DB, TENANT, _approved()).id == PREVIEW
    with pytest.raises(ConflictError, match="changed since it was approved"):
        social_schedule._design_file(_FLUSH_DB, TENANT, _approved(preview_file_id=NEWER))


# ── Approving obeys the workspace stop ─────────────────────────────────────
def _approve_env(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    from app.models.approval import ApprovalStatus
    from app.services import approvals, audit, metrics

    state = SimpleNamespace(applied=[])
    item = SimpleNamespace(
        id="i1", status=ApprovalStatus.PENDING.value, target_kind="review_test", type="Post",
        title="T", agent_slug="a", apply_error="",
    )
    monkeypatch.setattr(approvals, "get_item", lambda db, **k: item)
    monkeypatch.setattr(approvals, "read_payload", lambda org, it: {})
    monkeypatch.setitem(approvals.APPLIERS, "review_test", lambda db, org, it, payload: state.applied.append(it.id))
    monkeypatch.setattr(metrics, "accumulate", lambda *a, **k: None)
    monkeypatch.setattr(audit, "record_user_action", lambda *a, **k: None)
    state.item = item
    return state


def _approve() -> Any:  # noqa: ANN401
    from app.services import approvals

    actor = SimpleNamespace(id="u1", name="Asha")
    return approvals.approve(_FLUSH_DB, org=SimpleNamespace(id="t", settings={}), actor=actor, item_id="i1")


def test_approving_waits_while_the_workspace_is_stopped(monkeypatch: pytest.MonkeyPatch) -> None:
    state = _approve_env(monkeypatch)
    monkeypatch.setattr(agent_safety, "halt_reason", lambda db, org: agent_safety.WORKSPACE_STOP_REASON)
    with pytest.raises(ConflictError, match="workspace are stopped"):
        _approve()
    assert state.applied == [] and state.item.status == "pending"


def test_approving_waits_during_the_platform_stop(monkeypatch: pytest.MonkeyPatch) -> None:
    state = _approve_env(monkeypatch)
    monkeypatch.setattr(agent_safety, "halt_reason", lambda db, org: agent_safety.PLATFORM_STOP_REASON)
    with pytest.raises(ConflictError, match="Emergency stop"):
        _approve()
    assert state.applied == []


def test_approving_applies_when_nothing_is_stopped(monkeypatch: pytest.MonkeyPatch) -> None:
    state = _approve_env(monkeypatch)
    monkeypatch.setattr(agent_safety, "halt_reason", lambda db, org: "")
    assert _approve().status == "approved" and state.applied == ["i1"]


# ── Approving a due social post goes through the publisher ─────────────────
@pytest.fixture
def social(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    state = SimpleNamespace(calls=[], error=None, halts=[], hook=None)

    def publish(db: Any, org: Any, asset: MarketingAsset, content: dict) -> None:  # noqa: ANN401
        state.calls.append(asset.id)
        if state.hook is not None:
            state.hook(asset)
        if state.error is not None:
            raise state.error

    def halt_reason(db: Any, org: Any) -> str:  # noqa: ANN401
        return state.halts.pop(0) if state.halts else ""

    monkeypatch.setitem(assets.PUBLISHERS, AssetKind.SOCIAL_POST, publish)
    monkeypatch.setattr(assets, "OrgCipher", SimpleNamespace(for_org=lambda org: _Cipher()))
    monkeypatch.setattr(agent_safety, "halt_reason", halt_reason)
    return state


def _approvable(n: int = 1) -> tuple[list[MarketingAsset], _PubSession]:
    rows = _due(n)
    for row in rows:
        row.status = AssetStatus.DRAFT
    session = _PubSession(_Store(rows))
    for row in rows:
        session.touched[row.id] = row  # loaded by this request
    return rows, session


def test_approving_a_due_post_publishes_it_through_the_scheduled_publisher(social: SimpleNamespace) -> None:
    rows, session = _approvable()
    assets.release(session, ORG, rows[0], _Cipher())
    assert rows[0].status == AssetStatus.PUBLISHED and social.calls == ["post0"]
    claim = next(s for s in session.statements if "scheduled_for <=" in _sql(s))
    assert "marketing_assets.id IN" in _sql(claim)  # only this post


def test_approving_a_due_post_respects_the_daily_limit(social: SimpleNamespace) -> None:
    rows, session = _approvable()
    sent = _due(1)
    sent[0].id, sent[0].status, sent[0].published_at = "sent0", AssetStatus.PUBLISHED, utcnow() - timedelta(hours=1)
    session.store.rows.append(sent[0])
    session.store.committed["sent0"] = AssetStatus.PUBLISHED
    org = SimpleNamespace(id="t", settings={assets.DAILY_CAP_SETTING: 1})
    assets.release(session, org, rows[0], _Cipher())
    assert social.calls == [] and rows[0].status == AssetStatus.SCHEDULED
    assert "limit of 1 posts a day" in rows[0].detail["publish_note"]


def test_approving_a_due_post_while_stopped_leaves_it_scheduled_unsent(social: SimpleNamespace) -> None:
    social.halts = [agent_safety.WORKSPACE_STOP_REASON]
    rows, session = _approvable()
    assets.release(session, ORG, rows[0], _Cipher())
    assert social.calls == [] and rows[0].status == AssetStatus.SCHEDULED
    assert rows[0].scheduled_for <= utcnow()  # still due: it goes once resumed


def test_approving_a_post_the_platform_refuses_reports_the_reason(social: SimpleNamespace) -> None:
    social.error = ConnectorConfigError("Reconnect Reddit to post.")
    rows, session = _approvable()
    with pytest.raises(ConflictError, match="Reconnect Reddit"):
        assets.release(session, ORG, rows[0], _Cipher())
    assert rows[0].status == AssetStatus.FAILED


# ── The stop and the row are read again before every send ──────────────────
def test_a_stop_pressed_mid_pass_holds_the_remaining_posts(social: SimpleNamespace) -> None:
    rows = _due(3)
    # Clear at the start and before the first post; stopped before the second.
    social.halts = ["", "", agent_safety.WORKSPACE_STOP_REASON]
    store = _Store(rows)
    assert assets.publish_due(_PubSession(store), ORG) == 1
    assert social.calls == ["post0"]
    assert [store.committed[r.id] for r in rows] == [
        AssetStatus.PUBLISHED, AssetStatus.SCHEDULED, AssetStatus.SCHEDULED,
    ]
    assert "Agents were stopped" in rows[1].detail["publish_note"]
    assert "publish_attempts" not in rows[1].detail and "deferrals" not in rows[1].detail


def test_a_claimed_post_marked_interrupted_meanwhile_is_not_sent(social: SimpleNamespace) -> None:
    rows = _due(2)

    def mark_other(asset: MarketingAsset) -> None:
        # Another publisher fails post1 as interrupted while post0 is sending.
        if asset.id == "post0":
            rows[1].status = AssetStatus.FAILED

    social.hook = mark_other
    session = _PubSession(_Store(rows))
    assert assets.publish_due(session, ORG) == 1
    assert social.calls == ["post0"] and rows[1].status == AssetStatus.FAILED
    assert session.relocks == ["post0", "post1"]
    relock = next(s for s in session.statements if "FOR UPDATE" in _sql(s) and "SKIP LOCKED" not in _sql(s))
    assert "marketing_assets.tenant_id = " in _sql(relock) and _params(relock)["tenant_id_1"] == "t"


# ── Separate counters ──────────────────────────────────────────────────────
def test_a_retry_is_not_counted_as_a_deferral(social: SimpleNamespace) -> None:
    social.error = ConnectorError("Reddit could not be reached.")
    rows = _due()
    assets.publish_due(_PubSession(_Store(rows)), ORG)
    assert rows[0].status == AssetStatus.SCHEDULED
    assert rows[0].detail["publish_attempts"] == 1 and "deferrals" not in rows[0].detail


def test_our_own_spacing_is_not_counted_and_never_fails_a_post(social: SimpleNamespace) -> None:
    from app.connectors.reddit.connector import RedditRateLimited

    error = RedditRateLimited("Posts to r/x are kept 10 minutes apart.", retry_after=300)
    error.details[assets.SPACING_DETAIL] = True
    social.error = error
    rows = _due(deferrals=assets.MAX_DEFERRALS)
    assets.publish_due(_PubSession(_Store(rows)), ORG)
    assert rows[0].status == AssetStatus.SCHEDULED
    assert rows[0].detail["deferrals"] == assets.MAX_DEFERRALS  # unchanged


def test_the_platforms_own_wait_is_still_counted_and_capped(social: SimpleNamespace) -> None:
    from app.connectors.reddit.connector import RedditRateLimited

    social.error = RedditRateLimited("Slow down.", retry_after=60)
    rows = _due(deferrals=assets.MAX_DEFERRALS - 1)
    store = _Store(rows)
    assets.publish_due(_PubSession(store), ORG)
    assert rows[0].detail["deferrals"] == assets.MAX_DEFERRALS and rows[0].status == AssetStatus.SCHEDULED
    rows[0].scheduled_for = utcnow() - timedelta(seconds=1)
    assets.publish_due(_PubSession(store), ORG)
    assert rows[0].status == AssetStatus.FAILED


# ── Rejecting is tenant-filtered ───────────────────────────────────────────
class _RowsDb:
    def __init__(self, rows: list[MarketingAsset]) -> None:
        self.rows = rows
        self.statements: list[Any] = []

    def execute(self, stmt: Any) -> Any:  # noqa: ANN401
        self.statements.append(stmt)
        p = _params(stmt)
        found = [r for r in self.rows if r.id == p.get("id_1") and r.tenant_id == p.get("tenant_id_1")]
        return SimpleNamespace(scalars=lambda: SimpleNamespace(first=lambda: found[0] if found else None))

    def get(self, *_a: Any) -> None:  # noqa: ANN401
        raise AssertionError("db.get has no tenant filter")

    def flush(self) -> None:
        pass


def _reject(db: _RowsDb, tenant: str, asset_id: str) -> None:
    from app.services import approvals

    approvals.REJECTERS[assets.TARGET_KIND](db, SimpleNamespace(id=tenant), SimpleNamespace(target_id=asset_id), {})


def test_rejecting_changes_only_this_workspaces_asset() -> None:
    theirs = _design(AssetStatus.DRAFT)
    theirs.tenant_id = OTHER
    db = _RowsDb([theirs])
    _reject(db, TENANT, theirs.id)
    assert theirs.status == AssetStatus.DRAFT
    assert "marketing_assets.tenant_id = " in _sql(db.statements[0])

    mine = _design(AssetStatus.DRAFT)
    _reject(_RowsDb([mine]), TENANT, mine.id)
    assert mine.status == AssetStatus.REJECTED


# ── Facebook and Instagram insights stop on 429 and 5xx ────────────────────
@pytest.fixture
def no_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.connectors.base.connector import BaseConnector

    monkeypatch.setattr(BaseConnector._send.retry, "sleep", lambda _seconds: None)


def _meta(kind: str, handler: Any) -> Any:  # noqa: ANN401
    from app.connectors.base.credentials import Credentials
    from app.connectors.facebook_pages.connector import FacebookPagesConnector
    from app.connectors.instagram_business.connector import InstagramBusinessConnector

    if kind == "facebook":
        connector = FacebookPagesConnector(Credentials(values={"pageId": "1", "pageAccessToken": "tok"}))
    else:
        connector = InstagramBusinessConnector(Credentials(values={"igUserId": "5", "accessToken": "tok"}))
    connector._client = httpx.Client(base_url=connector.base_url, transport=httpx.MockTransport(handler))
    return connector


@pytest.mark.parametrize("kind", ["facebook", "instagram"])
@pytest.mark.parametrize("status", [429, 500, 503])
def test_insights_stop_when_rate_limited_or_the_server_fails(no_backoff: None, kind: str, status: int) -> None:
    connector = _meta(kind, lambda r: httpx.Response(status, json={"error": {"message": "busy"}}))
    with pytest.raises(ConnectorError) as caught:
        connector.read_post_metrics(["123", "456"])
    assert getattr(caught.value, "http_status", None) == status


@pytest.mark.parametrize("kind", ["facebook", "instagram"])
def test_insights_still_skip_a_post_the_platform_refuses(kind: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "/111" in request.url.path:
            return httpx.Response(400, json={"error": {"code": 100, "message": "Unsupported"}})
        if request.url.path.endswith("/insights"):
            return httpx.Response(200, json={"data": [{"name": "reach", "values": [{"value": 9}]}]})
        return httpx.Response(200, json={})

    assert list(_meta(kind, handler).read_post_metrics(["111", "222"])) == ["222"]


# ── The social publisher closes what it opened ─────────────────────────────
class _Bundle:
    def __init__(self, instances: dict) -> None:
        self.instances = instances
        self.closed = 0

    def close(self) -> None:
        self.closed += 1


class _Connector:
    def publish_post(self, **kwargs: Any) -> tuple[str, str]:  # noqa: ANN401
        return "remote1", "https://social.example/remote1"


def _post(network: str, channel: str) -> MarketingAsset:
    body = {"network": network, "text": "Hello", "hashtags": [], "link": ""}
    return MarketingAsset(
        id="a1", tenant_id="t", agent_slug="social_media_manager", kind=AssetKind.SOCIAL_POST, channel=channel,
        title="LinkedIn: Hello", content_encrypted=json.dumps(body), status=AssetStatus.DRAFT,
        detail={"network": network}, image_ids=[],
    )


def test_the_publisher_closes_the_bundle_after_a_post_and_after_a_refusal(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.services.connectors as connector_service
    from app.agents.social_media_manager import publishing

    db = SimpleNamespace(flush=lambda: None, get=lambda model, key: None)
    bundle = _Bundle({"linkedin_pages": _Connector()})
    monkeypatch.setattr(connector_service, "build_connector_bundle", lambda db, org: bundle)
    post = _post("linkedin", "linkedin_pages")
    publishing.publish_social_post(db, SimpleNamespace(id="t"), post, {"network": "linkedin", "text": "Hello"})
    assert post.remote_id == "remote1" and bundle.closed == 1

    empty = _Bundle({})
    monkeypatch.setattr(connector_service, "build_connector_bundle", lambda db, org: empty)
    with pytest.raises(ConflictError, match="not connected"):
        publishing.publish_social_post(db, SimpleNamespace(id="t"), _post("linkedin", "linkedin_pages"), {
            "network": "linkedin", "text": "Hello",
        })
    assert empty.closed == 1


# ── The content library hands out https links only ─────────────────────────
@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://facebook.com/p/1", "https://facebook.com/p/1"),
        ("javascript:alert(1)", ""),
        ("http://facebook.com/p/1", ""),
        ("data:text/html,<script>alert(1)</script>", ""),
        ("https://user:pw@evil.example/", ""),
        ("", ""),
    ],
)
def test_asset_remote_links_are_https_only(url: str, expected: str) -> None:
    from app.api.v1 import content

    post = _post("facebook", "facebook_pages")
    post.remote_url = url
    assert content._out(post, {}, {})["remote_url"] == expected


def test_publishing_an_asset_directly_waits_while_stopped(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.api.v1 import content

    post = _post("facebook", "facebook_pages")
    released: list[str] = []
    monkeypatch.setattr(content.asset_service, "get", lambda db, **k: post)
    monkeypatch.setattr(content, "_pending", lambda db, tenant, ids: {})
    monkeypatch.setattr(content.asset_service, "release", lambda *a: released.append("x"))
    monkeypatch.setattr(agent_safety, "halt_reason", lambda db, org: agent_safety.WORKSPACE_STOP_REASON)
    current = SimpleNamespace(
        tenant_id="t", organization=SimpleNamespace(id="t", settings={}), user=SimpleNamespace(id="u"),
        require_write=lambda module: None,
    )
    db = SimpleNamespace(commit=lambda: None)
    with pytest.raises(ConflictError, match="stopped"):
        content.publish_asset("a1", current, db)
    assert released == []


# ── Against a real database ────────────────────────────────────────────────
@requires_db
def test_a_post_failed_by_another_session_after_the_claim_is_not_sent(org, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ANN001
    from app.db.session import SessionLocal, bind_tenant
    from app.services.encryption import OrgCipher

    sent: list[str] = []
    monkeypatch.setitem(assets.PUBLISHERS, AssetKind.SOCIAL_POST, lambda db, o, a, c: sent.append(a.id))
    first, second = SessionLocal(), SessionLocal()
    try:
        bind_tenant(first, org.id)
        bind_tenant(second, org.id)
        post = assets.create(
            first, tenant_id=org.id, cipher=OrgCipher.for_org(org), agent_slug="creative_studio",
            kind=AssetKind.SOCIAL_POST, channel="facebook_pages", title="Facebook: test",
            content={"network": "facebook", "text": "Hi"}, scheduled_for=utcnow() - timedelta(minutes=1),
        )
        post.status = AssetStatus.SCHEDULED
        first.commit()
        claimed = assets._claim_due(first, org, utcnow(), [post.id])
        first.commit()
        other = second.get(MarketingAsset, post.id)
        other.status = AssetStatus.FAILED
        second.commit()
        assert assets._send_claimed(first, org, claimed[0], OrgCipher.for_org(org)) is False
        assert sent == []
        first.rollback()
    finally:
        first.close()
        second.close()
