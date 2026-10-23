"""Scoped approvals: brand/region/channel filters for list and act."""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session

from app.db.base import Base, utcnow
from app.models.approval import ApprovalItem, ApprovalStatus
from app.models.approval_scope import UserApprovalScope
from app.models.workspace import AuditLogEntry, Organization, User
from app.services import approval_scopes, approvals as approval_service
from tests.test_seo_pipeline_fixes import TENANT


@compiles(JSONB, "sqlite")
def _jsonb_on_sqlite(type_, compiler, **kw):  # noqa: ANN001, ANN003, ANN202
    return "JSON"


@compiles(PG_UUID, "sqlite")
def _uuid_on_sqlite(type_, compiler, **kw):  # noqa: ANN001, ANN003, ANN202
    return "CHAR(36)"


@pytest.fixture
def scope_db():  # noqa: ANN201
    engine = create_engine("sqlite://")
    tables = [
        Organization.__table__,
        User.__table__,
        AuditLogEntry.__table__,
        ApprovalItem.__table__,
        UserApprovalScope.__table__,
    ]
    Base.metadata.create_all(engine, tables=tables)
    with Session(engine) as session:
        org = Organization(
            id=TENANT,
            name="Acme",
            slug="acme-scope",
            wrapped_dek="test",
            dek_version=1,
            plan_name="Enterprise",
            plan_tier="enterprise",
            seats_total=10,
            is_active=True,
            data_region="eu-central",
            settings={},
            ip_allowlist_enabled=False,
            mfa_required=False,
        )
        admin = User(
            tenant_id=org.id,
            name="Admin",
            email="admin@example.com",
            email_index="idx-a",
            role="admin",
            is_active=True,
            is_owner=True,
            email_verified=True,
        )
        approver = User(
            tenant_id=org.id,
            name="Approver",
            email="appr@example.com",
            email_index="idx-b",
            role="approver",
            is_active=True,
            is_owner=False,
            email_verified=True,
        )
        session.add_all([org, admin, approver])
        session.flush()
        yield session, org, admin, approver
    engine.dispose()


def _item(org_id: str, *, brand: str = "", region: str = "", channel: str = "") -> ApprovalItem:
    # utcnow() is aware; SQLite round-trips as naive — stamp naive UTC so
    # list_pending's age maths do not mix kinds.
    submitted = utcnow().replace(tzinfo=None)
    return ApprovalItem(
        tenant_id=org_id,
        type="Content Rewrite",
        title="Rewrite home",
        agent_slug="on_page_seo_sync",
        agent_name="On-page",
        target_kind="seo_page",
        status=ApprovalStatus.PENDING.value,
        submitted_at=submitted,
        scope_brand=brand,
        scope_region=region,
        scope_channel=channel,
    )


def test_rls_registered() -> None:
    from app.db.rls import TENANT_SCOPED_TABLES

    assert "user_approval_scopes" in TENANT_SCOPED_TABLES


def test_unscoped_item_visible_to_all(scope_db):  # noqa: ANN001
    db, org, _admin, approver = scope_db
    item = _item(org.id)
    db.add(item)
    db.flush()
    approval_scopes.set_user_scope(
        db, org, approver, brands=["Other"], actor_email="a@example.com"
    )
    assert approval_scopes.item_in_scope(item, approval_scopes.load_scope(db, approver))


def test_scoped_item_filtered(scope_db):  # noqa: ANN001
    db, org, _admin, approver = scope_db
    eu = _item(org.id, brand="Acme", region="EU")
    us = _item(org.id, brand="Acme", region="US")
    db.add_all([eu, us])
    db.flush()
    approval_scopes.set_user_scope(
        db,
        org,
        approver,
        brands=["Acme"],
        regions=["EU"],
        actor_email="a@example.com",
    )
    visible = approval_scopes.filter_items(db, approver, [eu, us])
    assert [i.id for i in visible] == [eu.id]


def test_assert_can_act_blocks_out_of_scope(scope_db):  # noqa: ANN001
    db, org, _admin, approver = scope_db
    item = _item(org.id, channel="meta")
    db.add(item)
    db.flush()
    approval_scopes.set_user_scope(
        db, org, approver, channels=["google"], actor_email="a@example.com"
    )
    with pytest.raises(Exception, match="scope"):
        approval_scopes.assert_can_act(db, approver, item)


def test_list_pending_respects_viewer(scope_db):  # noqa: ANN001
    db, org, _admin, approver = scope_db
    db.add(_item(org.id, brand="Acme"))
    db.add(_item(org.id, brand="Beta"))
    db.flush()
    approval_scopes.set_user_scope(
        db, org, approver, brands=["Acme"], actor_email="a@example.com"
    )
    items = approval_service.list_pending(
        db, tenant_id=org.id, limit=50, viewer=approver
    )
    assert len(items) == 1
    assert items[0].scope_brand == "Acme"


def test_users_in_scope_for_item(scope_db):  # noqa: ANN001
    db, org, admin, approver = scope_db
    item = _item(org.id, brand="Acme")
    db.add(item)
    db.flush()
    approval_scopes.set_user_scope(
        db, org, approver, brands=["Acme"], actor_email="a@example.com"
    )
    # Admin has no scope row → unrestricted; approver matches Acme.
    users = approval_scopes.users_in_scope_for_item(db, tenant_id=org.id, item=item)
    ids = {u.id for u in users}
    assert admin.id in ids
    assert approver.id in ids
