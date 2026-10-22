"""Review fixes for serving creatives safely (fix group G1b).

* ``GET /files/{id}`` serves a full-size render only while its design is
  approved or exported, and only the render that was approved.
* An SVG paint value may carry one ``url(#id)`` and a plain fallback colour,
  never a second ``url(``.
* A missing Playwright fails the render fast with ``RendererUnavailable``,
  so the Pillow fallback draws instead of the job timing out.

Most tests run against a stand-in session that applies the tenant and id
filters of each query; the last one checks the gate against PostgreSQL and
needs ``TEST_DATABASE_URL``.
"""
from __future__ import annotations

import hashlib
import sys
import time
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.dialects import postgresql

from app.api.v1 import files as files_api
from app.core.config import settings
from app.models.content import AssetKind, AssetStatus, MarketingAsset
from app.models.creative import CreativeJob, CreativeJobStatus, FileKind, FileSource, StoredFile
from app.services.creative.svg import SvgRejected, sanitise_svg
from app.services.storage import FILE_NOT_FOUND
from tests.conftest import requires_db

TENANT_A = "00000000-0000-4000-8000-00000000000a"
TENANT_B = "00000000-0000-4000-8000-00000000000b"
USER_ID = "22222222-2222-4222-8222-222222222222"
JOB_A = "eeeeeeee-0000-4000-8000-00000000000a"
ASSET_1 = "aaaaaaaa-1111-4000-8000-000000000001"
ASSET_B = "bbbbbbbb-1111-4000-8000-000000000001"
RENDER = "ffffffff-0000-4000-8000-000000000001"
PREVIEW = "ffffffff-0000-4000-8000-000000000002"
OLD_RENDER = "ffffffff-0000-4000-8000-000000000003"
API = "/api/v1"
NOW = datetime(2026, 10, 1, tzinfo=UTC)


# ── A session that honours tenant and id filters ───────────────────────────
class _Result:
    def __init__(self, rows: list[Any]) -> None:
        self.rows = rows

    def scalars(self) -> _Result:
        return self

    def __iter__(self):  # noqa: ANN204
        return iter(self.rows)

    def first(self) -> Any:  # noqa: ANN401
        return self.rows[0] if self.rows else None

    def scalar_one_or_none(self) -> Any:  # noqa: ANN401
        return self.first()


class _Session:
    """Keeps rows matching each query's plain column filters (``tenant_id``,
    ``id``, ``kind``). JSON lookups are not applied: the route itself checks
    which file a design releases, so returning more designs is the harder test."""

    _COLUMNS = ("tenant_id", "id", "kind")

    def __init__(self, *rows: Any) -> None:  # noqa: ANN401
        self.rows = list(rows)
        self.statements: list[str] = []

    def execute(self, stmt: Any) -> _Result:  # noqa: ANN401
        entity = stmt.column_descriptions[0]["entity"]
        compiled = stmt.compile(dialect=postgresql.dialect())
        self.statements.append(str(compiled))
        filters: dict[str, Any] = {}
        for name, value in compiled.params.items():
            column = name.rsplit("_", 1)[0]
            if column in self._COLUMNS:
                filters[column] = value
        out = []
        for row in self.rows:
            if not isinstance(row, entity):
                continue
            if all(
                (getattr(row, c) in v) if isinstance(v, (list, tuple, set)) else getattr(row, c) == v
                for c, v in filters.items()
            ):
                out.append(row)
        return _Result(out)


def _file(file_id: str, *, kind: str = FileKind.RENDER, tenant_id: str = TENANT_A, job_id: str = JOB_A) -> StoredFile:
    row = StoredFile(
        id=file_id, tenant_id=tenant_id, kind=kind, source=FileSource.RENDERED, mime="image/png",
        size_bytes=4, width=10, height=10, sha256="0" * 64, display_name="instagram_square.png",
        created_by="", job_id=job_id,
    )
    row.created_at = NOW
    return row


