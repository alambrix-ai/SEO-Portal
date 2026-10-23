"""Prove row-level security blocks cross-tenant reads as the app role.

Fixture setup uses the owner role (``clean_db``). Assertions here open a
fresh ``SessionLocal`` connection — the same non-owner role the API uses —
and deliberately omit an application-level ``tenant_id`` filter so only RLS
can hide the other workspace's rows.
"""
from __future__ import annotations

import os

import pytest
from sqlalchemy import select, text

from tests.conftest import requires_db

pytestmark = [
    requires_db,
    pytest.mark.skipif(
        not (
            os.environ.get("TEST_MIGRATION_DATABASE_URL")
            or os.environ.get("MIGRATION_DATABASE_URL")
        ),
        reason=(
            "Cross-tenant RLS proof needs TEST_MIGRATION_DATABASE_URL "
            "(owner) distinct from TEST_DATABASE_URL (automarket_app)"
        ),
    ),
]


@requires_db
def test_runtime_session_is_the_non_owner_app_role(db) -> None:  # noqa: ANN001
    row = db.execute(
        text(
            "SELECT current_user, rolsuper, rolbypassrls "
            "FROM pg_roles WHERE rolname = current_user"
        )
    ).one()
    assert row[0] == "automarket_app"
    assert row[1] is False
    assert row[2] is False


@requires_db
def test_cross_tenant_select_is_blocked_by_rls_without_tenant_filter(
    clean_db, org, second_org
) -> None:  # noqa: ANN001
    """Pinned to org A, a bare SELECT on users must not return org B's admin.

    No ``WHERE tenant_id = …`` in the query: if this passes, RLS (not the
    application filter) is doing the isolation — which is what FORCE + a
    non-owner app role are for.
    """
    from app.db.session import SessionLocal, bind_tenant
    from app.models.workspace import User

    # Seed visibility: both tenants have an admin (org / second_org fixtures).
    # Owner is subject to FORCE RLS too — pin per tenant to confirm rows exist.
    bind_tenant(clean_db, org.id)
    admins_a = clean_db.execute(
        select(User).where(User.tenant_id == org.id)
    ).scalars().all()
    bind_tenant(clean_db, second_org.id)
    admins_b = clean_db.execute(
        select(User).where(User.tenant_id == second_org.id)
    ).scalars().all()
    assert admins_a and admins_b

    app = SessionLocal()
    try:
        bind_tenant(app, org.id)
        # Deliberately no tenant_id predicate — RLS alone must hide B.
        visible = app.execute(select(User)).scalars().all()
        visible_tenants = {u.tenant_id for u in visible}
        assert org.id in visible_tenants
        assert second_org.id not in visible_tenants

        # Wrong pin: org B's rows appear, org A's do not.
        bind_tenant(app, second_org.id)
        visible_b = app.execute(select(User)).scalars().all()
        assert {u.tenant_id for u in visible_b} == {second_org.id}
    finally:
        app.close()


@requires_db
def test_unpinned_app_session_sees_no_tenant_rows(clean_db, org) -> None:  # noqa: ANN001
    from app.db.session import SessionLocal, bind_tenant
    from app.models.workspace import User

    bind_tenant(clean_db, org.id)
    assert clean_db.execute(select(User)).scalars().first() is not None

    app = SessionLocal()
    try:
        # No bind_tenant: FORCE RLS with empty app.current_tenant → no rows.
        assert app.execute(select(User)).scalars().all() == []
    finally:
        app.close()
