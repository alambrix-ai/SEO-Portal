"""Sharing approved designs on social networks, and the scheduled publisher (step 5.7).

Most tests replace the database with a stand-in, so they run anywhere:
the schedule API's checks (approval, connection, caption limits, time,
tenant), time-zone conversion, the publisher's claim (two publishers never
send the same post), retries, Reddit's "wait" becoming a reschedule, the
daily cap, emergency stops, and the signed public image link refusing a
malformed signature with 404. The tests at the end prove the row lock and
tenant isolation against a real PostgreSQL database (``TEST_DATABASE_URL``).
"""
from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.dialects import postgresql

from app.core.config import settings
from app.core.exceptions import ConnectorConfigError, ConnectorError, InvalidInputError
from app.db.base import utcnow
from app.models.content import AssetKind, AssetStatus, MarketingAsset
from app.models.creative import FileKind, FileSource, StoredFile
from app.services import agent_safety, assets, audit, brand_kits, images, social_schedule
from tests.conftest import requires_db

TENANT_A = "00000000-0000-4000-8000-00000000000a"
TENANT_B = "00000000-0000-4000-8000-00000000000b"
USER_ID = "22222222-2222-4222-8222-222222222222"
CREATIVE_A = "cccccccc-0000-4000-8000-000000000001"
CREATIVE_B = "cccccccc-0000-4000-8000-000000000002"
DRAFT_A = "cccccccc-0000-4000-8000-000000000003"
FILE_A = "aaaaaaaa-0000-4000-8000-000000000005"
POST_A = "eeeeeeee-0000-4000-8000-000000000001"
API = "/api/v1"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
LONDON = "Europe/London"


# ── Stand-ins ──────────────────────────────────────────────────────────────
class _Cipher:
    def encrypt(self, value: str, *, context: str) -> str:
        return value

    def decrypt(self, value: str, *, context: str, default: str = "") -> str:
        return value


class _Result:
    def __init__(self, rows: list[Any]) -> None:  # noqa: ANN401
        self.rows = rows

    def scalars(self) -> _Result:
        return self

    def first(self) -> Any:  # noqa: ANN401
        return self.rows[0] if self.rows else None

    def one(self) -> Any:  # noqa: ANN401
        return self.rows[0]

    def __iter__(self):  # noqa: ANN204
        return iter(self.rows)


def _params(stmt: Any) -> dict[str, Any]:  # noqa: ANN401
    return dict(stmt.compile(dialect=postgresql.dialect()).params)


def _sql(stmt: Any) -> str:  # noqa: ANN401
    return str(stmt.compile(dialect=postgresql.dialect()))


class _ApiSession:
    """Answers the schedule service's queries from in-memory rows, by tenant."""

    def __init__(self, creatives: list[MarketingAsset] = (), posts: list[MarketingAsset] = ()) -> None:  # type: ignore[assignment]
        self.creatives = {(c.tenant_id, c.id): c for c in creatives}
        self.posts = list(posts)
        self.added: list[Any] = []
        self.statements: list[Any] = []
        self.commits = 0

    def execute(self, stmt: Any, params: Any = None) -> _Result:  # noqa: ANN401
        self.statements.append(stmt)
        p = _params(stmt)
        if p.get("kind_1") == AssetKind.CREATIVE:
            row = self.creatives.get((p.get("tenant_id_1"), p.get("id_1")))
            return _Result([row] if row is not None else [])
        if p.get("kind_1") == AssetKind.SOCIAL_POST:
            mine = [r for r in self.posts if r.tenant_id == p.get("tenant_id_1")]
            if "id_1" in p:
                return _Result([r for r in mine if r.id == p["id_1"]])
            columns = list(stmt.selected_columns)
            if len(columns) == 1 and columns[0].key == "detail":
                active = [r for r in mine if r.status in social_schedule.ACTIVE_STATUSES]
                return _Result([r.detail for r in active])
            return _Result(mine)
        return _Result([])

    def add(self, row: Any) -> None:  # noqa: ANN401
        if getattr(row, "id", None) is None:
            row.id = f"00000000-0000-4000-9000-{len(self.added):012d}"
        self.added.append(row)

    def flush(self) -> None:
        pass

    def commit(self) -> None:
        self.commits += 1

    def get_bind(self) -> Any:  # noqa: ANN401
        return SimpleNamespace(dialect=SimpleNamespace(name="sqlite"))


class _Publisher:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def publish_post(self, **kwargs: Any) -> tuple[str, str]:  # noqa: ANN401
        self.calls.append(kwargs)
        return "r1", "https://social.example/r1"

    def close(self) -> None:
        pass


