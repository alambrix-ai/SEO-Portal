"""Version hooks for brand kits, products and site analyses (step 5.8).

Most tests replace the database and the history store with stand-ins, so
they run anywhere: numbering, identical saves, restore re-validation, files
that are gone or not this workspace's, and that a history failure never
blocks a save. The tests at the end prove the same against PostgreSQL and
need ``TEST_DATABASE_URL``.
"""
from __future__ import annotations

import json
import logging
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager, nullcontext
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from app.core.exceptions import InvalidInputError, NotFoundError
from app.models.creative import BrandKit, FileKind, Product
from app.models.workspace import Organization
from app.services import brand_kits, history
from app.services.brand_kits import KitIn, KitPatch, ProductIn, ProductPatch
from app.services.history import site_analysis, store
from tests.conftest import requires_db

TENANT_A = "00000000-0000-4000-8000-00000000000a"
TENANT_B = "00000000-0000-4000-8000-00000000000b"
USER = SimpleNamespace(id="22222222-2222-4222-8222-222222222222", name="Asha", tenant_id=TENANT_A)
ORG = SimpleNamespace(id=TENANT_A, settings={})
LOGO_A = "aaaaaaaa-0000-4000-8000-000000000001"
PHOTO_A = "aaaaaaaa-0000-4000-8000-000000000003"
LOGO_B = "bbbbbbbb-0000-4000-8000-000000000001"
PHOTO_B = "bbbbbbbb-0000-4000-8000-000000000002"
NOW = datetime(2026, 10, 21, tzinfo=UTC)
SECRET_WORDS = "Our secret launch voice"

#: (tenant, file id) -> kind: the files each workspace really has.
FILES: dict[tuple[str, str], str] = {
    (TENANT_A, LOGO_A): FileKind.LOGO,
    (TENANT_A, PHOTO_A): FileKind.PRODUCT_PHOTO,
    (TENANT_B, LOGO_B): FileKind.LOGO,
    (TENANT_B, PHOTO_B): FileKind.PRODUCT_PHOTO,
}


# ── Stand-ins ──────────────────────────────────────────────────────────────
class _Result:
    def scalar_one(self) -> int:
        return 0

    def scalar_one_or_none(self) -> None:
        return None

    def scalars(self) -> list[Any]:
        return []


class _Db:
    """Every read finds nothing; the workspace row is the only thing ``get`` returns."""

    def __init__(self) -> None:
        self.added: list[Any] = []
        self.info: dict[str, Any] = {}

    def execute(self, stmt: Any) -> _Result:  # noqa: ANN401
        return _Result()

    def get(self, model: type, key: str) -> Any:  # noqa: ANN401
        return ORG if model is Organization and key == ORG.id else None

    def add(self, obj: Any) -> None:  # noqa: ANN401
        self.added.append(obj)

    def flush(self) -> None:
        """Apply the column defaults a real flush would."""
        for obj in self.added:
            for column in type(obj).__table__.columns:
                if getattr(obj, column.key, None) is not None or column.default is None:
                    continue
                if column.key in ("created_at", "updated_at"):
                    setattr(obj, column.key, NOW)
                elif column.default.is_callable:
                    setattr(obj, column.key, column.default.arg(None))
                else:
                    setattr(obj, column.key, column.default.arg)

    def begin_nested(self) -> Any:  # noqa: ANN401
        return nullcontext()


class _Cipher:
    def encrypt_json(self, document: Any, *, context: str) -> str:  # noqa: ANN401
        return "enc:" + json.dumps(document)

    def decrypt_json(self, token: str, *, context: str, default: Any = None) -> Any:  # noqa: ANN401
        return json.loads(token[4:]) if token.startswith("enc:") else default


def _fake_get_file(db: Any, tenant_id: str, file_id: str, kinds: Iterable[str] | None = None) -> Any:  # noqa: ANN401
    kind = FILES.get((tenant_id, file_id))
    if kind is None or (kinds is not None and kind not in tuple(kinds)):
        return None
    return SimpleNamespace(id=file_id, tenant_id=tenant_id, kind=kind)


