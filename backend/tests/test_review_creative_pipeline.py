"""Review fixes (cycle 1, group G1a) for the Creative Studio pipeline.

Covers: restore refused on an approved design; short quota and storage
transactions with a bounded lock wait (429 "busy"); the stale-job sweeper;
saving each format as soon as it is drawn; redraw failures that never leave
the spinner on; a separate small preview for digital formats; null goal and
tone; one clean-up of AI images; claim-text hashes; and the removal of the
dead ``tokens_nullable`` branch.

The unit tests reuse the in-memory world of ``test_creative_pipeline``. The
``requires_db`` tests repeat the locking behaviour against real PostgreSQL.
"""
from __future__ import annotations

import hashlib
import io
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from PIL import Image
from pydantic import ValidationError
from sqlalchemy import inspect
from sqlalchemy.exc import OperationalError
from sqlalchemy.sql import Select

from app.core.config import settings
from app.core.exceptions import ConflictError
from app.db.base import new_id, utcnow
from app.models.approval import ApprovalItem, ApprovalStatus
from app.models.content import AssetStatus, MarketingAsset
from app.models.creative import CreativeJob, CreativeJobStatus, FileKind, FileSource, StoredFile
from app.orchestration import runner
from app.services import approvals, assets, uploads
from app.services.creative import hooks, pipeline, quotas, render
from app.services.creative.formats import get_format
from app.services.creative.render import RenderError
from app.services.encryption import OrgCipher
from tests.conftest import requires_db
from tests.test_creative_pipeline import (  # noqa: F401 - pytest fixtures
    BANNER,
    SOCIAL,
    FakeDb,
    _actor,
    _org,
    _png,
    _request,
    _rows,
    _run_hooks,
    _submit,
    creative_hooks,
    world,
)

STORY = "instagram_story"


def _one_creative(world: SimpleNamespace) -> tuple[CreativeJob, ApprovalItem, MarketingAsset]:  # noqa: F811
    job = _submit(world, [BANNER])
    _run_hooks(world)
    assert job.status == CreativeJobStatus.SUCCEEDED, job.error_code
    return job, _rows(world, ApprovalItem)[0], _rows(world, MarketingAsset)[0]


GOOD_TEXT = {"format_id": BANNER, "copy": {"headline": "Made for the way you work", "body": "",
                                            "cta": "Go", "claim_ids": [1]}}


# ── Restore on an approved design ──────────────────────────────────────────
@pytest.mark.parametrize(
    "status",
    [AssetStatus.APPROVED, AssetStatus.EXPORTED, AssetStatus.PUBLISHING, AssetStatus.PUBLISHED],
)
def test_restore_on_an_approved_design_is_409_and_changes_nothing(world, status: str) -> None:  # noqa: ANN001, F811
    _job, _item, asset = _one_creative(world)
    asset.status = status
    before = assets.content_of(asset, OrgCipher.for_org(world.org))
    with pytest.raises(ConflictError) as raised:
        hooks.restore_creative_text(world.db, world.org, asset.id, GOOD_TEXT, user=None)
    assert raised.value.status_code == 409
    assert raised.value.message == "This design is approved. Ask for changes to make a new version."
    assert assets.content_of(asset, OrgCipher.for_org(world.org)) == before
    assert not world.hooks  # no redraw queued


def test_restore_on_a_draft_still_works(world) -> None:  # noqa: ANN001, F811
    _job, _item, asset = _one_creative(world)
    assert asset.status == AssetStatus.DRAFT
    restored = hooks.restore_creative_text(world.db, world.org, asset.id, GOOD_TEXT, user=None)
    assert restored["copy"]["headline"] == "Made for the way you work"
    assert asset.detail["claim_hashes"] == [hashlib.sha256(world.product.approved_claims[1].encode()).hexdigest()]


# ── Quotas: short transactions and a bounded wait ──────────────────────────
class _Orig:
    def __init__(self, sqlstate: str) -> None:
        self.sqlstate = sqlstate


class _LockSession:
    """Records statements; the advisory lock raises ``error`` if given."""

    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[str] = []

    def execute(self, stmt: Any, params: Any = None) -> None:  # noqa: ANN401
        sql = str(stmt)
        self.calls.append(sql)
        if "pg_advisory_xact_lock" in sql and self.error is not None:
            raise self.error

    def add(self, row: Any) -> None:  # noqa: ANN401
        self.calls.append("add")

    def flush(self) -> None:
        self.calls.append("flush")


def _timeout_error(sqlstate: str = "55P03") -> OperationalError:
    return OperationalError("SELECT pg_advisory_xact_lock(...)", {}, _Orig(sqlstate))