#: A design approved through the approval queue: the files it showed were recorded.
_APPROVED_DETAIL = {
    "render_file_id": FILE_A,
    "preview_file_id": FILE_A,
    assets.APPROVED_RENDER_KEY: FILE_A,
    assets.APPROVED_PREVIEW_KEY: FILE_A,
    assets.APPROVED_RENDER_SHA_KEY: hashlib.sha256(PNG).hexdigest(),
}


def _creative(asset_id: str = CREATIVE_A, tenant_id: str = TENANT_A, status: str = AssetStatus.APPROVED) -> MarketingAsset:
    return MarketingAsset(
        id=asset_id,
        tenant_id=tenant_id,
        agent_slug="creative_studio",
        kind=AssetKind.CREATIVE,
        channel="",
        title="Spring sale",
        content_encrypted="{}",
        status=status,
        detail=dict(_APPROVED_DETAIL),
        image_ids=[],
    )


def _stored() -> StoredFile:
    return StoredFile(
        id=FILE_A,
        tenant_id=TENANT_A,
        kind=FileKind.RENDER,
        source=FileSource.RENDERED,
        mime="image/png",
        size_bytes=len(PNG),
        width=1080,
        height=1080,
        sha256=hashlib.sha256(PNG).hexdigest(),
    )


def _current(role: str = "admin", *, tenant_id: str = TENANT_A, zone: str = LONDON) -> Any:  # noqa: ANN401
    from app.api.deps import CurrentUser

    return CurrentUser(
        user=SimpleNamespace(id=USER_ID, name="Test User", role=role, tenant_id=tenant_id),
        organization=SimpleNamespace(id=tenant_id, name="Test Motors", settings={"timezone": zone}),
        claims={},
        enabled_modules=None,
    )


def _client(session: _ApiSession, role: str = "admin", **kw: Any) -> TestClient:  # noqa: ANN401
    """The real app, with the social router mounted (wired for production in step 6.1)."""
    from app.api.deps import get_current_user
    from app.api.v1 import social
    from app.db.session import get_db
    from app.main import create_app
    from tests.conftest import _noop_lifespan

    app: FastAPI = create_app()
    app.router.lifespan_context = _noop_lifespan
    if not any(getattr(r, "path", "").startswith(f"{API}/social") for r in app.routes):
        app.include_router(social.router, prefix=API)
    app.dependency_overrides[get_current_user] = lambda: _current(role, **kw)
    app.dependency_overrides[get_db] = lambda: session
    return TestClient(app)


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Facebook and Reddit connected; the design's file only in workspace A."""
    import app.services.connectors as connector_service
    import app.services.storage as storage

    state = SimpleNamespace(connected={"facebook_pages": _Publisher(), "reddit": _Publisher()}, audits=[], now_calls=[])
    monkeypatch.setattr(
        connector_service,
        "build_connector_bundle",
        lambda db, org: SimpleNamespace(instances=dict(state.connected), close=lambda: None),
    )

    def get_file(_db: Any, tenant_id: str, file_id: str, kinds: Any = None) -> StoredFile | None:  # noqa: ANN401
        return _stored() if (tenant_id, file_id) == (TENANT_A, FILE_A) else None

    monkeypatch.setattr(brand_kits, "get_file", get_file)
    monkeypatch.setattr(storage, "storage_for", lambda row: SimpleNamespace(get=lambda db, tenant_id, file_id: PNG))
    monkeypatch.setattr(social_schedule, "OrgCipher", SimpleNamespace(for_org=lambda org: _Cipher()))
    monkeypatch.setattr(audit, "record_user_action", lambda _db, **kw: state.audits.append(kw))
    monkeypatch.setattr(social_schedule, "publish_now", lambda tenant, ids: state.now_calls.append((tenant, ids)))
    monkeypatch.setattr(settings, "reddit_enabled", True)
    return state


def _later(days: int = 2, zone: str = LONDON) -> str:
    return (utcnow().astimezone(ZoneInfo(zone)) + timedelta(days=days)).strftime("%Y-%m-%dT%H:%M")


def _body(*items: dict, creative: str = CREATIVE_A) -> dict:
    return {"creative_asset_id": creative, "items": list(items)}


def _fb(**extra: Any) -> dict:  # noqa: ANN401
    return {"network": "facebook", "caption": "Spring sale on now", "hashtags": ["spring"], "link": "https://shop.example/", **extra}