def _fake_owned(db: Any, tenant_id: str, file_ids: Sequence[str], kind: str) -> set[str]:  # noqa: ANN401
    return {f for f in file_ids if FILES.get((tenant_id, f)) == kind}


@pytest.fixture
def versions(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """An in-memory version store: numbered per subject, documents "encrypted"."""
    state: dict[str, Any] = {"rows": [], "audit": []}

    def latest_of(db: Any, tenant: str, kind: str, subject: str) -> Any:  # noqa: ANN401
        rows = [r for r in state["rows"] if (r.tenant_id, r.kind, r.subject_id) == (tenant, kind, subject)]
        return rows[-1] if rows else None

    def record(db: Any, **kw: Any) -> SimpleNamespace:  # noqa: ANN401
        version = 1 + sum(
            1 for r in state["rows"] if (r.tenant_id, r.kind, r.subject_id) == (kw["tenant_id"], kw["kind"], kw["subject_id"])
        )
        row = SimpleNamespace(
            id=f"v{len(state['rows']) + 1}", version=version, created_at=NOW, items={}, item_count=0, run_id="",
            **{k: kw.get(k, "") for k in ("tenant_id", "kind", "subject_id", "source", "label", "created_by",
                                          "created_by_name", "actor_type", "agent_slug", "model", "payload_encrypted")},
            inputs=kw.get("inputs") or {},
        )
        state["rows"].append(row)
        return row

    def get(db: Any, tenant: str, kind: str, entry_id: str) -> Any:  # noqa: ANN401
        for row in state["rows"]:
            if row.id == entry_id and row.tenant_id == tenant and row.kind == kind:
                return row
        raise NotFoundError("That history entry was not found")

    monkeypatch.setattr(history, "_cipher", lambda org: _Cipher())
    monkeypatch.setattr(store, "latest_of", latest_of)
    monkeypatch.setattr(store, "record", record)
    monkeypatch.setattr(store, "get", get)
    from app.services import audit

    monkeypatch.setattr(audit, "record_user_action", lambda db, **kw: state["audit"].append(kw))
    monkeypatch.setattr(brand_kits, "get_file", _fake_get_file)
    monkeypatch.setattr(brand_kits, "owned_file_ids", _fake_owned)
    return state


def _document(row: Any) -> dict[str, Any]:  # noqa: ANN401
    return json.loads(row.payload_encrypted[4:])


def _add_version(state: dict[str, Any], kind: str, subject: str, document: dict[str, Any], *, tenant: str = TENANT_A) -> Any:  # noqa: ANN401
    return store.record(
        None, tenant_id=tenant, kind=kind, subject_id=subject, source="saved", metrics={}, items={},
        payload_encrypted="enc:" + json.dumps(document),
    )


# ── Recording ──────────────────────────────────────────────────────────────
def test_kit_saves_are_numbered_and_identical_saves_record_nothing(
    versions: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    db = _Db()
    kit = brand_kits.create_kit(db, TENANT_A, KitIn(name="Main", logo_file_id=LOGO_A), actor=USER)  # type: ignore[arg-type]
    monkeypatch.setattr(brand_kits, "get_kit", lambda _db, _t, _k: kit)
    brand_kits.update_kit(db, TENANT_A, kit.id, KitPatch(voice=SECRET_WORDS), actor=USER)  # type: ignore[arg-type]
    brand_kits.update_kit(db, TENANT_A, kit.id, KitPatch(voice=SECRET_WORDS), actor=USER)  # type: ignore[arg-type]
    brand_kits.update_kit(db, TENANT_A, kit.id, KitPatch(primary_color="#FF0000"), actor=USER)  # type: ignore[arg-type]

    rows = versions["rows"]
    assert [r.version for r in rows] == [1, 2, 3]  # the identical save made no version
    assert {r.kind for r in rows} == {"brand_kit"} and {r.subject_id for r in rows} == {kit.id}
    assert all(r.actor_type == "user" and r.created_by == USER.id for r in rows)
    latest = _document(rows[-1])
    assert set(latest) == set(brand_kits.KIT_VERSION_FIELDS)
    assert latest["primary_color"] == "#ff0000" and latest["logo_file_id"] == LOGO_A
    # The customer's words are kept encrypted, never in the clear inputs.
    assert SECRET_WORDS not in json.dumps(rows[-1].inputs)


def test_product_saves_are_numbered_and_hold_ids_not_bytes(
    versions: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    db = _Db()
    product = brand_kits.create_product(  # type: ignore[arg-type]
        db, TENANT_A, ProductIn(name="Plan", image_file_ids=[PHOTO_A], approved_claims=["Free setup"]), actor=USER
    )
    monkeypatch.setattr(brand_kits, "get_product", lambda _db, _t, _p: product)
    brand_kits.update_product(db, TENANT_A, product.id, ProductPatch(name="Plan"), actor=USER)  # type: ignore[arg-type]
    brand_kits.update_product(db, TENANT_A, product.id, ProductPatch(price_text="£9"), actor=USER)  # type: ignore[arg-type]
    rows = versions["rows"]
    assert [r.version for r in rows] == [1, 2]
    document = _document(rows[-1])
    assert set(document) == set(brand_kits.PRODUCT_VERSION_FIELDS)
    assert document["image_file_ids"] == [PHOTO_A] and document["price_text"] == "£9"
    assert all(isinstance(v, (str, bool, list, type(None))) for v in document.values())


def test_a_history_failure_never_blocks_the_save(
    versions: dict[str, Any], monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def broken(*args: Any, **kwargs: Any) -> None:  # noqa: ANN401
        raise RuntimeError(f"history table missing {SECRET_WORDS}")

    monkeypatch.setattr(history, "record_version", broken)
    caplog.set_level(logging.DEBUG)
    db = _Db()
    kit = brand_kits.create_kit(db, TENANT_A, KitIn(name="Main", voice=SECRET_WORDS), actor=USER)  # type: ignore[arg-type]
    product = brand_kits.create_product(db, TENANT_A, ProductIn(name="Plan"), actor=USER)  # type: ignore[arg-type]
    assert db.added == [kit, product]
    assert versions["rows"] == []
    assert "Could not record history" in caplog.text


# ── Restore ────────────────────────────────────────────────────────────────
def _kit(**fields: Any) -> BrandKit:  # noqa: ANN401
    values = {"id": "cccccccc-0000-4000-8000-000000000001", "tenant_id": TENANT_A, "name": "Main",
              "is_default": True, "primary_color": "#111111", "heading_font": "inter", "body_font": "inter",
              "voice": "", "banned_phrases": [], "disclaimer": "", "logo_file_id": None, "logo_dark_file_id": None,
              "secondary_color": "#4b5563", "accent_color": "#2563eb", "text_color": "#111827",
              "background_color": "#ffffff"}
    values.update(fields)
    return BrandKit(**values)


def test_restoring_a_kit_brings_back_its_colours_and_fonts_and_records_a_version(
    versions: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    kit = _kit()
    monkeypatch.setattr(brand_kits, "get_kit", lambda _db, tenant, kit_id: kit if tenant == TENANT_A else None)
    old = _add_version(versions, "brand_kit", kit.id, {**brand_kits.kit_document(kit), "primary_color": "#ABCDEF",
                                                       "heading_font": "lora", "is_default": False})
    _add_version(versions, "brand_kit", kit.id, brand_kits.kit_document(kit))

    out = history.restore(_Db(), ORG, "brand_kit", old.id, user=USER)
    assert kit.primary_color == "#abcdef" and kit.heading_font == "lora"
    assert kit.is_default is True  # "make default" alone changes the default
    new = versions["rows"][-1]
    assert new.version == 3 and new.source == "restored" and out["entry"]["version"] == 3
    assert _document(new)["primary_color"] == "#abcdef"


@pytest.mark.parametrize(
    ("field", "value", "words"),
    [
        ("primary_color", "red;background:url(https://evil.example)", "main colour"),
        ("heading_font", "../../etc/passwd", "heading font"),
        ("banned_phrases", ["x"] * 500, "words to avoid"),
    ],
)
def test_an_invalid_old_version_is_refused_in_plain_words(
    versions: dict[str, Any], monkeypatch: pytest.MonkeyPatch, field: str, value: Any, words: str  # noqa: ANN401
) -> None:
    kit = _kit()
    monkeypatch.setattr(brand_kits, "get_kit", lambda _db, _t, _k: kit)
    bad = _add_version(versions, "brand_kit", kit.id, {**brand_kits.kit_document(kit), field: value})
    _add_version(versions, "brand_kit", kit.id, brand_kits.kit_document(kit))
    before = brand_kits.kit_document(kit)

    with pytest.raises(InvalidInputError) as caught:
        history.restore(_Db(), ORG, "brand_kit", bad.id, user=USER)
    assert caught.value.status_code == 422
    assert words in caught.value.message
    assert caught.value.details == {"field": field}
    assert "evil" not in caught.value.message and "passwd" not in caught.value.message
    assert brand_kits.kit_document(kit) == before
    assert len(versions["rows"]) == 2 and versions["audit"] == []


def test_an_invalid_restored_colour_names_the_fix(versions: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    kit = _kit()
    monkeypatch.setattr(brand_kits, "get_kit", lambda _db, _t, _k: kit)
    bad = _add_version(versions, "brand_kit", kit.id, {**brand_kits.kit_document(kit), "accent_color": "blue"})
    _add_version(versions, "brand_kit", kit.id, brand_kits.kit_document(kit))
    with pytest.raises(InvalidInputError) as caught:
        history.restore(_Db(), ORG, "brand_kit", bad.id, user=USER)
    assert caught.value.message == (
        "This version can't be restored because its accent colour is no longer allowed. "
        "Please use a colour like #1a2b3c."
    )


def test_another_workspaces_logo_in_an_old_version_is_left_out(
    versions: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    kit = _kit(logo_file_id=LOGO_A)
    monkeypatch.setattr(brand_kits, "get_kit", lambda _db, _t, _k: kit)
    old = _add_version(versions, "brand_kit", kit.id, {**brand_kits.kit_document(kit), "logo_file_id": LOGO_B,
                                                       "voice": "Older"})
    _add_version(versions, "brand_kit", kit.id, brand_kits.kit_document(kit))
    db = _Db()
    out = history.restore(db, ORG, "brand_kit", old.id, user=USER)
    assert kit.logo_file_id is None and kit.voice == "Older"
    assert out["notes"] == [brand_kits.LOGO_LEFT_OUT]
    assert brand_kits.RESTORE_NOTES_KEY not in db.info
    assert _document(versions["rows"][-1])["logo_file_id"] is None


def test_another_workspaces_photo_in_an_old_product_version_is_left_out(
    versions: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    product = Product(id="dddddddd-0000-4000-8000-000000000001", tenant_id=TENANT_A, name="Plan", description="",
                      url="", price_text="", approved_claims=[], image_file_ids=[], disclaimer="", is_archived=False)
    monkeypatch.setattr(brand_kits, "get_product", lambda _db, _t, _p: product)
    old = _add_version(versions, "product", product.id, {**brand_kits.product_document(product),
                                                         "image_file_ids": [PHOTO_A, PHOTO_B, LOGO_A]})
    _add_version(versions, "product", product.id, brand_kits.product_document(product))
    db = _Db()
    out = history.restore(db, ORG, "product", old.id, user=USER)
    assert product.image_file_ids == [PHOTO_A]
    assert out["notes"] == [brand_kits.PHOTOS_LEFT_OUT.format(n=2)]
    assert brand_kits.RESTORE_NOTES_KEY not in db.info


def test_a_restored_product_url_is_rechecked(versions: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    product = Product(id="dddddddd-0000-4000-8000-000000000001", tenant_id=TENANT_A, name="Plan", description="",
                      url="", price_text="", approved_claims=[], image_file_ids=[], disclaimer="", is_archived=False)
    monkeypatch.setattr(brand_kits, "get_product", lambda _db, _t, _p: product)
    old = _add_version(versions, "product", product.id, {**brand_kits.product_document(product),
                                                         "url": "javascript:alert(1)"})
    _add_version(versions, "product", product.id, brand_kits.product_document(product))
    with pytest.raises(InvalidInputError) as caught:
        history.restore(_Db(), ORG, "product", old.id, user=USER)
    assert "web page" in caught.value.message and product.url == ""


def test_restoring_another_workspaces_kit_is_not_found(versions: dict[str, Any]) -> None:
    # The version belongs to this workspace's history, but the kit lookup is tenant-filtered.
    old = _add_version(versions, "brand_kit", "cccccccc-0000-4000-8000-000000000009", {"name": "Theirs"})
    _add_version(versions, "brand_kit", "cccccccc-0000-4000-8000-000000000009", {"name": "Now"})
    with pytest.raises(NotFoundError):
        history.restore(_Db(), ORG, "brand_kit", old.id, user=USER)
    # And another workspace's version is not found at all.
    theirs = _add_version(versions, "brand_kit", "cccccccc-0000-4000-8000-000000000001", {"name": "X"}, tenant=TENANT_B)
    with pytest.raises(NotFoundError):
        history.restore(_Db(), ORG, "brand_kit", theirs.id, user=USER)


def test_restorers_are_registered_for_kits_and_products() -> None:
    assert history.RESTORERS["brand_kit"] is brand_kits.restore_kit_version
    assert history.RESTORERS["product"] is brand_kits.restore_product_version


# ── Site analysis ──────────────────────────────────────────────────────────
class _SiteDb:
    def __init__(self, row: Any, user: Any = None) -> None:  # noqa: ANN401
        self.row, self.user, self.commits, self.queries = row, user, 0, []

    def get(self, model: type, key: str) -> Any:  # noqa: ANN401
        from app.models.demo import SiteAnalysis

        return {SiteAnalysis: self.row, Organization: ORG}.get(model)

    def scalar(self, stmt: Any) -> Any:  # noqa: ANN401
        self.queries.append(str(stmt))
        return self.user

    def begin_nested(self) -> Any:  # noqa: ANN401
        return nullcontext()

    def commit(self) -> None:
        self.commits += 1


REPORT = {"overall": {"score": 71, "grade": "C"}, "checks": [{"id": "title", "status": "fail", "title": "Title"}]}


def _run_site(monkeypatch: pytest.MonkeyPatch, db: _SiteDb, *, report: Any = REPORT) -> list[dict[str, Any]]:  # noqa: ANN401
    from app.services.demo_analysis import service, workspace

    calls: list[dict[str, Any]] = []

    @contextmanager
    def scope(tenant_id: str) -> Iterator[_SiteDb]:
        yield db

    def build_report(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        if isinstance(report, Exception):
            raise report
        return report

    monkeypatch.setattr("app.db.session.session_scope", scope)
    monkeypatch.setattr(
        "app.services.connectors.build_connector_bundle",
        lambda db, org: SimpleNamespace(instances={}, names={}, close=lambda: None),
    )
    monkeypatch.setattr("app.llm.from_connector.provider_from_connector", lambda *a, **k: object())
    monkeypatch.setattr(service, "build_report", build_report)
    monkeypatch.setattr(history, "record_version", lambda *a, **kw: calls.append({"args": a, **kw}))
    workspace.run("a1", TENANT_A)
    return calls


def _site_row(requested_by: str = USER.id) -> SimpleNamespace:
    return SimpleNamespace(
        id="eeeeeeee-0000-4000-8000-000000000001", tenant_id=TENANT_A, status="queued", started_at=None,
        finished_at=None, error="", report=None, website_url="https://www.example.com", business_url="https://maps.example",
        model="Claude", context={"llm": {"slug": "anthropic"}}, requested_by=requested_by,
        progress=[{"key": "fetch", "state": "pending", "detail": ""}],
    )


def test_a_finished_site_analysis_records_a_version_by_the_requester(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.models.demo import DemoStatus

    row = _site_row()
    db = _SiteDb(row, user=USER)
    calls = _run_site(monkeypatch, db)
    assert row.status == DemoStatus.DONE and len(calls) == 1
    _db, org, kind, subject, payload = calls[0]["args"]
    assert kind == "site_analysis" and org is ORG
    assert subject == site_analysis.subject_for("https://example.com")
    assert payload == {"analysis_id": row.id, "website_url": row.website_url,
                       "business_url": row.business_url, "report": REPORT}
    assert calls[0]["actor"] is USER and calls[0]["model"] == "Claude"
    assert "users.tenant_id" in db.queries[0]  # the requester is looked up in this workspace only


def test_a_site_analysis_without_a_known_requester_is_credited_to_the_system(monkeypatch: pytest.MonkeyPatch) -> None:
    db = _SiteDb(_site_row(requested_by="not-a-user-id"))
    calls = _run_site(monkeypatch, db)
    assert calls[0]["actor"] is None and db.queries == []


def test_a_failed_site_analysis_records_no_version(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core.exceptions import AppError
    from app.models.demo import DemoStatus

    row = _site_row()
    calls = _run_site(monkeypatch, _SiteDb(row), report=AppError("We could not reach that website."))
    assert row.status == DemoStatus.FAILED and calls == []


def test_a_site_history_failure_does_not_fail_the_analysis(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.models.demo import DemoStatus
    from app.services.demo_analysis import workspace

    row = _site_row()
    db = _SiteDb(row)
    _run_site(monkeypatch, db)  # installs the stand-ins

    def broken(*args: Any, **kwargs: Any) -> None:  # noqa: ANN401
        raise RuntimeError("no history table")

    row.status = "queued"
    monkeypatch.setattr(history, "record_version", broken)
    workspace.run("a1", TENANT_A)
    assert row.status == DemoStatus.DONE and row.report == REPORT and db.commits > 0


# ── Against PostgreSQL ─────────────────────────────────────────────────────
def _db_png(db: Any, tenant_id: str, kind: str) -> Any:  # noqa: ANN401
    import hashlib
    import io

    from PIL import Image

    from app.models.creative import FileSource
    from app.services import uploads

    out = io.BytesIO()
    Image.new("RGB", (16, 12), "red").save(out, format="PNG")
    data = out.getvalue()
    return uploads.store_file(
        db, tenant_id=tenant_id, data=data, mime="image/png", kind=kind, source=FileSource.UPLOAD,
        sha256=hashlib.sha256(data).hexdigest(), display_name="logo.png",
    )


@requires_db
def test_db_kit_versions_restore_and_tenant_isolation(clean_db, org, second_org) -> None:  # noqa: ANN001
    from app.db.session import bind_tenant
    from app.models.workspace import User

    bind_tenant(clean_db, org.id)
    user = clean_db.query(User).filter(User.tenant_id == org.id).first()
    logo = _db_png(clean_db, org.id, FileKind.LOGO)
    kit = brand_kits.create_kit(clean_db, org.id, KitIn(name="Main", logo_file_id=logo.id), actor=user)
    brand_kits.update_kit(clean_db, org.id, kit.id, KitPatch(primary_color="#ff0000", heading_font="lora"), actor=user)
    brand_kits.update_kit(clean_db, org.id, kit.id, KitPatch(primary_color="#ff0000"), actor=user)
    clean_db.commit()
    page = history.history(clean_db, org, "brand_kit", 10, subject=kit.id)["entries"]
    assert [e["version"] for e in page] == [2, 1]

    first = next(r for r in store.recent(clean_db, org.id, "brand_kit", 10) if r.version == 1)
    history.restore(clean_db, org, "brand_kit", first.id, user=user)
    clean_db.commit()
    clean_db.refresh(kit)
    assert kit.primary_color == "#1f2937" and kit.heading_font == "inter"
    assert store.latest_of(clean_db, org.id, "brand_kit", kit.id).version == 3

    # Another workspace can neither see nor restore this kit's versions.
    bind_tenant(clean_db, second_org.id)
    with pytest.raises(NotFoundError):
        history.restore(clean_db, second_org, "brand_kit", first.id, user=user)