def test_admission_that_cannot_get_the_lock_in_time_is_a_friendly_429(monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setattr(quotas, "_fresh_settings", lambda db, tenant_id: {})
    monkeypatch.setattr(quotas, "usage", lambda db, tenant_id, now=None: quotas.Usage())
    db = _LockSession(_timeout_error())
    with pytest.raises(quotas.QuotaBusy) as raised:
        quotas.admit_job(db, CreativeJob(tenant_id="t1", created_by="u1"))  # type: ignore[arg-type]
    assert raised.value.status_code == 429
    assert raised.value.message == quotas.BUSY_MESSAGE
    assert "55P03" not in raised.value.message and "advisory" not in raised.value.message
    assert "set_config('lock_timeout'" in db.calls[0]
    assert "add" not in db.calls  # nothing admitted


def test_admission_sets_the_lock_timeout_and_then_resets_it(monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setattr(quotas, "_fresh_settings", lambda db, tenant_id: {})
    monkeypatch.setattr(quotas, "usage", lambda db, tenant_id, now=None: quotas.Usage())
    db = _LockSession()
    quotas.admit_job(db, CreativeJob(tenant_id="t1", created_by="u1"))  # type: ignore[arg-type]
    assert "set_config('lock_timeout'" in db.calls[0]
    assert "pg_advisory_xact_lock" in db.calls[1]
    assert db.calls[2] == "SET LOCAL lock_timeout TO DEFAULT"


def test_other_database_errors_are_not_reported_as_busy() -> None:
    db = _LockSession(_timeout_error("08006"))  # connection failure
    with pytest.raises(OperationalError):
        quotas.admit_job(db, CreativeJob(tenant_id="t1", created_by="u1"))  # type: ignore[arg-type]


class _ShortStore:
    """One committed job row shared by short sessions, plus a real lock."""

    def __init__(self, row: CreativeJob) -> None:
        self.row = row
        self.lock = threading.Lock()
        self.sessions: list[_ShortSession] = []


class _ShortSession:
    def __init__(self, store: _ShortStore) -> None:
        self.store = store
        self.held = False
        self.committed = False
        self.calls: list[str] = []

    def execute(self, stmt: Any, params: Any = None) -> Any:  # noqa: ANN401
        sql = str(stmt)
        self.calls.append(sql)
        if "pg_advisory_xact_lock" in sql:
            self.store.lock.acquire()
            self.held = True
            return None
        if isinstance(stmt, Select) and stmt._for_update_arg is not None:
            row = self.store.row
            return SimpleNamespace(scalars=lambda: SimpleNamespace(first=lambda: row))
        return None

    def flush(self) -> None:
        pass

    def end(self, *, commit: bool) -> None:
        self.committed = commit
        if self.held:
            self.store.lock.release()
            self.held = False


def _short_store(monkeypatch: pytest.MonkeyPatch, row: CreativeJob) -> _ShortStore:
    store = _ShortStore(row)

    @contextmanager
    def scope(tenant_id: str | None = None):  # noqa: ANN202
        session = _ShortSession(store)
        store.sessions.append(session)
        try:
            yield session
        except Exception:
            session.end(commit=False)
            raise
        session.end(commit=True)

    monkeypatch.setattr(quotas, "_session_scope", scope)
    monkeypatch.setattr(quotas, "_fresh_settings", lambda db, tenant_id: {})
    monkeypatch.setattr(quotas, "usage", lambda db, tenant_id, now=None: quotas.Usage(tokens=row.tokens_out or 0))
    return store


def test_a_token_reservation_commits_on_its_own_and_releases_the_lock(monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setattr(settings, "creative_tokens_per_month", 10_000)
    job_id = new_id()
    committed = CreativeJob(id=job_id, tenant_id="t1", tokens_in=0, tokens_out=0)
    caller = CreativeJob(id=job_id, tenant_id="t1", tokens_in=0, tokens_out=0)
    store = _short_store(monkeypatch, committed)
    job_session = _LockSession()

    reserved = quotas.reserve_tokens(job_session, caller, 2000)  # type: ignore[arg-type]

    assert reserved == 2000 and committed.tokens_out == 2000
    assert job_session.calls == []  # the job's long session is never used
    assert not store.lock.locked()  # released at once, not at the end of the job
    assert store.sessions[0].committed is True
    assert any("lock_timeout" in c for c in store.sessions[0].calls)
    # The caller's copy shows the new value without being marked as changed.
    assert caller.tokens_out == 2000
    assert not inspect(caller).attrs.tokens_out.history.has_changes()

    quotas.settle_tokens(job_session, caller, reserved, tokens_in=10, tokens_out=20)  # type: ignore[arg-type]
    assert (committed.tokens_in, committed.tokens_out) == (10, 20)
    assert (caller.tokens_in, caller.tokens_out) == (10, 20)


def test_a_refused_reservation_rolls_back_and_releases_the_lock(monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setattr(settings, "creative_tokens_per_month", 1000)
    committed = CreativeJob(id=new_id(), tenant_id="t1", tokens_in=0, tokens_out=900)
    store = _short_store(monkeypatch, committed)
    with pytest.raises(quotas.QuotaExceeded):
        quotas.reserve_tokens(_LockSession(), committed, 500)  # type: ignore[arg-type]
    assert committed.tokens_out == 900
    assert store.sessions[0].committed is False and not store.lock.locked()


def test_a_reservation_for_a_job_that_is_not_committed_is_refused(monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setattr(settings, "creative_images_per_month", 10)
    store = _short_store(monkeypatch, CreativeJob(id=new_id(), tenant_id="t1"))
    store.row = None  # type: ignore[assignment]
    from app.core.exceptions import NotFoundError

    with pytest.raises(NotFoundError):
        quotas.reserve_images(_LockSession(), CreativeJob(id=new_id(), tenant_id="t1"), 1)  # type: ignore[arg-type]


def test_the_dead_tokens_nullable_branch_is_gone() -> None:
    assert not hasattr(quotas, "tokens_nullable")
    assert CreativeJob.__table__.c.tokens_in.nullable is True


# ── Storage: a short transaction of its own on PostgreSQL ──────────────────
class _PgBind:
    dialect = SimpleNamespace(name="postgresql")


class _CallerSession:
    def __init__(self) -> None:
        self.merged: list[StoredFile] = []
        self.added: list[Any] = []

    def get_bind(self) -> _PgBind:
        return _PgBind()

    def merge(self, row: StoredFile, *, load: bool = True) -> StoredFile:
        assert load is False
        self.merged.append(row)
        return row

    def add(self, row: Any) -> None:  # noqa: ANN401
        self.added.append(row)


class _StorageSession:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.added: list[StoredFile] = []
        self.statements: list[str] = []
        self.committed = False

    def get_bind(self) -> _PgBind:
        return _PgBind()

    def execute(self, stmt: Any, params: Any = None) -> None:  # noqa: ANN401
        self.statements.append(str(stmt))
        if "pg_advisory_xact_lock" in str(stmt) and self.error is not None:
            raise self.error

    def add(self, row: StoredFile) -> None:
        self.added.append(row)

    def flush(self) -> None:
        pass


def _storage_scope(monkeypatch: pytest.MonkeyPatch, session: _StorageSession, used: int = 0) -> list[str]:
    tenants: list[str] = []

    @contextmanager
    def scope(tenant_id: str | None = None):  # noqa: ANN202
        tenants.append(tenant_id or "")
        yield session
        session.committed = True

    monkeypatch.setattr(uploads, "_session_scope", scope)
    monkeypatch.setattr(uploads, "tenant_usage_bytes", lambda _db, _tenant: used)
    return tenants


def _png_bytes() -> bytes:
    return _png((20, 20))


def test_store_file_writes_in_its_own_committed_transaction(monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setattr(settings, "creative_storage_max_bytes_per_tenant", 10_000_000)
    short = _StorageSession()
    tenants = _storage_scope(monkeypatch, short)
    caller = _CallerSession()
    data = _png_bytes()
    stored = uploads.store_file(
        caller, tenant_id="t1", data=data, mime="image/png", kind=FileKind.RENDER,  # type: ignore[arg-type]
        source=FileSource.RENDERED, sha256=hashlib.sha256(data).hexdigest(),
    )
    assert tenants == ["t1"]  # pinned to the workspace
    assert short.added == [stored] and short.committed is True
    assert any("lock_timeout" in s for s in short.statements)
    assert any("pg_advisory_xact_lock" in s for s in short.statements)
    assert caller.merged == [stored] and caller.added == []


def test_store_file_over_the_cap_writes_nothing_anywhere(monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setattr(settings, "creative_storage_max_bytes_per_tenant", 100)
    short = _StorageSession()
    _storage_scope(monkeypatch, short, used=90)
    caller = _CallerSession()
    data = _png_bytes()
    with pytest.raises(uploads.StorageFull):
        uploads.store_file(
            caller, tenant_id="t1", data=data, mime="image/png", kind=FileKind.RENDER,  # type: ignore[arg-type]
            source=FileSource.RENDERED, sha256=hashlib.sha256(data).hexdigest(),
        )
    assert short.added == [] and caller.merged == []


def test_a_storage_lock_timeout_is_a_friendly_429(monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setattr(settings, "creative_storage_max_bytes_per_tenant", 10_000_000)
    short = _StorageSession(_timeout_error())
    _storage_scope(monkeypatch, short)
    data = _png_bytes()
    with pytest.raises(uploads.StorageBusy) as raised:
        uploads.store_file(
            _CallerSession(), tenant_id="t1", data=data, mime="image/png",  # type: ignore[arg-type]
            kind=FileKind.LOGO, source=FileSource.UPLOAD, sha256=hashlib.sha256(data).hexdigest(),
        )
    assert raised.value.status_code == 429 and "55P03" not in raised.value.message


# ── The stale-job sweeper ──────────────────────────────────────────────────
def test_the_sweeper_fails_only_old_queued_or_running_jobs() -> None:
    org = _org()
    db = FakeDb(org)
    now = utcnow()
    old = now - timedelta(seconds=settings.creative_render_time_limit + pipeline.STALE_GRACE_SECONDS + 5)
    fresh = now - timedelta(seconds=30)

    def job(status: str, created: Any, started: Any = None, tenant: str = org.id) -> CreativeJob:  # noqa: ANN401
        row = CreativeJob(id=new_id(), tenant_id=tenant, status=status, created_at=created,
                          started_at=started, error_code="", brief_encrypted="")
        db.add(row)
        return row

    stuck_queued = job(CreativeJobStatus.QUEUED, old)
    stuck_running = job(CreativeJobStatus.RUNNING, old, old)
    running_now = job(CreativeJobStatus.RUNNING, old, fresh)  # queued long ago, started recently
    queued_now = job(CreativeJobStatus.QUEUED, fresh)
    finished = job(CreativeJobStatus.SUCCEEDED, old, old)
    other_tenant = job(CreativeJobStatus.QUEUED, old, tenant=new_id())

    assert pipeline.sweep_tenant_stale_jobs(db, org.id, now=now) == 2
    for row in (stuck_queued, stuck_running):
        assert row.status == CreativeJobStatus.FAILED and row.error_code == "timed_out"
        assert row.finished_at is not None
    for row in (running_now, queued_now, other_tenant):
        assert row.status in (CreativeJobStatus.QUEUED, CreativeJobStatus.RUNNING)
    assert finished.status == CreativeJobStatus.SUCCEEDED
    assert pipeline.error_message("timed_out").startswith("This design took too long")


def test_a_stuck_revision_puts_the_previous_version_back(world) -> None:  # noqa: ANN001, F811
    _job, item, asset = _one_creative(world)
    approvals.request_changes(world.db, org=world.org, actor=_actor(world.org), item_id=item.id,
                              feedback="shorter")
    world.hooks.clear()  # the worker never picks the revision up
    revision = world.db.get(CreativeJob, world.db.info[pipeline.REVISION_JOB_KEY])
    revision.created_at = utcnow() - timedelta(hours=1)
    assert pipeline.sweep_tenant_stale_jobs(world.db, world.org.id) == 1
    assert revision.status == CreativeJobStatus.FAILED and revision.error_code == "timed_out"
    pending = [i for i in _rows(world, ApprovalItem) if i.status == ApprovalStatus.PENDING.value]
    assert len(pending) == 1 and pending[0].target_id == asset.id


def test_sweep_stale_jobs_runs_per_workspace(monkeypatch) -> None:  # noqa: ANN001
    import app.db.session as session_module

    seen: list[str] = []

    def fake_run_per_organization(db: Any, handler: Any, **kw: Any) -> list[int]:  # noqa: ANN401
        return [handler(db, tenant) for tenant in ("a", "b")]

    monkeypatch.setattr(session_module, "run_per_organization", fake_run_per_organization)
    monkeypatch.setattr(pipeline, "sweep_tenant_stale_jobs",
                        lambda db, tenant_id, now=None: seen.append(tenant_id) or 3)
    assert pipeline.sweep_stale_jobs(object()) == 6  # type: ignore[arg-type]
    assert seen == ["a", "b"]


# ── Each format saved as soon as it is drawn ───────────────────────────────
class SoftTimeLimitExceeded(Exception):  # noqa: N818 - the name Celery uses
    pass


def test_a_time_limit_part_way_keeps_the_formats_already_made(world, monkeypatch) -> None:  # noqa: ANN001, F811
    real_draw = pipeline.draw

    def draw(spec: Any, *a: Any, **k: Any) -> Any:  # noqa: ANN401
        if spec.id == SOCIAL:
            raise SoftTimeLimitExceeded()
        return real_draw(spec, *a, **k)

    monkeypatch.setattr(pipeline, "draw", draw)
    commits = world.db.commits
    job = _submit(world, [BANNER, SOCIAL])
    _run_hooks(world)
    assert job.status == CreativeJobStatus.FAILED and job.error_code == "render_timeout"
    made = _rows(world, MarketingAsset)
    assert len(made) == 1 and assets.content_of(made[0], OrgCipher.for_org(world.org))["format_id"] == BANNER
    assert [i.status for i in _rows(world, ApprovalItem)] == [ApprovalStatus.PENDING.value]
    assert [o["format_id"] for o in job.outputs] == [BANNER] and job.outputs[0]["asset_id"] == made[0].id
    assert world.db.commits > commits


def test_a_format_that_fails_to_save_removes_its_files(world, monkeypatch) -> None:  # noqa: ANN001, F811
    def broken(*_a: Any, **_k: Any) -> None:  # noqa: ANN401
        raise RuntimeError("queue down")

    monkeypatch.setattr(approvals, "queue_manual", broken)
    job = _submit(world, [BANNER])
    _run_hooks(world)
    assert job.status == CreativeJobStatus.FAILED
    renders = [r for r in _rows(world, StoredFile) if r.kind in (FileKind.RENDER, FileKind.PREVIEW)]
    assert len(renders) == 2 and sorted(world.discarded) == sorted(r.id for r in renders)


# ── Redraw failures ────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("error", "code"),
    [
        (uploads.StorageFull(uploads.STORAGE_FULL_MESSAGE), "storage_full"),
        (RuntimeError("disk on fire"), "render_failed"),
        (uploads.StorageBusy(), "busy"),
    ],
)
def test_a_redraw_that_cannot_save_clears_the_spinner(world, monkeypatch, error: Exception, code: str) -> None:  # noqa: ANN001, F811
    _job, _item, asset = _one_creative(world)
    before = asset.detail["preview_file_id"]

    def store_file(*_a: Any, **_k: Any) -> None:  # noqa: ANN401
        raise error

    monkeypatch.setattr(uploads, "store_file", store_file)
    asset.detail = {**asset.detail, "preview_pending": True}
    assert pipeline.redraw(world.db, world.org, asset) == {"status": "failed", "error_code": code}
    assert asset.detail["preview_pending"] is False and asset.detail["redraw_error"] == code
    assert asset.detail["preview_file_id"] == before


# ── Previews ───────────────────────────────────────────────────────────────
def test_a_digital_format_gets_its_own_small_preview(world) -> None:  # noqa: ANN001, F811
    job = _submit(world, [STORY])
    _run_hooks(world)
    assert job.status == CreativeJobStatus.SUCCEEDED, job.error_code
    asset = _rows(world, MarketingAsset)[0]
    files = {r.id: r for r in _rows(world, StoredFile)}
    render, preview = files[asset.detail["render_file_id"]], files[asset.detail["preview_file_id"]]
    assert render.id != preview.id
    assert render.kind == FileKind.RENDER and preview.kind == FileKind.PREVIEW
    assert max(preview.width, preview.height) <= pipeline.DIGITAL_PREVIEW_MAX_PX
    with Image.open(io.BytesIO(world.files[preview.id])) as img:
        assert max(img.size) <= pipeline.DIGITAL_PREVIEW_MAX_PX
    payload = approvals.read_payload(world.org, _rows(world, ApprovalItem)[0])
    assert payload["preview_file_id"] == preview.id != render.id


def test_store_drawn_never_reuses_the_render_as_the_preview(world) -> None:  # noqa: ANN001, F811
    data = _png((1600, 900))
    layout = SimpleNamespace()
    drawn = pipeline.Drawn(data, "image/png", 1600, 900, layout)  # type: ignore[arg-type]
    main, preview = pipeline.store_drawn(world.db, world.org.id, "job", get_format("x_post"), drawn)
    assert main.id != preview.id and preview.kind == FileKind.PREVIEW
    assert max(preview.width, preview.height) <= pipeline.DIGITAL_PREVIEW_MAX_PX
    pdf = pipeline.Drawn(b"%PDF-1.7", "application/pdf", 10, 10, layout)  # type: ignore[arg-type]
    with pytest.raises(RenderError):
        pipeline.store_drawn(world.db, world.org.id, "job", get_format("flyer_a4"), pdf)


@pytest.mark.parametrize("format_id", [BANNER, "email_header", "linkedin_post", STORY])
def test_digital_preview_longest_side_is_strictly_smaller_than_render(world, format_id: str) -> None:  # noqa: ANN001, F811
    job = _submit(world, [format_id])
    _run_hooks(world)
    assert job.status == CreativeJobStatus.SUCCEEDED, job.error_code
    files = {r.id: r for r in _rows(world, StoredFile)}
    asset = _rows(world, MarketingAsset)[0]
    render_file, preview = files[asset.detail["render_file_id"]], files[asset.detail["preview_file_id"]]
    assert max(preview.width, preview.height) < max(render_file.width, render_file.height)
    with Image.open(io.BytesIO(world.files[preview.id])) as img:
        assert max(img.size) < max(render_file.width, render_file.height)
        assert img.format == "JPEG"


def test_digital_preview_flattens_transparency_onto_white() -> None:
    img = Image.new("RGBA", (40, 20), (0, 0, 0, 0))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    data, _size = pipeline._downscale(buf.getvalue(), pipeline.DIGITAL_PREVIEW_MAX_PX, jpeg=True)
    with Image.open(io.BytesIO(data)) as out:
        assert out.format == "JPEG"
        assert max(out.size) < 40
        assert out.getpixel((0, 0)) == (255, 255, 255)


def test_print_preview_stays_png() -> None:
    img = Image.new("RGB", (80, 80), (10, 20, 30))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    data, _size = pipeline._downscale(buf.getvalue())
    with Image.open(io.BytesIO(data)) as out:
        assert out.format == "PNG"


def test_a_failed_job_discards_unreferenced_generated_backgrounds(world, monkeypatch) -> None:  # noqa: ANN001, F811
    monkeypatch.setattr(pipeline.images, "generate_bytes", lambda db_, org_, prompt, **kw: SimpleNamespace(
        data=_png((512, 512)), mime="image/png", width=512, height=512, provider="openai",
        model=kw["model_override"]))

    def boom(*_a: Any, **_k: Any) -> None:  # noqa: ANN401
        raise RenderError()

    monkeypatch.setattr(render, "render_creative", boom)
    discarded: list[str] = []

    def spy(db_: Any, *, tenant_id: str, file_ids: list[str]) -> None:  # noqa: ANN401
        discarded.extend(file_ids)
        world.discarded.extend(file_ids)

    monkeypatch.setattr(uploads, "discard_files", spy)
    job = _submit(world, [BANNER], image_model={"provider": "openai", "model": "img-1"})
    _run_hooks(world)
    assert job.status == CreativeJobStatus.FAILED
    backgrounds = [r for r in _rows(world, StoredFile) if r.kind == FileKind.BACKGROUND]
    assert len(backgrounds) == 1
    assert backgrounds[0].id in discarded


def test_a_successful_job_keeps_generated_backgrounds(world, monkeypatch) -> None:  # noqa: ANN001, F811
    monkeypatch.setattr(pipeline.images, "generate_bytes", lambda db_, org_, prompt, **kw: SimpleNamespace(
        data=_png((512, 512)), mime="image/png", width=512, height=512, provider="openai",
        model=kw["model_override"]))
    discarded: list[str] = []

    def spy(db_: Any, *, tenant_id: str, file_ids: list[str]) -> None:  # noqa: ANN401
        discarded.extend(file_ids)

    monkeypatch.setattr(uploads, "discard_files", spy)
    job = _submit(world, [BANNER], image_model={"provider": "openai", "model": "img-1"})
    _run_hooks(world)
    assert job.status == CreativeJobStatus.SUCCEEDED, job.error_code
    backgrounds = [r for r in _rows(world, StoredFile) if r.kind == FileKind.BACKGROUND]
    assert len(backgrounds) == 1
    assert backgrounds[0].id not in discarded


def test_a_partial_job_discards_only_unreferenced_generated_backgrounds(world, monkeypatch) -> None:  # noqa: ANN001, F811
    monkeypatch.setattr(pipeline.images, "generate_bytes", lambda db_, org_, prompt, **kw: SimpleNamespace(
        data=_png((512, 512)), mime="image/png", width=512, height=512, provider="openai",
        model=kw["model_override"]))
    real_draw = pipeline.draw

    def draw(spec: Any, *a: Any, **k: Any) -> Any:  # noqa: ANN401
        if spec.id == STORY:
            raise RenderError()
        return real_draw(spec, *a, **k)

    monkeypatch.setattr(pipeline, "draw", draw)
    discarded: list[str] = []

    def spy(db_: Any, *, tenant_id: str, file_ids: list[str]) -> None:  # noqa: ANN401
        discarded.extend(file_ids)
        world.discarded.extend(file_ids)

    monkeypatch.setattr(uploads, "discard_files", spy)
    job = _submit(world, [BANNER, STORY], image_model={"provider": "openai", "model": "img-1"})
    _run_hooks(world)
    assert job.status == CreativeJobStatus.SUCCEEDED, job.error_code
    backgrounds = [r for r in _rows(world, StoredFile) if r.kind == FileKind.BACKGROUND]
    assert len(backgrounds) == 2
    asset = _rows(world, MarketingAsset)[0]
    kept_id = asset.detail["background"]["file_id"]
    orphans = [r.id for r in backgrounds if r.id != kept_id]
    assert orphans
    assert discarded == orphans
    assert kept_id not in discarded


def test_a_late_success_does_not_overwrite_a_failed_job(world, monkeypatch) -> None:  # noqa: ANN001, F811
    real = runner.run_on_demand

    def flip(db: Any, *a: Any, **k: Any) -> Any:  # noqa: ANN401
        outcome = real(db, *a, **k)
        claimed = next(r for r in world.db.rows if isinstance(r, CreativeJob))
        claimed.status = CreativeJobStatus.FAILED
        claimed.error_code = "timed_out"
        return outcome

    monkeypatch.setattr(runner, "run_on_demand", flip)
    job = _submit(world, [BANNER])
    _run_hooks(world)
    assert job.status == CreativeJobStatus.FAILED
    assert job.error_code == "timed_out"
    files = [r for r in _rows(world, StoredFile) if r.kind in (FileKind.RENDER, FileKind.PREVIEW)]
    assert files
    assert {r.id for r in files} <= set(world.discarded)


# ── Request: null goal and tone ────────────────────────────────────────────
@pytest.mark.parametrize("value", [None, "omit"])
def test_null_or_missing_goal_and_tone_become_empty(world, value: Any) -> None:  # noqa: ANN001, ANN401, F811
    request = _request(world, [BANNER])
    body = request.model_dump()
    for name in ("goal", "tone"):
        if value == "omit":
            body.pop(name)
        else:
            body[name] = None
    parsed = pipeline.CreativeJobRequest.model_validate(body)
    assert parsed.goal == "" and parsed.tone == ""


@pytest.mark.parametrize("bad", [{"goal": 7}, {"tone": ["x"]}, {"goal": "g" * 101}])
def test_goal_and_tone_still_reject_bad_values(world, bad: dict) -> None:  # noqa: ANN001, F811
    body = {**_request(world, [BANNER]).model_dump(), **bad}
    with pytest.raises(ValidationError):
        pipeline.CreativeJobRequest.model_validate(body)


# ── AI images are cleaned once ─────────────────────────────────────────────
def test_an_ai_image_is_normalised_once(world, monkeypatch) -> None:  # noqa: ANN001, F811
    calls: list[int] = []
    real = uploads.normalise_raster

    def counting(data: bytes) -> Any:  # noqa: ANN401
        calls.append(len(data))
        return real(data)

    monkeypatch.setattr(uploads, "normalise_raster", counting)
    monkeypatch.setattr(pipeline.images, "generate_bytes", lambda db_, org_, prompt, **kw: SimpleNamespace(
        data=_png((512, 512)), mime="image/png", width=512, height=512, provider="openai",
        model=kw["model_override"]))
    job = _submit(world, [BANNER], image_model={"provider": "openai", "model": "img-1"})
    _run_hooks(world)
    assert job.status == CreativeJobStatus.SUCCEEDED, job.error_code
    assert len(calls) == 1
    assert [r.kind for r in _rows(world, StoredFile)].count(FileKind.BACKGROUND) == 1


# ── Claim text hashes ──────────────────────────────────────────────────────
def _with_claim(world: SimpleNamespace) -> MarketingAsset:  # noqa: F811
    _job, _item, asset = _one_creative(world)
    hooks.restore_creative_text(world.db, world.org, asset.id, GOOD_TEXT, user=None)
    _run_hooks(world)
    assert asset.detail["redraw_error"] == ""
    return asset


def test_a_redraw_is_refused_when_a_claim_text_changed(world) -> None:  # noqa: ANN001, F811
    asset = _with_claim(world)
    world.product.approved_claims = [world.product.approved_claims[0], "Ships in a box we can reuse"]
    before = asset.detail["preview_file_id"]
    assert pipeline.redraw(world.db, world.org, asset) == {"status": "failed", "error_code": "claims_changed"}
    assert asset.detail["redraw_error"] == "claims_changed" and asset.detail["preview_pending"] is False
    assert asset.detail["preview_file_id"] == before
    assert "claims" in pipeline.error_message("claims_changed")


def test_a_redraw_with_unchanged_claims_goes_ahead(world) -> None:  # noqa: ANN001, F811
    asset = _with_claim(world)
    before = asset.detail["preview_file_id"]
    assert pipeline.redraw(world.db, world.org, asset) == {"status": "ok"}
    assert asset.detail["preview_file_id"] != before


def test_new_designs_record_their_claim_hashes(world) -> None:  # noqa: ANN001, F811
    _job, _item, asset = _one_creative(world)
    copy = assets.content_of(asset, OrgCipher.for_org(world.org))["copy"]
    assert asset.detail["claim_hashes"] == pipeline.claim_hashes(world.product.approved_claims, copy["claim_ids"])


# ── Real PostgreSQL ────────────────────────────────────────────────────────
def _committed_job(org_id: str, **kw: Any) -> str:  # noqa: ANN401
    from app.db.session import SessionLocal, bind_tenant

    session = SessionLocal()
    try:
        bind_tenant(session, org_id)
        row = CreativeJob(tenant_id=org_id, created_by="u1", **kw)
        session.add(row)
        session.commit()
        return row.id
    finally:
        session.close()


@requires_db
def test_db_a_running_job_does_not_hold_the_quota_lock(org) -> None:  # noqa: ANN001
    from app.db.session import SessionLocal, bind_tenant

    job_id = _committed_job(org.id, status=CreativeJobStatus.RUNNING)
    worker = SessionLocal()
    try:
        bind_tenant(worker, org.id)
        job = worker.get(CreativeJob, job_id)
        quotas.reserve_tokens(worker, job, 100)
        # The worker's own transaction stays open, as during a long job.

        def admit() -> float:
            started = time.monotonic()
            session = SessionLocal()
            try:
                bind_tenant(session, org.id)
                quotas.admit_job(session, CreativeJob(tenant_id=org.id, created_by="u2"))
                session.commit()
            finally:
                session.close()
            return time.monotonic() - started

        with ThreadPoolExecutor(max_workers=1) as pool:
            elapsed = pool.submit(admit).result(timeout=20)
        assert elapsed < 3
        assert job.tokens_out == 100
    finally:
        worker.rollback()
        worker.close()

    check = SessionLocal()
    try:
        bind_tenant(check, org.id)
        assert check.get(CreativeJob, job_id).tokens_out == 100  # committed on its own
    finally:
        check.close()


@requires_db
def test_db_admission_gives_up_with_429_when_the_lock_is_held(org, monkeypatch) -> None:  # noqa: ANN001
    from app.db.session import SessionLocal, bind_tenant

    monkeypatch.setattr(quotas, "REQUEST_LOCK_TIMEOUT", "300ms")
    holder = SessionLocal()
    waiter = SessionLocal()
    try:
        bind_tenant(holder, org.id)
        quotas._lock(holder, org.id)
        bind_tenant(waiter, org.id)
        started = time.monotonic()
        with pytest.raises(quotas.QuotaBusy):
            quotas.admit_job(waiter, CreativeJob(tenant_id=org.id, created_by="u1"))
        assert time.monotonic() - started < 5
    finally:
        waiter.rollback()
        holder.rollback()
        waiter.close()
        holder.close()


@requires_db
def test_db_concurrent_reservations_never_overshoot(org, monkeypatch) -> None:  # noqa: ANN001
    from app.db.session import SessionLocal, bind_tenant

    monkeypatch.setattr(settings, "creative_images_per_month", 3)
    job_id = _committed_job(org.id, status=CreativeJobStatus.RUNNING)

    def attempt(_: int) -> bool:
        session = SessionLocal()
        try:
            bind_tenant(session, org.id)
            quotas.reserve_images(session, session.get(CreativeJob, job_id), 1)
            return True
        except quotas.QuotaExceeded:
            return False
        finally:
            session.rollback()
            session.close()

    with ThreadPoolExecutor(max_workers=6) as pool:
        assert sum(pool.map(attempt, range(6))) == 3
    check = SessionLocal()
    try:
        bind_tenant(check, org.id)
        assert check.get(CreativeJob, job_id).images_generated == 3
    finally:
        check.close()


@requires_db
def test_db_store_file_does_not_hold_the_storage_lock_for_the_caller(org) -> None:  # noqa: ANN001
    from app.db.session import SessionLocal, bind_tenant

    data = _png_bytes()
    digest = hashlib.sha256(data).hexdigest()
    caller = SessionLocal()
    try:
        bind_tenant(caller, org.id)
        first = uploads.store_file(caller, tenant_id=org.id, data=data, mime="image/png",
                                   kind=FileKind.LOGO, source=FileSource.UPLOAD, sha256=digest)
        # The caller has not committed; another write must not wait for it.

        def other() -> str:
            session = SessionLocal()
            try:
                bind_tenant(session, org.id)
                return uploads.store_file(session, tenant_id=org.id, data=data, mime="image/png",
                                          kind=FileKind.LOGO, source=FileSource.UPLOAD, sha256=digest).id
            finally:
                session.close()

        with ThreadPoolExecutor(max_workers=1) as pool:
            second = pool.submit(other).result(timeout=8)
        assert second != first.id
    finally:
        caller.rollback()
        caller.close()


@requires_db
def test_db_store_file_gives_up_with_429_when_the_storage_lock_is_held(org, monkeypatch) -> None:  # noqa: ANN001
    from sqlalchemy import text

    from app.db.session import SessionLocal, bind_tenant

    monkeypatch.setattr(uploads, "STORAGE_LOCK_TIMEOUT", "300ms")
    holder = SessionLocal()
    caller = SessionLocal()
    data = _png_bytes()
    try:
        bind_tenant(holder, org.id)
        holder.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                       {"key": f"stored_files:{org.id}"})
        bind_tenant(caller, org.id)
        with pytest.raises(uploads.StorageBusy):
            uploads.store_file(caller, tenant_id=org.id, data=data, mime="image/png", kind=FileKind.LOGO,
                               source=FileSource.UPLOAD, sha256=hashlib.sha256(data).hexdigest())
    finally:
        holder.rollback()
        caller.rollback()
        holder.close()
        caller.close()


@requires_db
def test_db_the_sweeper_fails_stuck_jobs_in_every_workspace(org, second_org) -> None:  # noqa: ANN001
    from app.db.session import SessionLocal, bind_tenant

    old = utcnow() - timedelta(hours=2)
    stuck_a = _committed_job(org.id, status=CreativeJobStatus.QUEUED, created_at=old)
    stuck_b = _committed_job(second_org.id, status=CreativeJobStatus.RUNNING, created_at=old, started_at=old)
    fresh = _committed_job(org.id, status=CreativeJobStatus.QUEUED)

    session = SessionLocal()
    try:
        assert pipeline.sweep_stale_jobs(session) == 2
    finally:
        session.close()

    check = SessionLocal()
    try:
        for tenant, job_id, status in (
            (org.id, stuck_a, CreativeJobStatus.FAILED),
            (second_org.id, stuck_b, CreativeJobStatus.FAILED),
            (org.id, fresh, CreativeJobStatus.QUEUED),
        ):
            bind_tenant(check, tenant)
            row = check.get(CreativeJob, job_id)
            assert row.status == status
            if status == CreativeJobStatus.FAILED:
                assert row.error_code == "timed_out"
            check.rollback()
            check.expunge_all()
    finally:
        check.close()