# ── The schedule API ───────────────────────────────────────────────────────
def test_scheduling_creates_one_post_per_network_with_a_copy_of_the_picture(env: SimpleNamespace) -> None:
    session = _ApiSession([_creative()])
    when = _later()
    reddit = {"network": "reddit", "caption": "Our spring guide", "title": "Spring guide", "subreddit": "r/SmallBusiness", "when_local": when}
    response = _client(session).post(f"{API}/social/schedule", json=_body(_fb(when_local=when), reddit))
    assert response.status_code == 200, response.text
    posts = response.json()["posts"]
    assert [p["network"] for p in posts] == ["facebook", "reddit"]
    assert all(p["status"] == "scheduled" and p["status_label"] == "Scheduled" and p["can_cancel"] for p in posts)
    assert posts[0]["scheduled_local"] == when and posts[0]["timezone"] == LONDON
    assert posts[1]["subreddit"] == "smallbusiness"

    created = [r for r in session.added if isinstance(r, MarketingAsset)]
    pictures = [r for r in session.added if not isinstance(r, MarketingAsset)]
    assert len(created) == 2 and len(pictures) == 2
    assert all(p.data == PNG and p.tenant_id == TENANT_A for p in pictures)
    fb, rd = created
    assert fb.kind == AssetKind.SOCIAL_POST and fb.channel == "facebook_pages" and rd.channel == "reddit"
    assert fb.image_ids == [pictures[0].id]
    assert fb.detail["source_asset_id"] == CREATIVE_A and len(fb.detail["idempotency_key"]) == 64
    reddit_content = json.loads(rd.content_encrypted)
    assert reddit_content["subreddit"] == "SmallBusiness" and reddit_content["hashtags"] == []
    expected = datetime.strptime(when, "%Y-%m-%dT%H:%M").replace(tzinfo=ZoneInfo(LONDON)).astimezone(UTC)
    assert fb.scheduled_for == expected
    assert session.commits == 1 and env.now_calls == []
    # Audited with ids only: no caption, title or link.
    logged = json.dumps([{k: v for k, v in a.items() if k != "user"} for a in env.audits])
    assert "Spring sale on now" not in logged and "shop.example" not in logged
    assert env.audits[0]["context"]["post_ids"] == [fb.id, rd.id]


def test_post_now_is_due_at_once_and_published_in_the_background(env: SimpleNamespace) -> None:
    session = _ApiSession([_creative()])
    response = _client(session).post(f"{API}/social/schedule", json=_body(_fb()))
    assert response.status_code == 200, response.text
    created = [r for r in session.added if isinstance(r, MarketingAsset)]
    assert created[0].scheduled_for <= utcnow() and created[0].detail["post_now"] is True
    assert env.now_calls == [(TENANT_A, [created[0].id])]


def test_an_unapproved_design_cannot_be_shared(env: SimpleNamespace) -> None:
    session = _ApiSession([_creative(DRAFT_A, status=AssetStatus.DRAFT)])
    response = _client(session).post(f"{API}/social/schedule", json=_body(_fb(), creative=DRAFT_A))
    assert response.status_code == 409
    assert "Approve this design" in response.text
    assert session.added == [] and session.commits == 0


def test_a_design_changed_after_approval_cannot_be_shared(env: SimpleNamespace) -> None:
    # An approved design whose render was swapped afterwards (a restore of an
    # older version): its current picture was never approved.
    changed = _creative()
    changed.detail = {**changed.detail, "render_file_id": "aaaaaaaa-0000-4000-8000-000000000099"}
    session = _ApiSession([changed])
    response = _client(session).post(f"{API}/social/schedule", json=_body(_fb(when_local=_later())))
    assert response.status_code == 409
    assert "changed since it was approved" in response.text
    assert session.added == [] and session.commits == 0


def test_a_design_approved_without_a_recorded_render_cannot_be_shared(env: SimpleNamespace) -> None:
    legacy = _creative()
    legacy.detail = {"render_file_id": FILE_A, "preview_file_id": FILE_A}
    session = _ApiSession([legacy])
    response = _client(session).post(f"{API}/social/schedule", json=_body(_fb(when_local=_later())))
    assert response.status_code == 409
    assert "approved version" in response.text
    assert session.added == []


def test_a_disconnected_network_is_refused(env: SimpleNamespace) -> None:
    env.connected.pop("facebook_pages")
    session = _ApiSession([_creative()])
    response = _client(session).post(f"{API}/social/schedule", json=_body(_fb(when_local=_later())))
    assert response.status_code == 409
    assert "isn't connected" in response.text
    assert session.added == []


def test_reddit_is_refused_while_the_platform_has_it_switched_off(env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "reddit_enabled", False)
    session = _ApiSession([_creative()])
    item = {"network": "reddit", "caption": "Hi", "title": "T", "subreddit": "marketing"}
    assert _client(session).post(f"{API}/social/schedule", json=_body(item)).status_code == 409


def test_a_caption_over_the_networks_limit_is_refused(env: SimpleNamespace) -> None:
    env.connected["x_twitter"] = _Publisher()
    session = _ApiSession([_creative()])
    response = _client(session).post(
        f"{API}/social/schedule", json=_body({"network": "x", "caption": "word " * 80})
    )
    assert response.status_code == 422
    assert "X allows 280" in response.text
    assert session.added == []


