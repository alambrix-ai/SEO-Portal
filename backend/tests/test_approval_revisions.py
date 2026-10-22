"""Asking for changes and editing text on approval cards (step 3.4).

Most tests run the service and route functions against a small in-memory
session double, so they need no database. The database-backed tests check
the real row lock and tenant isolation.
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from app.api.deps import CurrentUser
from app.api.v1 import approvals as approvals_api
from app.api.v1 import content as content_api
from app.core.exceptions import ConflictError, ForbiddenError, InvalidInputError, NotFoundError
from app.db.base import new_id, utcnow
from app.models.approval import ApprovalItem, ApprovalStatus
from app.models.content import AssetKind, AssetStatus, MarketingAsset
from app.models.creative import BrandKit, Product
from app.schemas.workspace import EditTextRequest, RequestChangesRequest
from app.services import approvals, assets
from app.services.encryption import Ctx, OrgCipher
from tests.conftest import requires_db
from tests.support.fake_llm import FakeLLMProvider

FEEDBACK = "Make the headline shorter and friendlier"


# ── Doubles ────────────────────────────────────────────────────────────────
class _Result:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def scalars(self) -> _Result:
        return self

    def first(self) -> Any:  # noqa: ANN401
        return self._rows[0] if self._rows else None

    def __iter__(self):  # noqa: ANN204
        return iter(self._rows)


class FakeDb:
    """Answers ``select(Model).where(id ..., tenant_id ...)`` from memory.

    A row matches when both its id and its tenant id are among the
    statement's bound values, which is how every query under test filters.
    """

    def __init__(self, *rows: Any) -> None:
        self.rows = list(rows)
        self.flushes = 0

    def execute(self, stmt: Any) -> _Result:  # noqa: ANN401
        entity = stmt.column_descriptions[0]["entity"]
        values: set[str] = set()
        for value in stmt.compile().params.values():
            values.update(value if isinstance(value, (list, tuple)) else [value])
        return _Result(
            [r for r in self.rows if isinstance(r, entity) and r.id in values and r.tenant_id in values]
        )

    def get(self, model: type, row_id: str) -> Any:  # noqa: ANN401
        return next((r for r in self.rows if isinstance(r, model) and r.id == row_id), None)

    def flush(self) -> None:
        self.flushes += 1

    def add(self, row: Any) -> None:  # noqa: ANN401
        if getattr(row, "id", None) is None:
            row.id = new_id()
        self.rows.append(row)


def _org() -> SimpleNamespace:
    org_id = new_id()
    return SimpleNamespace(id=org_id, wrapped_dek=OrgCipher.provision(org_id), settings={})


def _actor(org: SimpleNamespace, role: str = "admin") -> SimpleNamespace:
    return SimpleNamespace(id=new_id(), tenant_id=org.id, name="Asha", role=role)


def _item(org: SimpleNamespace, *, kind: str = "test_kind", payload: dict | None = None,
          status: str = ApprovalStatus.PENDING.value, target_id: str | None = None) -> ApprovalItem:
    cipher = OrgCipher.for_org(org)
    return ApprovalItem(
        id=new_id(),
        tenant_id=org.id,
        type="Ad Creative",
        title="Spring banner",
        agent_slug="creative_studio",
        agent_name="Creative Studio",
        target_kind=kind,
        target_id=target_id,
        payload_encrypted=cipher.encrypt_json(payload or {}, context=Ctx.APPROVAL_PAYLOAD),
        meta="",
        status=status,
        submitted_at=utcnow(),
    )


@pytest.fixture
def audit_log(monkeypatch):  # noqa: ANN001, ANN201
    entries: list[dict] = []
    monkeypatch.setattr(approvals.audit, "record_user_action", lambda db, **kw: entries.append(kw))
    return entries


@pytest.fixture
def test_reviser():  # noqa: ANN201
    calls: list[approvals.RevisionRequest] = []

    def reviser(db, org, item, payload, request):  # noqa: ANN001, ANN202
        calls.append(request)
        return approvals.RevisionOutcome()

    approvals.REVISERS["test_kind"] = reviser
    yield calls
    approvals.REVISERS.pop("test_kind", None)


# ── Ask for changes ────────────────────────────────────────────────────────
def test_request_changes_supersedes_and_hands_over_encrypted_feedback(audit_log, test_reviser):
    org = _org()
    item = _item(org)
    db = FakeDb(item)

    result = approvals.request_changes(
        db, org=org, actor=_actor(org), item_id=item.id, feedback=f"  {FEEDBACK}  "
    )

    assert item.status == ApprovalStatus.SUPERSEDED.value
    assert item.decision_note == "Changes requested"
    assert (result.revision, result.max_revisions) == (1, approvals.max_revisions())
    [request] = test_reviser
    assert FEEDBACK not in request.feedback_encrypted
    assert approvals.read_feedback(org, request.feedback_encrypted) == FEEDBACK
    # Feedback is never in the audit trail, only its length.
    [entry] = audit_log
    assert FEEDBACK not in repr(entry)
    assert entry["context"]["feedback_chars"] == len(FEEDBACK)


def test_feedback_is_encrypted_under_the_creative_feedback_context(test_reviser, audit_log):
    org = _org()
    item = _item(org)
    approvals.request_changes(FakeDb(item), org=org, actor=_actor(org), item_id=item.id, feedback=FEEDBACK)
    token = test_reviser[0].feedback_encrypted
    cipher = OrgCipher.for_org(org)
    assert cipher.decrypt(token, context=Ctx.CREATIVE_FEEDBACK) == FEEDBACK
    # The context is bound into the ciphertext: read under another, it yields nothing.
    assert cipher.decrypt(token, context=Ctx.APPROVAL_PAYLOAD) != FEEDBACK


@pytest.mark.parametrize("status", ["approved", "rejected", "expired", "practised", "superseded"])
def test_a_decided_item_gets_409(status, test_reviser, audit_log):
    org = _org()
    item = _item(org, status=status)
    db = FakeDb(item)
    with pytest.raises(ConflictError, match="already been decided"):
        approvals.request_changes(db, org=org, actor=_actor(org), item_id=item.id, feedback=FEEDBACK)
    with pytest.raises(ConflictError, match="already been decided"):
        approvals.edit_text(db, org=org, actor=_actor(org), item_id=item.id, fields={"headline": "x"})
    assert test_reviser == [] and audit_log == []


def test_an_unknown_kind_gets_409(audit_log):
    org = _org()
    item = _item(org, kind="budget_plan")
    db = FakeDb(item)
    with pytest.raises(ConflictError):
        approvals.request_changes(db, org=org, actor=_actor(org), item_id=item.id, feedback=FEEDBACK)
    with pytest.raises(ConflictError):
        approvals.edit_text(db, org=org, actor=_actor(org), item_id=item.id, fields={"headline": "x"})
    assert item.status == ApprovalStatus.PENDING.value


def test_another_tenants_item_is_not_found(test_reviser, audit_log):
    org, other = _org(), _org()
    item = _item(other)
    with pytest.raises(NotFoundError):
        approvals.request_changes(FakeDb(item), org=org, actor=_actor(org), item_id=item.id, feedback=FEEDBACK)
    assert item.status == ApprovalStatus.PENDING.value and test_reviser == []


def test_the_revision_limit_gives_a_friendly_409(test_reviser, audit_log, monkeypatch):
    monkeypatch.setattr(approvals.settings, "creative_max_revisions", 2)
    org = _org()
    item = _item(org, payload={approvals.REVISION_KEY: 2})
    with pytest.raises(ConflictError, match="revised 2 times") as caught:
        approvals.request_changes(FakeDb(item), org=org, actor=_actor(org), item_id=item.id, feedback=FEEDBACK)
    assert caught.value.details == {"revision": 2, "max_revisions": 2}
    assert test_reviser == [] and item.status == ApprovalStatus.PENDING.value


def test_queue_revision_carries_the_count_forward():
    org = _org()
    previous = _item(org, payload={approvals.REVISION_KEY: 1})
    db = FakeDb(previous)
    revised = approvals.queue_revision(db, org=org, previous=previous, title="Spring banner v3", payload={"asset_id": "a"})
    stored = approvals.read_payload(org, revised)
    assert stored[approvals.REVISION_KEY] == 2 and stored[approvals.REVISES_KEY] == previous.id
    with pytest.raises(NotFoundError):
        approvals.queue_revision(db, org=_org(), previous=previous, title="x", payload={})


@pytest.mark.parametrize("feedback", ["", "   ", "x" * (approvals.MAX_FEEDBACK_CHARS + 1)])
def test_empty_or_oversized_feedback_is_refused(feedback, test_reviser, audit_log):
    org = _org()
    item = _item(org)
    with pytest.raises(InvalidInputError):
        approvals.request_changes(FakeDb(item), org=org, actor=_actor(org), item_id=item.id, feedback=feedback)
    assert test_reviser == []


def test_request_bodies_are_bounded():
    with pytest.raises(ValidationError):
        RequestChangesRequest(feedback="x" * 2001)
    with pytest.raises(ValidationError):
        RequestChangesRequest(feedback=FEEDBACK, extra="nope")
    with pytest.raises(ValidationError):
        EditTextRequest(fields={})
    with pytest.raises(ValidationError):
        EditTextRequest(fields={"Headline; DROP": "x"})
    with pytest.raises(ValidationError):
        EditTextRequest(fields={f"f{i}": "x" for i in range(9)})
    with pytest.raises(ValidationError):
        EditTextRequest(fields={"headline": "x" * 2001})
    assert EditTextRequest(fields={"headline": "Hello"}).fields == {"headline": "Hello"}


def test_a_failing_reviser_leaves_the_item_pending_and_hides_its_error(audit_log):
    org = _org()
    item = _item(org)

    def boom(*_):  # noqa: ANN002, ANN202
        raise RuntimeError("provider said sk-secret-123 at /internal/path")

    approvals.REVISERS["test_kind"] = boom
    try:
        with pytest.raises(ConflictError) as caught:
            approvals.request_changes(FakeDb(item), org=org, actor=_actor(org), item_id=item.id, feedback=FEEDBACK)
    finally:
        approvals.REVISERS.pop("test_kind", None)
    assert "sk-secret" not in caught.value.message
    assert item.status == ApprovalStatus.PENDING.value and audit_log == []


# ── Edit text on a creative ────────────────────────────────────────────────
COPY = {"headline": "Fresh coffee, every morning", "body": "Roasted in Pune.", "cta": "Order now", "claim_ids": [0]}


def _creative(org: SimpleNamespace, *, product_id: str, kit_id: str, status: str = AssetStatus.DRAFT) -> MarketingAsset:
    asset = MarketingAsset(
        id=new_id(),
        tenant_id=org.id,
        agent_slug="creative_studio",
        run_id="",
        kind=AssetKind.CREATIVE,
        channel="display",
        title="Spring banner",
        status=status,
        image_ids=[],
        detail={"product_id": product_id, "brand_kit_id": kit_id, "preview_file_id": "file-1"},
        error="",
    )
    assets.set_content(asset, OrgCipher.for_org(org), {"format_id": "banner_300x250", "copy": dict(COPY)})
    return asset


def _product(tenant_id: str) -> Product:
    return Product(id=new_id(), tenant_id=tenant_id, name="Bean Box", description="Coffee beans",
                   price_text="Rs 499", approved_claims=["Free delivery in Pune"], disclaimer="")


def _kit(tenant_id: str) -> BrandKit:
    return BrandKit(id=new_id(), tenant_id=tenant_id, name="Main", banned_phrases=["cheap"])


class _NoLLM(FakeLLMProvider):
    def complete(self, *args, **kwargs):  # noqa: ANN002, ANN003, ANN201
        raise AssertionError("editing text must not call a model")


@pytest.fixture
def creative_setup(audit_log, monkeypatch):  # noqa: ANN001, ANN201
    """A pending creative with a recording re-renderer and no reviser.

    The registries are global and Creative Studio (5.1) registers real hooks
    on import, so they are swapped through ``monkeypatch`` and restored after.
    """
    from app import llm

    org = _org()
    product, kit = _product(org.id), _kit(org.id)
    asset = _creative(org, product_id=product.id, kit_id=kit.id)
    item = _item(org, kind=assets.TARGET_KIND, payload={"asset_id": asset.id}, target_id=asset.id)
    rendered: list[dict] = []
    monkeypatch.setitem(
        assets.RERENDERERS, AssetKind.CREATIVE, lambda db, org_, a, content: rendered.append(content)
    )
    monkeypatch.delitem(assets.ASSET_REVISERS, AssetKind.CREATIVE, raising=False)
    llm.set_provider(_NoLLM())
    return SimpleNamespace(org=org, asset=asset, item=item, db=FakeDb(item, asset, product, kit),
                           rendered=rendered, audit=audit_log)


def _copy_of(setup: SimpleNamespace) -> dict:
    return assets.content_of(setup.asset, OrgCipher.for_org(setup.org))["copy"]


def test_edit_text_revalidates_saves_and_rerenders(creative_setup):
    s = creative_setup
    result = approvals.edit_text(s.db, org=s.org, actor=_actor(s.org), item_id=s.item.id,
                                 fields={"headline": "Fresh coffee, delivered"})
    assert _copy_of(s)["headline"] == "Fresh coffee, delivered"
    assert _copy_of(s)["claim_ids"] == [0]
    assert len(s.rendered) == 1 and result.approval_id == s.item.id
    assert s.item.status == ApprovalStatus.PENDING.value
    assert s.audit[0]["context"]["fields"] == ["headline"]
    assert "Fresh coffee, delivered" not in repr(s.audit)


@pytest.mark.parametrize(
    ("fields", "match"),
    [
        ({"headline": "Our cheap coffee"}, "."),  # banned phrase (QA)
        ({"headline": "Guaranteed best coffee"}, "."),  # unprovable promise (QA)
        ({"headline": "Now 70% off coffee"}, "."),  # number the product never states
        ({"headline": "x" * 41}, "too long"),  # format limit (parse_copy)
        ({"headline": "Line one\nline two"}, "."),  # one-line field
        ({"disclaimer": "None"}, "Only the headline"),  # not an editable field
        ({"claim_ids": "[0, 1]"}, "Only the headline"),
    ],
)
def test_an_invalid_edit_gets_422_and_changes_nothing(creative_setup, fields, match):
    s = creative_setup
    with pytest.raises(InvalidInputError, match=match):
        approvals.edit_text(s.db, org=s.org, actor=_actor(s.org), item_id=s.item.id, fields=fields)
    assert _copy_of(s) == COPY and s.rendered == [] and s.audit == []


def test_another_tenants_product_is_never_used_for_grounding(audit_log, monkeypatch):
    from app import llm

    org, other = _org(), _org()
    foreign = _product(other.id)
    asset = _creative(org, product_id=foreign.id, kit_id="")
    item = _item(org, kind=assets.TARGET_KIND, payload={"asset_id": asset.id}, target_id=asset.id)
    monkeypatch.setitem(assets.RERENDERERS, AssetKind.CREATIVE, lambda *a: None)
    llm.set_provider(_NoLLM())
    # The copy picks claim 0; without this tenant's product there is no claim 0.
    with pytest.raises(InvalidInputError):
        approvals.edit_text(FakeDb(item, asset, foreign), org=org, actor=_actor(org),
                            item_id=item.id, fields={"headline": "Fresh coffee"})


def test_edit_without_a_rerenderer_gets_409(creative_setup, monkeypatch):
    s = creative_setup
    monkeypatch.delitem(assets.RERENDERERS, AssetKind.CREATIVE, raising=False)
    with pytest.raises(ConflictError):
        approvals.edit_text(s.db, org=s.org, actor=_actor(s.org), item_id=s.item.id, fields={"headline": "Hi there"})
    assert _copy_of(s) == COPY


def test_a_published_creative_cannot_be_edited(creative_setup):
    s = creative_setup
    s.asset.status = AssetStatus.PUBLISHING
    with pytest.raises(ConflictError):
        approvals.edit_text(s.db, org=s.org, actor=_actor(s.org), item_id=s.item.id, fields={"headline": "Hi there"})


def test_a_creative_without_a_reviser_cannot_ask_for_changes(creative_setup):
    s = creative_setup
    with pytest.raises(ConflictError):
        approvals.request_changes(s.db, org=s.org, actor=_actor(s.org), item_id=s.item.id, feedback=FEEDBACK)
    assert s.item.status == ApprovalStatus.PENDING.value


def test_support_flags_and_preview(creative_setup, monkeypatch):
    s = creative_setup
    entries = [(s.item, approvals.read_payload(s.org, s.item))]
    support = approvals.revision_support(s.db, org=s.org, entries=entries)[s.item.id]
    assert support.edit and not support.revise and support.preview_file_id == "file-1"

    monkeypatch.setitem(assets.ASSET_REVISERS, AssetKind.CREATIVE, lambda *a: approvals.RevisionOutcome())
    assert approvals.revision_support(s.db, org=s.org, entries=entries)[s.item.id].revise
    monkeypatch.setattr(approvals.settings, "creative_max_revisions", 0)
    assert not approvals.revision_support(s.db, org=s.org, entries=entries)[s.item.id].revise


# ── Routes: roles and the raw content PUT ──────────────────────────────────
def _current(org: SimpleNamespace, role: str) -> CurrentUser:
    return CurrentUser(_actor(org, role), org, {})


def test_an_approver_editing_text_gets_403(creative_setup):
    s = creative_setup
    with pytest.raises(ForbiddenError):
        approvals_api.edit_text(s.item.id, EditTextRequest(fields={"headline": "Hi there"}),
                                _current(s.org, "approver"), s.db, "127.0.0.1")
    assert _copy_of(s) == COPY


def test_an_approver_sees_ask_for_changes_but_not_edit(creative_setup, monkeypatch):
    s = creative_setup
    monkeypatch.setitem(assets.ASSET_REVISERS, AssetKind.CREATIVE, lambda *a: approvals.RevisionOutcome())
    [card] = approvals_api._cards(_current(s.org, "approver"), s.db, [s.item])
    assert card.can_request_changes and not card.can_edit_text
    assert card.preview_file_id == "file-1"
    [card] = approvals_api._cards(_current(s.org, "admin"), s.db, [s.item])
    assert card.can_request_changes and card.can_edit_text
    [card] = approvals_api._cards(_current(s.org, "client"), s.db, [s.item])
    assert not card.can_request_changes and not card.can_edit_text


def test_a_viewer_cannot_request_changes(test_reviser, audit_log):
    org = _org()
    item = _item(org)
    with pytest.raises(ForbiddenError):
        approvals_api.request_changes(item.id, RequestChangesRequest(feedback=FEEDBACK),
                                      _current(org, "seo"), FakeDb(item), "127.0.0.1")
    assert test_reviser == []


def test_a_raw_content_put_on_a_creative_gets_409(creative_setup):
    s = creative_setup
    request = content_api.AssetUpdateRequest(content={"copy": {"headline": "Anything at all"}})
    with pytest.raises(ConflictError, match="Edit text"):
        content_api.update_asset(s.asset.id, request, _current(s.org, "admin"), s.db)
    assert _copy_of(s) == COPY


def test_publishing_has_a_status_label():
    assert content_api.status_label("social_post", AssetStatus.PUBLISHING) == "Publishing now"


# ── CSV exports (formula injection) ────────────────────────────────────────
@pytest.mark.parametrize("cell", ['=HYPERLINK("https://evil.example","x")', "@SUM(A1)", "-2+3", "+cmd"])
def test_spreadsheet_exports_neutralise_formulas(cell):
    text = assets.csv_bytes(["Header"], [[cell]]).decode("utf-8-sig")
    assert f"'{cell}" in text.replace('""', '"')


HYPERLINK = '=HYPERLINK("https://evil.example","Open")'


@pytest.mark.parametrize("cell", [HYPERLINK, "@SUM(A1)", "+=cmd", "-=1+1", " =1+1"])
def test_ads_import_mode_refuses_formula_cells(cell):
    with pytest.raises(InvalidInputError):
        assets.csv_bytes(["Keyword"], [[cell]], mode="ads_import")
    with pytest.raises(InvalidInputError):
        assets.csv_bytes(["Keyword"], [[cell]], utf16=True)


def test_spreadsheet_mode_neutralises_the_same_cell():
    text = assets.csv_bytes(["Keyword"], [[HYPERLINK]], mode="spreadsheet").decode("utf-8-sig")
    assert "'=HYPERLINK" in text
    # The default, with no mode and no export in progress, is the safe one.
    assert "'=HYPERLINK" in assets.csv_bytes(["Keyword"], [[HYPERLINK]]).decode("utf-8-sig")


def test_ads_import_mode_keeps_signs_and_placeholder_ids():
    text = assets.csv_bytes(["Id", "Keyword"], [["-1", "-free"], ["-10", "+coffee"]], mode="ads_import")
    rows = text.decode("utf-8-sig").splitlines()
    assert rows[1:] == ["-1,-free", "-10,+coffee"]
    editor = assets.csv_bytes(["Keyword"], [["-free"], ["+coffee"]], utf16=True).decode("utf-16-le")
    assert "-free" in editor and "+coffee" in editor


def test_export_picks_the_mode_by_format_not_content():
    exported: dict[str, bytes] = {}
    original = assets.EXPORTERS.get(("search_campaign", "microsoft_bulk"))
    labels = dict(assets.EXPORT_LABELS.get("search_campaign", {}))

    @assets.register_exporter("search_campaign", "microsoft_bulk", "Microsoft bulk")
    def fake_bulk(asset, content):  # noqa: ANN001, ANN202
        exported["bulk"] = assets.csv_bytes(["Id"], [["-1"]])
        return assets.ExportFile("x.csv", "text/csv", exported["bulk"])

    @assets.register_exporter("test_kind", "csv", "Spreadsheet")
    def fake_plain(asset, content):  # noqa: ANN001, ANN202
        exported["plain"] = assets.csv_bytes(["Id"], [["-1"]])
        return assets.ExportFile("y.csv", "text/csv", exported["plain"])

    try:
        cipher = SimpleNamespace(decrypt=lambda token, context: "{}")
        assets.export(SimpleNamespace(kind="search_campaign", title="c", content_encrypted=""), cipher, "microsoft_bulk")
        assets.export(SimpleNamespace(kind="test_kind", title="c", content_encrypted=""), cipher, "csv")
    finally:
        if original is not None:
            assets.EXPORTERS[("search_campaign", "microsoft_bulk")] = original
        else:
            assets.EXPORTERS.pop(("search_campaign", "microsoft_bulk"), None)
        if labels:
            assets.EXPORT_LABELS["search_campaign"] = labels
        else:
            assets.EXPORT_LABELS.pop("search_campaign", None)
        assets.EXPORTERS.pop(("test_kind", "csv"), None)
        assets.EXPORT_LABELS.pop("test_kind", None)
    assert exported["bulk"].decode("utf-8-sig").splitlines()[1] == "-1"
    assert exported["plain"].decode("utf-8-sig").splitlines()[1] == "'-1"
    # And the mode does not leak past the export.
    assert assets.csv_bytes(["Id"], [["-1"]]).decode("utf-8-sig").splitlines()[1] == "'-1"


# ── Database: real row lock and tenant isolation ───────────────────────────
@requires_db
def test_request_changes_against_the_database(clean_db, org, second_org, test_reviser):
    from sqlalchemy import select

    from app.db.session import bind_tenant
    from app.models.workspace import User

    bind_tenant(clean_db, org.id)
    actor = clean_db.execute(select(User).where(User.tenant_id == org.id)).scalars().first()
    item = approvals.queue_manual(
        clean_db, org=org, type_="Ad Creative", title="Banner", agent_slug="creative_studio",
        agent_name="Creative Studio", target_kind="test_kind", target_id=None, payload={},
    )
    clean_db.commit()

    result = approvals.request_changes(clean_db, org=org, actor=actor, item_id=item.id, feedback=FEEDBACK)
    clean_db.commit()
    assert result.revision == 1
    clean_db.refresh(item)
    assert item.status == ApprovalStatus.SUPERSEDED.value

    with pytest.raises(ConflictError):
        approvals.request_changes(clean_db, org=org, actor=actor, item_id=item.id, feedback=FEEDBACK)
    clean_db.rollback()

    bind_tenant(clean_db, second_org.id)
    with pytest.raises(NotFoundError):
        approvals.request_changes(clean_db, org=second_org, actor=actor, item_id=item.id, feedback=FEEDBACK)
    clean_db.rollback()
