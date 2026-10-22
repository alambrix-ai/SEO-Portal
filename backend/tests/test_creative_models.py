"""Creative Studio data model: validation, constraints, migration and isolation.

Colours and fonts are written into rendered HTML and CSS, where autoescaping
does not protect anything, so the tests below try to smuggle CSS and markup
through both and expect a refusal. The migration cannot be run here (there is
no disposable database in this environment), so it is executed against a
recording stand-in for ``alembic.op`` and its schema compared with the models.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from sqlalchemy import CheckConstraint, ForeignKeyConstraint, Index
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable

from app.models.content import AssetKind, AssetStatus
from app.models.creative import (
    FONT_IDS,
    HEX_COLOUR_SQL,
    STORED_FILE_MIMES,
    BrandKit,
    CreativeJob,
    Product,
    StoredFile,
    check_font_id,
    normalise_hex,
)
from app.services.encryption import Ctx, OrgCipher
from tests.conftest import requires_db

VERSIONS = Path(__file__).resolve().parents[1] / "alembic" / "versions"
CREATIVE_TABLES = ("stored_files", "brand_kits", "products", "creative_jobs")
GOOD_SHA = "a" * 64


def _load_migration(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"m_{name}", VERSIONS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ── Colours ────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("#1A2B3C", "#1a2b3c"),
        ("#ffffff", "#ffffff"),
        ("#00AA00", "#00aa00"),
    ],
)
def test_hex_colours_are_accepted_and_normalised(raw: str, expected: str) -> None:
    assert normalise_hex(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "#fff",  # short form
        "red",
        "rgb(1,2,3)",
        "#12345g",
        "#1234567",
        "",
        "#123456;}body{background:url(https://evil.example/x)",
        "#123\n456",
        # Python's `$` matches before a final newline; the validator must not.
        "#123456\n",
        "#123456\r\n",
        " #123456",
        "#123456 ",
        "</style><script>alert(1)</script>",
        None,
        123456,
    ],
)
def test_anything_but_a_six_digit_hex_colour_is_refused(raw: Any) -> None:  # noqa: ANN401
    with pytest.raises(ValueError):
        normalise_hex(raw)


def test_brand_kit_normalises_colours_as_they_are_set() -> None:
    kit = BrandKit(name="Main", primary_color="#ABCDEF")
    assert kit.primary_color == "#abcdef"


@pytest.mark.parametrize(
    "column",
    ["primary_color", "secondary_color", "accent_color", "text_color", "background_color"],
)
def test_brand_kit_refuses_css_injection_in_every_colour(column: str) -> None:
    with pytest.raises(ValueError):
        BrandKit(name="Main", **{column: "#000000;color:red"})


# ── Fonts ──────────────────────────────────────────────────────────────────
def test_font_allow_list_is_exactly_the_shipped_fonts() -> None:
    assert FONT_IDS == (
        "inter",
        "lato",
        "poppins",
        "montserrat",
        "lora",
        "playfair_display",
        "oswald",
        "bebas_neue",
    )
    for font in FONT_IDS:
        assert check_font_id(font) == font


@pytest.mark.parametrize(
    "font",
    ["comic_sans", "Inter", "inter;", "../../etc/passwd", "url(x)", "", None],
)
def test_unknown_fonts_are_refused(font: Any) -> None:  # noqa: ANN401
    with pytest.raises(ValueError):
        check_font_id(font)
    with pytest.raises(ValueError):
        BrandKit(name="Main", heading_font=font)
    with pytest.raises(ValueError):
        BrandKit(name="Main", body_font=font)


# ── Stored files ───────────────────────────────────────────────────────────
@pytest.mark.parametrize("mime", ["image/svg+xml", "text/html", "image/gif", "application/octet-stream"])
def test_stored_files_refuse_unservable_types(mime: str) -> None:
    with pytest.raises(ValueError):
        StoredFile(kind="logo", source="upload", mime=mime, size_bytes=1, sha256=GOOD_SHA)


@pytest.mark.parametrize("digest", ["A" * 64, "a" * 63, "g" * 64, ""])
def test_stored_files_need_a_lower_case_sha256(digest: str) -> None:
    with pytest.raises(ValueError):
        StoredFile(kind="logo", source="upload", mime="image/png", size_bytes=1, sha256=digest)


def test_stored_file_bytes_are_not_loaded_with_the_row() -> None:
    assert StoredFile.__mapper__.attrs["data"].deferred is True


# ── Schema shape ───────────────────────────────────────────────────────────
def test_every_creative_table_is_tenant_scoped() -> None:
    from app.db.rls import TENANT_SCOPED_TABLES

    for model in (StoredFile, BrandKit, Product, CreativeJob):
        table = model.__table__
        assert table.name in TENANT_SCOPED_TABLES
        fk = next(iter(table.c.tenant_id.foreign_keys))
        assert fk.column.table.name == "organizations"
        assert fk.ondelete == "CASCADE"


def test_one_default_kit_per_workspace_is_a_partial_unique_index() -> None:
    index = next(i for i in BrandKit.__table__.indexes if i.name == "uq_brand_kits_one_default")
    assert isinstance(index, Index)
    assert index.unique is True
    assert [c.name for c in index.columns] == ["tenant_id"]
    assert str(index.dialect_options["postgresql"]["where"]) == "is_default"


def test_logos_are_cleared_not_cascaded_when_a_file_is_deleted() -> None:
    fks = {
        fk.parent.name: fk
        for c in BrandKit.__table__.constraints
        if isinstance(c, ForeignKeyConstraint)
        for fk in c.elements
    }
    for column in ("logo_file_id", "logo_dark_file_id"):
        assert fks[column].column.table.name == "stored_files"
        assert fks[column].ondelete == "SET NULL"


def test_database_constraints_pin_colours_fonts_and_types() -> None:
    ddl = str(CreateTable(BrandKit.__table__).compile(dialect=postgresql.dialect()))
    assert f"primary_color ~ '{HEX_COLOUR_SQL}'" in ddl
    assert "heading_font IN ('inter'" in ddl
    assert "'bebas_neue')" in ddl
    ddl = str(CreateTable(StoredFile.__table__).compile(dialect=postgresql.dialect()))
    assert "mime IN ('image/png', 'image/jpeg', 'image/webp', 'application/pdf')" in ddl
    assert "svg" not in ddl


def test_creative_jobs_hold_no_plaintext_brief_or_copy() -> None:
    columns = set(CreativeJob.__table__.c.keys())
    assert {"brief_encrypted", "feedback_encrypted", "outputs"} <= columns
    assert not {"brief", "feedback", "copy", "prompt"} & columns


def test_new_constants_exist() -> None:
    assert AssetKind.CREATIVE == "creative"
    assert AssetStatus.PUBLISHING == "publishing"
    assert len(AssetStatus.PUBLISHING) <= 16  # marketing_assets.status width
    assert Ctx.CREATIVE_BRIEF == "creative_job.brief"
    assert Ctx.CREATIVE_FEEDBACK == "creative_job.feedback"


def test_brief_ciphertext_is_bound_to_its_column() -> None:
    from app.core.crypto import unwrap_dek

    org_id = "11111111-1111-4111-8111-111111111111"
    cipher = OrgCipher(org_id=org_id, _dek=unwrap_dek(OrgCipher.provision(org_id), org_id))
    token = cipher.encrypt("Spring sale brief", context=Ctx.CREATIVE_BRIEF)
    assert "Spring sale" not in token
    assert cipher.decrypt(token, context=Ctx.CREATIVE_BRIEF) == "Spring sale brief"
    # A brief moved into the feedback column does not decrypt there.
    assert cipher.decrypt(token, context=Ctx.CREATIVE_FEEDBACK) == ""


# ── Migration ──────────────────────────────────────────────────────────────
def test_migration_chain_and_ids() -> None:
    module = _load_migration("0019_creative_studio")
    previous = _load_migration("0018_connector_verifications")
    assert module.revision == "0019_creative_studio"
    assert module.down_revision == previous.revision == "0018_connector_verifications"
    # alembic_version.version_num is VARCHAR(32).
    assert len(module.revision) <= 32
    assert callable(module.upgrade)
    assert callable(module.downgrade)
    # Frozen copies must match the application's allow-lists at creation.
    assert module.FONT_IDS == FONT_IDS
    assert module.STORED_FILE_MIMES == STORED_FILE_MIMES
    assert module.HEX_COLOUR_SQL == HEX_COLOUR_SQL
    assert not [p.name for p in VERSIONS.glob("0019_*.py") if p.name != "0019_creative_studio.py"]


class _RecordingOp:
    """Stands in for ``alembic.op``: records calls, touches no database."""

    def __init__(self) -> None:
        self.tables: dict[str, list[Any]] = {}
        self.indexes: list[tuple[str, str, list[str], dict[str, Any]]] = []
        self.dropped: list[str] = []
        self.executed: list[str] = []

    def get_bind(self) -> object:
        return object()

    def create_table(self, name: str, *elements: Any) -> None:  # noqa: ANN401
        self.tables[name] = list(elements)

    def create_index(self, name: str, table: str, columns: list[str], **kw: Any) -> None:  # noqa: ANN401
        self.indexes.append((name, table, columns, kw))

    def drop_table(self, name: str) -> None:
        self.dropped.append(name)

    def execute(self, statement: str) -> None:
        self.executed.append(statement)


def _run(direction: str, existing: set[str], monkeypatch: pytest.MonkeyPatch) -> tuple[_RecordingOp, list]:
    import sqlalchemy as sa

    import app.db.rls as rls

    module = _load_migration("0019_creative_studio")
    recorder = _RecordingOp()
    policies: list[tuple[str, ...]] = []

    class _Inspector:
        def get_table_names(self) -> list[str]:
            return sorted(existing)

    monkeypatch.setattr(module, "op", recorder)
    monkeypatch.setattr(sa, "inspect", lambda _bind: _Inspector())
    monkeypatch.setattr(rls, "apply_policies", lambda _conn, tables: policies.append(tables))
    getattr(module, direction)()
    return recorder, policies


#: Columns 0019 created NOT NULL that migration 0022 made nullable.
RELAXED_BY_0022 = frozenset({("creative_jobs", "tokens_in"), ("creative_jobs", "tokens_out")})


def test_migration_creates_what_the_models_declare(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.db.base import Base

    recorder, policies = _run("upgrade", {"organizations"}, monkeypatch)
    assert list(recorder.tables) == list(CREATIVE_TABLES)
    assert policies == [CREATIVE_TABLES]
    for name, elements in recorder.tables.items():
        model = Base.metadata.tables[name]
        columns = {e.name: e for e in elements if hasattr(e, "type") and hasattr(e, "nullable")}
        assert set(columns) == set(model.c.keys()), name
        for col_name, col in columns.items():
            # 0022 later made these nullable; 0019 itself is frozen.
            expected = False if (name, col_name) in RELAXED_BY_0022 else model.c[col_name].nullable
            assert col.nullable == expected, (name, col_name)
        checks = {e.name for e in elements if isinstance(e, CheckConstraint)}
        model_checks = {c.name for c in model.constraints if isinstance(c, CheckConstraint)}
        assert checks == model_checks, name
    default_index = next(i for i in recorder.indexes if i[0] == "uq_brand_kits_one_default")
    assert default_index[2] == ["tenant_id"]
    assert default_index[3]["unique"] is True
    assert str(default_index[3]["postgresql_where"]) == "is_default"


def test_migration_is_idempotent_over_existing_tables(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder, policies = _run("upgrade", {"organizations", *CREATIVE_TABLES}, monkeypatch)
    assert recorder.tables == {}
    assert policies == []


def test_downgrade_drops_in_reverse_dependency_order(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder, _ = _run("downgrade", {"organizations", *CREATIVE_TABLES}, monkeypatch)
    assert recorder.dropped == list(reversed(CREATIVE_TABLES))
    assert all("DROP POLICY IF EXISTS tenant_isolation" in s for s in recorder.executed)


def test_downgrade_skips_tables_that_are_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder, _ = _run("downgrade", {"organizations", "stored_files"}, monkeypatch)
    assert recorder.dropped == ["stored_files"]


# ── Database behaviour ─────────────────────────────────────────────────────
def _skip_if_rls_is_bypassed(db: Any) -> None:  # noqa: ANN401
    from sqlalchemy import text

    bypass = db.execute(
        text("SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname = current_user")
    ).scalar_one()
    if bypass:
        pytest.skip("The test database role bypasses row-level security")


@requires_db
def test_another_workspaces_creative_rows_are_invisible(clean_db, org, second_org) -> None:  # noqa: ANN001
    from sqlalchemy import select

    from app.db.session import bind_tenant

    _skip_if_rls_is_bypassed(clean_db)
    bind_tenant(clean_db, org.id)
    stored = StoredFile(
        tenant_id=org.id, kind="logo", source="upload", mime="image/png",
        size_bytes=3, sha256=GOOD_SHA, data=b"png",
    )
    clean_db.add(stored)
    clean_db.flush()
    clean_db.add_all(
        [
            BrandKit(tenant_id=org.id, name="Main", is_default=True, logo_file_id=stored.id),
            Product(tenant_id=org.id, name="Widget"),
            CreativeJob(tenant_id=org.id, created_by="u", brief_encrypted="x"),
        ]
    )
    clean_db.commit()

    bind_tenant(clean_db, second_org.id)
    for model in (StoredFile, BrandKit, Product, CreativeJob):
        # Deliberately no tenant filter: RLS alone must hide the rows.
        assert clean_db.execute(select(model)).scalars().all() == [], model.__tablename__

    bind_tenant(clean_db, org.id)
    assert len(clean_db.execute(select(BrandKit)).scalars().all()) == 1


@requires_db
def test_a_second_default_kit_is_rejected(clean_db, org) -> None:  # noqa: ANN001
    from sqlalchemy.exc import IntegrityError

    from app.db.session import bind_tenant

    bind_tenant(clean_db, org.id)
    clean_db.add(BrandKit(tenant_id=org.id, name="Main", is_default=True))
    clean_db.add(BrandKit(tenant_id=org.id, name="Other", is_default=False))
    clean_db.flush()
    nested = clean_db.begin_nested()
    clean_db.add(BrandKit(tenant_id=org.id, name="Second", is_default=True))
    with pytest.raises(IntegrityError):
        clean_db.flush()
    nested.rollback()


@requires_db
def test_the_database_refuses_a_bad_colour_written_around_the_model(clean_db, org) -> None:  # noqa: ANN001
    from sqlalchemy import text
    from sqlalchemy.exc import IntegrityError

    from app.db.session import bind_tenant

    bind_tenant(clean_db, org.id)
    nested = clean_db.begin_nested()
    with pytest.raises(IntegrityError):
        clean_db.execute(
            text(
                "INSERT INTO brand_kits (id, tenant_id, name, primary_color) "
                "VALUES (gen_random_uuid(), :t, 'Raw', 'red;}')"
            ),
            {"t": org.id},
        )
    nested.rollback()