def test_a_time_in_the_past_is_refused(env: SimpleNamespace) -> None:
    session = _ApiSession([_creative()])
    response = _client(session).post(f"{API}/social/schedule", json=_body(_fb(when_local=_later(days=-1))))
    assert response.status_code == 422
    assert "already passed" in response.text


@pytest.mark.parametrize(
    "item",
    [
        _fb(when_local="2026-13-01T10:00"),  # no such month
        _fb(when_local="tomorrow"),  # not the format
        _fb(link="javascript:alert(1)"),
        _fb(link="https://user:pw@shop.example/"),
        {"network": "reddit", "caption": "Hi", "title": "T", "subreddit": "../../api/v1/me"},
        {"network": "reddit", "caption": "Hi", "title": "T", "subreddit": "marketing", "hashtags": ["spam"]},
        {"network": "reddit", "caption": "Hi", "subreddit": "marketing"},  # no title
        {"network": "myspace", "caption": "Hi"},
        _fb(extra_field="x"),
    ],
)
def test_malformed_items_are_refused(env: SimpleNamespace, item: dict) -> None:
    session = _ApiSession([_creative()])
    response = _client(session).post(f"{API}/social/schedule", json=_body(item))
    assert response.status_code == 422, response.text
    assert session.added == []


def test_another_workspaces_design_is_not_found(env: SimpleNamespace) -> None:
    session = _ApiSession([_creative(CREATIVE_B, tenant_id=TENANT_B)])
    response = _client(session).post(f"{API}/social/schedule", json=_body(_fb(), creative=CREATIVE_B))
    assert response.status_code == 404
    assert session.added == []
    sql = _sql(session.statements[0])
    assert "marketing_assets.tenant_id = " in sql
    # Listing its posts is "not found" too, not an empty list.
    assert _client(session).get(f"{API}/social/posts", params={"creative": CREATIVE_B}).status_code == 404


def test_the_same_design_network_and_time_cannot_be_scheduled_twice(env: SimpleNamespace) -> None:
    session = _ApiSession([_creative()])
    client = _client(session)
    when = _later()
    assert client.post(f"{API}/social/schedule", json=_body(_fb(when_local=when))).status_code == 200
    session.posts = [r for r in session.added if isinstance(r, MarketingAsset)]
    again = client.post(f"{API}/social/schedule", json=_body(_fb(when_local=when)))
    assert again.status_code == 409 and "already scheduled" in again.text
    # Twice in one request is refused before anything is written.
    fresh = _ApiSession([_creative()])
    twice = _client(fresh).post(f"{API}/social/schedule", json=_body(_fb(when_local=when), _fb(when_local=when)))
    assert twice.status_code == 422 and fresh.added == []


def test_a_view_only_role_cannot_schedule_or_cancel(env: SimpleNamespace) -> None:
    session = _ApiSession([_creative()])
    client = _client(session, role="approver")
    assert client.post(f"{API}/social/schedule", json=_body(_fb())).status_code == 403
    assert client.post(f"{API}/social/posts/{POST_A}/cancel").status_code == 403
    assert client.get(f"{API}/social/posts").status_code == 200
    assert _client(session, role="client").get(f"{API}/social/posts").status_code == 403


def _social_post(status: str, *, tenant_id: str = TENANT_A, **fields: Any) -> MarketingAsset:  # noqa: ANN401
    return MarketingAsset(
        id=POST_A,
        tenant_id=tenant_id,
        agent_slug="creative_studio",
        kind=AssetKind.SOCIAL_POST,
        channel="facebook_pages",
        title="Facebook: Spring sale",
        content_encrypted=json.dumps({"network": "facebook", "text": "Hi"}),
        status=status,
        scheduled_for=utcnow() + timedelta(hours=1),
        detail={"network": "facebook", "source_asset_id": CREATIVE_A},
        image_ids=[],
        **fields,
    )


def test_status_shows_posted_with_a_link_and_failed_with_a_reason(env: SimpleNamespace) -> None:
    posted = _social_post(AssetStatus.PUBLISHED, remote_url="https://facebook.com/p/1", published_at=utcnow())
    failed = _social_post(AssetStatus.FAILED, error="Facebook said the page is unpublished.")
    failed.id = "eeeeeeee-0000-4000-8000-000000000002"
    sneaky = _social_post(AssetStatus.PUBLISHED, remote_url="javascript:alert(1)")
    sneaky.id = "eeeeeeee-0000-4000-8000-000000000003"
    session = _ApiSession([_creative()], posts=[posted, failed, sneaky])
    rows = _client(session).get(f"{API}/social/posts", params={"creative": CREATIVE_A}).json()
    by_id = {r["id"]: r for r in rows}
    assert by_id[POST_A]["status_label"] == "Posted" and by_id[POST_A]["link"] == "https://facebook.com/p/1"
    assert by_id[failed.id]["status_label"] == "Failed" and by_id[failed.id]["reason"].startswith("Facebook said")
    assert by_id[sneaky.id]["link"] == ""  # only https links are handed to the browser
    assert not any(r["can_cancel"] for r in rows)