def _asset(status: str, *, asset_id: str = ASSET_1, tenant_id: str = TENANT_A, render: str = RENDER,
           **extra: Any) -> MarketingAsset:  # noqa: ANN401
    detail = {"render_file_id": render, "file_id": render, "preview_file_id": PREVIEW, "job_id": JOB_A, **extra}
    return MarketingAsset(
        id=asset_id, tenant_id=tenant_id, agent_slug="creative_studio", kind=AssetKind.CREATIVE,
        channel="download", title="Design", status=status, detail=detail,
    )


def _job(*asset_ids: str, tenant_id: str = TENANT_A) -> CreativeJob:
    return CreativeJob(
        id=JOB_A, tenant_id=tenant_id, created_by=USER_ID, status=CreativeJobStatus.SUCCEEDED,
        formats=["instagram_square"], outputs=[{"format_id": "instagram_square", "asset_id": a} for a in asset_ids],
    )


class _Storage:
    def __init__(self) -> None:
        self.reads: list[str] = []

    def get(self, _db: Any, *, tenant_id: str, file_id: str) -> bytes:  # noqa: ANN401
        self.reads.append(file_id)
        return b"\x89PNG"


@pytest.fixture
def storage(monkeypatch: pytest.MonkeyPatch) -> _Storage:
    fake = _Storage()
    monkeypatch.setattr(files_api, "storage_for", lambda _row: fake)
    return fake


def _client(session: _Session, role: str = "admin") -> TestClient:
    from app.api.deps import CurrentUser, get_current_user
    from app.db.session import get_db
    from app.main import create_app
    from tests.conftest import _noop_lifespan

    app = create_app()
    app.router.lifespan_context = _noop_lifespan
    current = CurrentUser(
        user=SimpleNamespace(id=USER_ID, name="Test User", role=role, tenant_id=TENANT_A),
        organization=SimpleNamespace(id=TENANT_A, name="Test", settings={}),
        claims={},
        enabled_modules=None,
    )
    app.dependency_overrides[get_current_user] = lambda: current
    app.dependency_overrides[get_db] = lambda: session
    return TestClient(app)


# ── The render approval gate ───────────────────────────────────────────────
@pytest.mark.parametrize("status", [AssetStatus.APPROVED, AssetStatus.EXPORTED])
def test_an_approved_designs_render_is_served(status: str, storage: _Storage) -> None:
    session = _Session(_file(RENDER), _job(ASSET_1), _asset(status))
    with _client(session) as client:
        response = client.get(f"{API}/files/{RENDER}")
    assert response.status_code == 200
    assert storage.reads == [RENDER]


@pytest.mark.parametrize(
    "status",
    [AssetStatus.DRAFT, AssetStatus.REJECTED, AssetStatus.FAILED, AssetStatus.SCHEDULED, AssetStatus.PUBLISHED],
)
def test_a_render_is_not_found_until_its_design_is_approved(status: str, storage: _Storage) -> None:
    session = _Session(_file(RENDER), _job(ASSET_1), _asset(status))
    with _client(session) as client:
        response = client.get(f"{API}/files/{RENDER}")
    assert response.status_code == 404
    assert response.json()["detail"] == FILE_NOT_FOUND
    assert storage.reads == []


def test_the_preview_of_an_unapproved_design_is_still_served(storage: _Storage) -> None:
    session = _Session(_file(PREVIEW, kind=FileKind.PREVIEW), _job(ASSET_1), _asset(AssetStatus.DRAFT))
    with _client(session) as client:
        assert client.get(f"{API}/files/{PREVIEW}").status_code == 200
    assert storage.reads == [PREVIEW]


def test_a_render_with_no_owning_design_is_not_served(storage: _Storage) -> None:
    session = _Session(_file(RENDER, job_id=""))
    with _client(session) as client:
        assert client.get(f"{API}/files/{RENDER}").status_code == 404
    assert storage.reads == []


