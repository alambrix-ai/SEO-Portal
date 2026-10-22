"""Portal Admin people directory: unique accounts, nested workspaces, operator-only."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from tests.conftest import requires_db


def _dependency_calls(dependant: Any) -> set[Any]:  # noqa: ANN401
    calls = set()
    for dep in dependant.dependencies:
        calls.add(dep.call)
        calls |= _dependency_calls(dep)
    return calls


def test_the_accounts_route_is_portal_admin_only() -> None:
    from app.api.deps import require_portal_admin
    from app.api.v1 import portal

    routes = [route for route in portal.router.routes if getattr(route, "path", "") == "/portal/accounts"]
    assert len(routes) == 1
    assert "GET" in routes[0].methods
    assert require_portal_admin in _dependency_calls(routes[0].dependant)


def test_a_workspace_admin_cannot_list_accounts(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.api.deps import CurrentUser, require_portal_admin
    from app.core.config import settings
    from app.core.exceptions import ForbiddenError

    monkeypatch.setattr(settings, "portal_admin_emails", ["ops@platform.example"])

    class Caller:
        is_portal_admin = CurrentUser.is_portal_admin
        require_portal_admin = CurrentUser.require_portal_admin

        def __init__(self, email: str) -> None:
            self.user = SimpleNamespace(email=email)

    with pytest.raises(ForbiddenError):
        require_portal_admin(Caller("owner@customer.example"))


@requires_db
def test_the_same_email_is_one_account_with_two_workspaces(clean_db, org, second_org):  # noqa: ANN001
    from app.api.v1.portal import collect_accounts
    from app.core.crypto import email_index
    from app.core.rbac import Role
    from app.db.session import bind_tenant
    from app.models.workspace import User

    email = "dana@agency.example"
    index = email_index(email)
    later = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)
    earlier = datetime(2026, 10, 1, 8, 0, tzinfo=UTC)

    bind_tenant(clean_db, org.id)
    clean_db.add(
        User(
            tenant_id=org.id,
            name="Dana Whitfield",
            email=email,
            email_index=index,
            role=Role.ADMIN.value,
            is_owner=True,
            last_login_at=later,
            sso_provider="google",
        )
    )
    bind_tenant(clean_db, second_org.id)
    clean_db.add(
        User(
            tenant_id=second_org.id,
            name="Dana W",
            email=email,
            email_index=index,
            role=Role.CLIENT.value,
            last_login_at=earlier,
            locked_until=datetime.now(UTC) + timedelta(hours=1),
        )
    )
    clean_db.commit()

    bind_tenant(clean_db, org.id)
    accounts = collect_accounts(clean_db, org.id)
    dana = next(account for account in accounts if account.email == email)

    assert dana.workspace_count == 2
    assert dana.name == "Dana W" or dana.name == "Dana Whitfield"
    assert {seat.organization_id for seat in dana.seats} == {org.id, second_org.id}
    assert dana.last_login_at is not None
    assert dana.last_login_at.startswith("2026-10-21")
    assert dana.sso_provider == "google"
    assert dana.is_locked is True
    dumped = dana.model_dump()
    assert "email_index" not in dumped
    assert all("email_index" not in seat for seat in dumped["seats"])


@requires_db
def test_overview_counts_unique_people_not_only_seats(clean_db, org, second_org, monkeypatch):  # noqa: ANN001
    from fastapi.testclient import TestClient

    from app.api.deps import CurrentUser, get_current_user
    from app.core.config import settings
    from app.core.crypto import email_index
    from app.core.rbac import Role
    from app.db.session import bind_tenant, get_db
    from app.main import create_app
    from app.models.workspace import User

    email = "shared@agency.example"
    index = email_index(email)
    bind_tenant(clean_db, org.id)
    clean_db.add(
        User(
            tenant_id=org.id,
            name="Shared",
            email=email,
            email_index=index,
            role=Role.MANAGER.value,
        )
    )
    bind_tenant(clean_db, second_org.id)
    clean_db.add(
        User(
            tenant_id=second_org.id,
            name="Shared",
            email=email,
            email_index=index,
            role=Role.SEO.value,
        )
    )
    clean_db.commit()

    monkeypatch.setattr(settings, "portal_admin_emails", ["ops@platform.example"])

    async def _noop_lifespan(_app):  # noqa: ANN001, ANN202
        yield

    app = create_app()
    app.router.lifespan_context = _noop_lifespan

    def current() -> CurrentUser:
        user = SimpleNamespace(
            id="ops-1", email="ops@platform.example", name="Ops", role="admin"
        )
        return CurrentUser(user, org, {})  # type: ignore[arg-type]

    def _db():  # noqa: ANN202
        yield clean_db

    app.dependency_overrides[get_current_user] = current
    app.dependency_overrides[get_db] = _db
    with TestClient(app) as client:
        body = client.get("/api/v1/portal/overview").json()
        people = client.get("/api/v1/portal/accounts").json()

    assert body["accounts"] >= 1
    shared = [row for row in people if row["email"] == email]
    assert len(shared) == 1
    assert shared[0]["workspace_count"] == 2
    assert body["users"] >= body["accounts"]
    assert body["accounts"] == len(people)