def test_cancel_only_while_scheduled(env: SimpleNamespace) -> None:
    scheduled = _social_post(AssetStatus.SCHEDULED)
    session = _ApiSession(posts=[scheduled])
    response = _client(session).post(f"{API}/social/posts/{POST_A}/cancel")
    assert response.status_code == 200, response.text
    assert response.json()["post"]["status_label"] == "Cancelled"
    assert scheduled.status == AssetStatus.REJECTED and scheduled.detail["cancelled"] is True
    assert "FOR UPDATE" in _sql(session.statements[-1])

    for status in (AssetStatus.PUBLISHING, AssetStatus.PUBLISHED, AssetStatus.FAILED):
        row = _social_post(status)
        assert _client(_ApiSession(posts=[row])).post(f"{API}/social/posts/{POST_A}/cancel").status_code == 409
        assert row.status == status


def test_another_workspaces_post_cannot_be_cancelled(env: SimpleNamespace) -> None:
    theirs = _social_post(AssetStatus.SCHEDULED, tenant_id=TENANT_B)
    response = _client(_ApiSession(posts=[theirs])).post(f"{API}/social/posts/{POST_A}/cancel")
    assert response.status_code == 404 and theirs.status == AssetStatus.SCHEDULED


# ── Time zones ─────────────────────────────────────────────────────────────
NOW = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)


def test_local_times_convert_to_utc_in_the_workspace_zone() -> None:
    london = ZoneInfo(LONDON)
    # British Summer Time until 25 October 2026, then GMT.
    assert social_schedule.local_to_utc("2026-10-22T09:00", london, LONDON, now=NOW) == datetime(2026, 10, 22, 8, 0, tzinfo=UTC)
    assert social_schedule.local_to_utc("2026-10-27T09:00", london, LONDON, now=NOW) == datetime(2026, 10, 27, 9, 0, tzinfo=UTC)
    kolkata = ZoneInfo("Asia/Kolkata")
    assert social_schedule.local_to_utc("2026-10-22T09:00", kolkata, "Asia/Kolkata", now=NOW) == datetime(2026, 10, 22, 3, 30, tzinfo=UTC)


def test_times_that_dont_exist_or_are_out_of_range_are_refused() -> None:
    new_york = ZoneInfo("America/New_York")
    with pytest.raises(InvalidInputError, match="clocks change"):
        social_schedule.local_to_utc("2027-03-14T02:30", new_york, "America/New_York", now=NOW)
    with pytest.raises(InvalidInputError, match="already passed"):
        social_schedule.local_to_utc("2026-10-21T12:00", UTC, "UTC", now=NOW)  # less than a minute ahead
    with pytest.raises(InvalidInputError, match="a year ahead"):
        social_schedule.local_to_utc("2027-10-22T12:00", UTC, "UTC", now=NOW)
    assert social_schedule.local_to_utc("2026-10-21T12:02", UTC, "UTC", now=NOW) == datetime(2026, 10, 21, 12, 2, tzinfo=UTC)


# ── The scheduled publisher ────────────────────────────────────────────────
class _Store:
    """Rows shared by several publisher sessions, with committed state and row locks.

    A session sees another's changes only once they are committed, and
    ``FOR UPDATE SKIP LOCKED`` skips rows another session has locked, as
    PostgreSQL does.
    """

    def __init__(self, rows: list[MarketingAsset]) -> None:
        self.rows = rows
        self.committed = {r.id: r.status for r in rows}
        self.locks: dict[str, object] = {}