def test_a_redrawn_design_releases_only_the_render_that_was_approved(storage: _Storage) -> None:
    # Approved with OLD_RENDER; a redraw since then made RENDER, not yet approved.
    asset = _asset(AssetStatus.APPROVED, approved_render_file_id=OLD_RENDER)
    session = _Session(_file(RENDER), _file(OLD_RENDER), _job(ASSET_1), asset)
    with _client(session) as client:
        assert client.get(f"{API}/files/{RENDER}").status_code == 404
        assert client.get(f"{API}/files/{OLD_RENDER}").status_code == 200
    assert storage.reads == [OLD_RENDER]


def test_an_older_render_of_an_approved_design_is_not_served(storage: _Storage) -> None:
    # The design was revised (new render) and then approved: the superseded
    # render was never approved, so it stays unavailable.
    session = _Session(_file(OLD_RENDER), _job(ASSET_1), _asset(AssetStatus.APPROVED, render=RENDER))
    with _client(session) as client:
        assert client.get(f"{API}/files/{OLD_RENDER}").status_code == 404
    assert storage.reads == []


def test_a_render_redrawn_outside_its_job_is_found_through_the_design(storage: _Storage) -> None:
    # No job link on the file: the design's own file ids find the owner.
    session = _Session(_file(RENDER, job_id=""), _asset(AssetStatus.APPROVED))
    with _client(session) as client:
        assert client.get(f"{API}/files/{RENDER}").status_code == 200


def test_another_workspaces_approved_design_never_releases_this_render(storage: _Storage) -> None:
    # Workspace B's approved design names our render id; it must not count.
    theirs = _asset(AssetStatus.APPROVED, asset_id=ASSET_B, tenant_id=TENANT_B)
    session = _Session(_file(RENDER), _job(ASSET_1, ASSET_B), _asset(AssetStatus.DRAFT), theirs)
    with _client(session) as client:
        assert client.get(f"{API}/files/{RENDER}").status_code == 404
    assert storage.reads == []
    owner_queries = [s for s in session.statements if "FROM marketing_assets" in s or "FROM creative_jobs" in s]
    assert owner_queries and all(".tenant_id = " in s for s in owner_queries)


def test_released_render_ids_prefer_the_recorded_approval() -> None:
    plain = _asset(AssetStatus.APPROVED)
    assert files_api.released_render_ids(plain) == {RENDER}
    recorded = _asset(AssetStatus.APPROVED, approved_render_file_id=OLD_RENDER)
    assert files_api.released_render_ids(recorded) == {OLD_RENDER}
    empty = _asset(AssetStatus.APPROVED, render="")
    empty.detail = {}
    assert files_api.released_render_ids(empty) == set()


# ── SVG fallback paint ─────────────────────────────────────────────────────
def _svg(body: str) -> bytes:
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10" viewBox="0 0 10 10">'
        '<defs><linearGradient id="g"><stop offset="0" stop-color="#000"/></linearGradient>'
        '<linearGradient id="h"><stop offset="0" stop-color="#fff"/></linearGradient></defs>'
        f"{body}</svg>"
    ).encode()


@pytest.mark.parametrize("fill", ["url(#g)", "url(#g) none", "url(#g) currentColor", "url(#g) #abc",
                                  "url(#g) #a1b2c3", "url('#g') red"])
def test_a_fragment_paint_with_a_plain_fallback_is_kept(fill: str) -> None:
    markup = sanitise_svg(_svg(f'<rect width="1" height="1" fill="{fill}"/>'))
    assert f'fill="{fill}"' in markup.replace("&apos;", "'").replace("&#39;", "'")


@pytest.mark.parametrize("fill", ["url(#g) url(#h)", "url(#g) url(https://evil.example/x)",
                                  "url(#g) URL(#h)", "url(#g)url(#h)"])