class _PubSession:
    def __init__(self, store: _Store, *, before_commit: Any = None) -> None:  # noqa: ANN401
        self.store = store
        self.statements: list[Any] = []
        self.before_commit = before_commit
        self.commits = 0
        #: Rows this session has loaded; their changes are written when it commits.
        self.touched: dict[str, MarketingAsset] = {}
        #: Ids read again under a row lock before sending.
        self.relocks: list[str] = []

    def execute(self, stmt: Any, params: Any = None) -> _Result:  # noqa: ANN401
        self.statements.append(stmt)
        sql = _sql(stmt)
        p = _params(stmt)
        if "count(" in sql:
            sent = [r for r in self.store.rows if self.store.committed[r.id] == AssetStatus.PUBLISHED]
            oldest = min((r.published_at for r in sent), default=None)
            return _Result([(len(sent), oldest)])
        wanted = p.get("status_1")
        if "FOR UPDATE" in sql and "SKIP LOCKED" not in sql:
            # The publisher reading a claimed row again under its lock.
            self.relocks.append(p.get("id_1"))
            return _Result([r for r in self.store.rows if r.id == p.get("id_1") and r.tenant_id == p.get("tenant_id_1")])
        if "FOR UPDATE SKIP LOCKED" not in sql:
            return _Result([])
        out = []
        for row in self.store.rows:
            # A session sees its own uncommitted changes, and others' committed ones.
            status = row.status if row.id in self.touched else self.store.committed[row.id]
            if status != wanted:
                continue
            if self.store.locks.get(row.id, self) is not self:
                continue  # SKIP LOCKED
            if wanted == AssetStatus.SCHEDULED and row.scheduled_for > p["scheduled_for_1"]:
                continue
            if wanted == AssetStatus.PUBLISHING and row.updated_at >= p["updated_at_1"]:
                continue
            self.store.locks[row.id] = self
            self.touched[row.id] = row
            out.append(row)
        return _Result(out)

    def flush(self) -> None:
        pass

    def commit(self) -> None:
        if self.before_commit is not None:
            hook, self.before_commit = self.before_commit, None
            hook()
        self.commits += 1
        for row_id, row in self.touched.items():
            self.store.committed[row_id] = row.status
            if self.store.locks.get(row_id) is self:
                del self.store.locks[row_id]

    def rollback(self) -> None:
        pass

    def get(self, model: Any, key: str) -> None:  # noqa: ANN401
        return None


def _due(n: int = 1, *, kind: str = AssetKind.SOCIAL_POST, **detail: Any) -> list[MarketingAsset]:  # noqa: ANN401
    rows = []
    for i in range(n):
        rows.append(
            MarketingAsset(
                id=f"post{i}",
                tenant_id="t",
                agent_slug="creative_studio",
                kind=kind,
                channel="reddit",
                title="Reddit: Spring",
                content_encrypted=json.dumps({"network": "reddit", "text": "Hi", "title": "T"}),
                status=AssetStatus.SCHEDULED,
                scheduled_for=utcnow() - timedelta(minutes=1),
                updated_at=utcnow() - timedelta(minutes=5),
                detail={"network": "reddit", **detail},
                image_ids=[],
                error="",
            )
        )
    return rows


@pytest.fixture
def publisher(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """A recording publisher for social posts; no emergency stop."""
    state = SimpleNamespace(calls=[], error=None)

    def publish(db: Any, org: Any, asset: MarketingAsset, content: dict) -> None:  # noqa: ANN401
        state.calls.append(asset.id)
        if state.error is not None:
            raise state.error
        asset.remote_url = f"https://www.reddit.com/{asset.id}"

    monkeypatch.setitem(assets.PUBLISHERS, AssetKind.SOCIAL_POST, publish)
    monkeypatch.setattr(assets, "OrgCipher", SimpleNamespace(for_org=lambda org: _Cipher()))
    monkeypatch.setattr(agent_safety, "halt_reason", lambda db, org: "")
    return state


ORG = SimpleNamespace(id="t", settings={})


def test_the_claim_locks_rows_and_skips_locked_ones(publisher: SimpleNamespace) -> None:
    session = _PubSession(_Store(_due()))
    assert assets.publish_due(session, ORG) == 1
    claim = next(s for s in session.statements if "scheduled_for <=" in _sql(s))
    assert "FOR UPDATE SKIP LOCKED" in _sql(claim) and "marketing_assets.tenant_id = " in _sql(claim)


def test_two_publishers_at_once_send_each_post_exactly_once(publisher: SimpleNamespace) -> None:
    store = _Store(_due(3))
    second = _PubSession(store)
    results: list[int] = []
    # The second publisher runs after the first has locked the rows but
    # before it commits its claim (SKIP LOCKED), and again while the first
    # is sending (it sees "publishing").
    first = _PubSession(store, before_commit=lambda: results.append(assets.publish_due(second, ORG)))

    def publish_and_race(db: Any, org: Any, asset: MarketingAsset, content: dict) -> None:  # noqa: ANN401
        publisher.calls.append(asset.id)
        if len(publisher.calls) == 1:
            results.append(assets.publish_due(_PubSession(store), ORG))

    assets.PUBLISHERS[AssetKind.SOCIAL_POST] = publish_and_race
    sent = assets.publish_due(first, ORG)
    assert sent == 3 and results == [0, 0]
    assert sorted(publisher.calls) == ["post0", "post1", "post2"]
    assert all(store.committed[r.id] == AssetStatus.PUBLISHED for r in store.rows)
    # A later pass finds nothing left to send.
    assert assets.publish_due(_PubSession(store), ORG) == 0 and len(publisher.calls) == 3


def test_a_reddit_rate_limit_reschedules_instead_of_failing(publisher: SimpleNamespace) -> None:
    from app.connectors.reddit.connector import RedditRateLimited

    publisher.error = RedditRateLimited("Reddit asked us to slow down.", retry_after=120)
    rows = _due()
    before = utcnow()
    assert assets.publish_due(_PubSession(_Store(rows)), ORG) == 0
    post = rows[0]
    assert post.status == AssetStatus.SCHEDULED and post.error == ""
    assert before + timedelta(seconds=115) <= post.scheduled_for <= utcnow() + timedelta(seconds=125)
    assert post.detail["publish_note"] and post.detail["deferrals"] == 1
    assert "publish_attempts" not in post.detail  # waiting is not a failed attempt


def test_too_many_waits_end_in_a_plain_failure(publisher: SimpleNamespace) -> None:
    from app.connectors.reddit.connector import RedditRateLimited

    publisher.error = RedditRateLimited("Slow down.", retry_after=60)
    rows = _due(deferrals=assets.MAX_DEFERRALS)
    assets.publish_due(_PubSession(_Store(rows)), ORG)
    assert rows[0].status == AssetStatus.FAILED and "stopped trying" in rows[0].error


def test_the_immediate_publish_path_reschedules_a_rate_limit_too(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.connectors.reddit.connector import RedditRateLimited

    def publish(*_args: Any) -> None:  # noqa: ANN401
        raise RedditRateLimited("Posts to r/marketing are kept 10 minutes apart.", retry_after=300)

    monkeypatch.setitem(assets.PUBLISHERS, AssetKind.SOCIAL_POST, publish)
    post = _due()[0]
    post.status = AssetStatus.DRAFT
    result = assets.publish(SimpleNamespace(flush=lambda: None), ORG, post, _Cipher())
    assert result.status == AssetStatus.SCHEDULED and post.scheduled_for > utcnow() + timedelta(seconds=290)


def test_a_refused_post_fails_with_its_reason_and_is_not_retried(publisher: SimpleNamespace) -> None:
    from app.connectors.reddit.connector import RedditPostRefused

    publisher.error = RedditPostRefused("r/marketing does not allow link posts.")
    rows = _due()
    assets.publish_due(_PubSession(_Store(rows)), ORG)
    assert rows[0].status == AssetStatus.FAILED and rows[0].error == "r/marketing does not allow link posts."
    assert publisher.calls == ["post0"]


def test_a_transient_error_is_retried_with_backoff_then_failed(publisher: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "social_publish_max_attempts", 3)
    monkeypatch.setattr(settings, "social_publish_retry_minutes", 15)
    publisher.error = ConnectorError("Reddit could not be reached. Try again in a few minutes.")
    rows = _due()
    store = _Store(rows)
    post = rows[0]
    gaps = []
    for _ in range(2):
        before = utcnow()
        assets.publish_due(_PubSession(store), ORG)
        assert post.status == AssetStatus.SCHEDULED and post.error == ""
        gaps.append(round((post.scheduled_for - before).total_seconds() / 60))
        post.scheduled_for = utcnow() - timedelta(seconds=1)  # time passes
    assert gaps == [15, 30]
    assets.publish_due(_PubSession(store), ORG)
    assert post.status == AssetStatus.FAILED and post.detail["publish_attempts"] == 3
    assert post.error.startswith("Reddit could not be reached")
    assert len(publisher.calls) == 3


def test_a_configuration_error_fails_at_once(publisher: SimpleNamespace) -> None:
    publisher.error = ConnectorConfigError("Reconnect Reddit to post.")
    rows = _due()
    assets.publish_due(_PubSession(_Store(rows)), ORG)
    assert rows[0].status == AssetStatus.FAILED and rows[0].error == "Reconnect Reddit to post."


def test_raw_exception_text_never_reaches_the_post(publisher: SimpleNamespace) -> None:
    publisher.error = RuntimeError("Traceback (most recent call last): password=hunter2")
    rows = _due()
    assets.publish_due(_PubSession(_Store(rows)), ORG)
    assert rows[0].status == AssetStatus.FAILED and "hunter2" not in rows[0].error


def test_nothing_is_sent_while_an_emergency_stop_is_on(publisher: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(agent_safety, "halt_reason", lambda db, org: agent_safety.PLATFORM_STOP_REASON)
    session = _PubSession(_Store(_due()))
    assert assets.publish_due(session, ORG) == 0
    assert publisher.calls == [] and session.statements == []


def test_the_daily_cap_holds_posts_for_later(publisher: SimpleNamespace) -> None:
    sent = _due(2)
    for i, row in enumerate(sent):
        row.id = f"sent{i}"
        row.status = AssetStatus.PUBLISHED
        row.published_at = utcnow() - timedelta(hours=3 - i)
    waiting = _due(1)
    store = _Store(sent + waiting)
    org = SimpleNamespace(id="t", settings={assets.DAILY_CAP_SETTING: 2})
    assert assets.publish_due(_PubSession(store), org) == 0
    post = waiting[0]
    assert publisher.calls == [] and post.status == AssetStatus.SCHEDULED
    assert post.scheduled_for > utcnow() + timedelta(hours=20)
    assert "limit of 2 posts a day" in post.detail["publish_note"]


def test_a_post_left_publishing_is_failed_not_resent(publisher: SimpleNamespace) -> None:
    rows = _due()
    rows[0].status = AssetStatus.PUBLISHING
    rows[0].updated_at = utcnow() - timedelta(minutes=assets.STALE_PUBLISHING_MINUTES + 5)
    store = _Store(rows)
    assets.publish_due(_PubSession(store), ORG)
    assert rows[0].status == AssetStatus.FAILED and "Check the platform" in rows[0].error
    assert publisher.calls == []


def test_post_now_only_claims_the_named_posts(publisher: SimpleNamespace) -> None:
    rows = _due(2)
    session = _PubSession(_Store(rows))
    assets.publish_due(session, ORG, only_ids=["post1"])
    claim = next(s for s in session.statements if "scheduled_for <=" in _sql(s))
    assert "marketing_assets.id IN" in _sql(claim)


# ── Signed public image links ──────────────────────────────────────────────
@pytest.mark.parametrize("sig", ["é" * 40, "A" * 40, "0" * 39, "0" * 41, "g" * 40, "", "../" * 14])
def test_a_malformed_signature_is_refused_without_an_error(sig: str) -> None:
    assert images.verify("t", "i", int(utcnow().timestamp()) + 60, sig) is False


def test_a_valid_signature_still_verifies() -> None:
    url = images.public_url("t", "i", seconds=60)
    _tenant, _image, expires, sig = url.rsplit("/", 4)[1:]
    assert images.verify("t", "i", int(expires), sig) is True
    assert images.verify("t", "other", int(expires), sig) is False


@pytest.mark.parametrize("sig", ["%C3%A9" * 40, "Z" * 40, "abc"])
def test_the_public_image_route_answers_404_for_a_non_hex_signature(sig: str) -> None:
    from app.main import create_app
    from tests.conftest import _noop_lifespan

    app = create_app()
    app.router.lifespan_context = _noop_lifespan
    expires = int(utcnow().timestamp()) + 3600
    response = TestClient(app).get(f"{API}/public/images/t/i/{expires}/{sig}")
    assert response.status_code == 404


# ── Against a real database ────────────────────────────────────────────────
@requires_db
def test_skip_locked_claims_a_row_once_across_two_sessions(org) -> None:  # noqa: ANN001
    from app.db.session import SessionLocal, bind_tenant
    from app.services.encryption import OrgCipher

    first, second = SessionLocal(), SessionLocal()
    try:
        bind_tenant(first, org.id)
        bind_tenant(second, org.id)
        post = assets.create(
            first,
            tenant_id=org.id,
            cipher=OrgCipher.for_org(org),
            agent_slug="creative_studio",
            kind=AssetKind.SOCIAL_POST,
            channel="facebook_pages",
            title="Facebook: test",
            content={"network": "facebook", "text": "Hi"},
            scheduled_for=utcnow() - timedelta(minutes=1),
        )
        post.status = AssetStatus.SCHEDULED
        first.commit()

        now = utcnow()
        claimed = assets._claim_due(first, org, now, None)  # locked, not yet committed
        assert [a.id for a in claimed] == [post.id]
        assert assets._claim_due(second, org, now, None) == []  # skipped, not waited for
        first.commit()
        assert assets._claim_due(second, org, utcnow(), None) == []  # now "publishing"
        second.rollback()
    finally:
        first.close()
        second.close()


@requires_db
def test_another_workspaces_design_is_not_found_in_the_database(clean_db, org, second_org) -> None:  # noqa: ANN001
    from app.core.exceptions import NotFoundError
    from app.db.session import bind_tenant
    from app.services.encryption import OrgCipher

    bind_tenant(clean_db, second_org.id)
    theirs = assets.create(
        clean_db,
        tenant_id=second_org.id,
        cipher=OrgCipher.for_org(second_org),
        agent_slug="creative_studio",
        kind=AssetKind.CREATIVE,
        channel="",
        title="Theirs",
        content={},
    )
    theirs.status = AssetStatus.APPROVED
    clean_db.commit()
    bind_tenant(clean_db, org.id)
    with pytest.raises(NotFoundError):
        social_schedule.load_creative(clean_db, org.id, theirs.id)