def test_a_second_url_in_a_paint_is_rejected(fill: str) -> None:
    with pytest.raises(SvgRejected):
        sanitise_svg(_svg(f'<rect width="1" height="1" fill="{fill}"/>'))


@pytest.mark.parametrize("fill", ["url(#g) rgb(1,2,3)", "url(#g) var(--x)", "url(#g) #zzz", "url(#g) Red;x"])
def test_a_fallback_that_is_not_a_plain_colour_is_dropped(fill: str) -> None:
    markup = sanitise_svg(_svg(f'<rect width="1" height="1" fill="{fill}"/>'))
    assert "fill=" not in markup.split("<rect", 1)[1]


# ── Chromium without Playwright falls back to Pillow ───────────────────────
def test_a_missing_playwright_fails_fast_as_renderer_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services.creative.render.base import RendererUnavailable
    from app.services.creative.render.chromium import ChromiumRenderer

    monkeypatch.setitem(sys.modules, "playwright.sync_api", None)  # the import raises ImportError
    monkeypatch.setattr(settings, "creative_render_timeout_seconds", 5)
    renderer = ChromiumRenderer(max_concurrent=1)
    started = time.monotonic()
    with pytest.raises(RendererUnavailable):
        renderer.start()
    # Far below the 3 x timeout + grace wait a lost job would take.
    assert time.monotonic() - started < 5
    assert renderer.recently_failed
    assert renderer._busy == 0  # the slot was given back


def test_render_creative_uses_pillow_when_playwright_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services.creative import render as render_api
    from app.services.creative.render.chromium import ChromiumRenderer
    from tests.test_creative_render import _inputs

    monkeypatch.setitem(sys.modules, "playwright.sync_api", None)
    monkeypatch.setattr(settings, "creative_renderer", "chromium")
    monkeypatch.setattr(settings, "creative_render_timeout_seconds", 5)
    monkeypatch.setattr(render_api, "_chromium", ChromiumRenderer(max_concurrent=1))
    monkeypatch.setattr(render_api, "_degraded", False)
    result = render_api.render_creative(_inputs())
    assert result.renderer == "pillow"
    assert render_api.renderer_status() == "degraded"


# ── Database-backed ────────────────────────────────────────────────────────
@requires_db
def test_db_render_is_served_only_after_approval(clean_db, org) -> None:  # noqa: ANN001
    from sqlalchemy import select

    from app.api.deps import CurrentUser
    from app.db.session import bind_tenant
    from app.models.workspace import User
    from app.services import uploads

    bind_tenant(clean_db, org.id)
    data = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
    render = uploads.store_file(
        clean_db, tenant_id=org.id, data=data, mime="image/png", kind=FileKind.RENDER,
        source=FileSource.RENDERED, sha256=hashlib.sha256(data).hexdigest(), display_name="instagram_square.png",
    )
    asset = MarketingAsset(
        tenant_id=org.id, agent_slug="creative_studio", kind=AssetKind.CREATIVE, channel="download",
        title="Design", status=AssetStatus.DRAFT,
        detail={"render_file_id": render.id, "file_id": render.id, "preview_file_id": render.id},
    )
    clean_db.add(asset)
    clean_db.commit()
    render_id, asset_id = render.id, asset.id
    admin = clean_db.execute(select(User).where(User.tenant_id == org.id)).scalars().first()

    from app.api.deps import get_current_user
    from app.db.session import get_db
    from app.main import create_app
    from tests.conftest import _noop_lifespan

    app = create_app()
    app.router.lifespan_context = _noop_lifespan
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(user=admin, organization=org, claims={})
    app.dependency_overrides[get_db] = lambda: clean_db
    with TestClient(app) as client:
        assert client.get(f"{API}/files/{render_id}").status_code == 404
        bind_tenant(clean_db, org.id)
        clean_db.get(MarketingAsset, asset_id).status = AssetStatus.APPROVED
        clean_db.commit()
        assert client.get(f"{API}/files/{render_id}").status_code == 200
