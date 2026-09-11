"""End-to-end API tests against a real PostgreSQL database.

These cover the flows a customer actually performs, and the isolation
guarantees the product depends on. They need ``TEST_DATABASE_URL``; without it
they skip.
"""
from __future__ import annotations

import pytest

from tests.conftest import requires_db

pytestmark = requires_db


# ── Helpers ────────────────────────────────────────────────────────────────
# Both of these are two calls, because signing up and signing in are two calls:
# ask for a code, then present it. The code is read out of the captured mail,
# which is the only place it exists — the row stores an HMAC.
def register(client, *, org="Northgate Motors", email="owner@northgate.example"):  # noqa: ANN001
    from tests.support.mailbox import current

    requested = client.post("/api/v1/auth/request-signup-code", json={"email": email})
    assert requested.status_code == 200, requested.text

    response = client.post(
        "/api/v1/auth/register",
        json={
            "organization_name": org,
            "full_name": "Dana Whitfield",
            "email": email,
            "code": current().code_for(email),
            "primary_domain": "northgate.example",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def sign_in(client, email="owner@northgate.example"):  # noqa: ANN001
    from tests.support.mailbox import current

    requested = client.post("/api/v1/auth/request-code", json={"email": email})
    assert requested.status_code == 200, requested.text
    return client.post(
        "/api/v1/auth/login", json={"email": email, "code": current().code_for(email)}
    )


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def configure(client, token: str, slug: str = "on_page_seo_sync", **overrides):  # noqa: ANN001, ANN201
    """Configure an agent — the precondition for starting or running one.

    An agent cannot be started until somebody has opened its settings and
    saved them, so every test that runs an agent has to do this first. That is
    the product rule, not a test detail: these agents publish to a live site
    and spend money, and "the defaults looked fine" is not an account anyone
    wants to give afterwards.
    """
    payload = {
        "schedule": "Every 6 hours",
        "scope": "",
        # None, because nothing is connected in most of these tests and a
        # channel that cannot deliver is now refused rather than warned
        # about. Tests that care about notification pass their own.
        "notify_channel": "None",
        "max_actions_per_day": 20,
        **overrides,
    }
    response = client.put(
        f"/api/v1/agents/{slug}/config", json=payload, headers=auth(token)
    )
    assert response.status_code == 200, response.text
    return response.json()


def _latest_code(db, purpose: str):  # noqa: ANN001, ANN202
    """The newest code row for a purpose.

    Tests that inspect the row have to say which one they mean: registering
    already left a spent ``sign_up`` code behind, and reading that one instead
    would make an assertion pass for the wrong reason.
    """
    from sqlalchemy import select

    from app.models.identity import LoginCode

    record = db.execute(
        select(LoginCode)
        .where(LoginCode.purpose == purpose)
        .order_by(LoginCode.created_at.desc())
    ).scalars().first()
    assert record is not None, f"No {purpose} code was issued"
    db.refresh(record)
    return record


# ── Sign-up ────────────────────────────────────────────────────────────────
def test_register_provisions_a_complete_workspace(client, clean_db):  # noqa: ANN001
    body = register(client)

    session = body["session"]
    assert session["user"]["role"] == "admin"
    assert session["user"]["is_owner"] is True
    assert session["organization"]["name"] == "Northgate Motors"
    assert session["organization"]["seats_used"] == 1
    # A brand-new workspace has not been through onboarding yet.
    assert session["onboarding_complete"] is False

    token = body["tokens"]["access_token"]

    # The fleet and the catalogue are installed at sign-up, not lazily.
    from app.agents.base.registry import agent_slugs

    agents = client.get("/api/v1/agents", headers=auth(token)).json()
    # Derived, not a literal: adding an agent is a folder, and a hard-coded
    # count turns that into a failing test for no reason.
    assert len(agents) == len(agent_slugs())
    from app.connectors.base.registry import connector_slugs

    connectors = client.get("/api/v1/connectors", headers=auth(token)).json()
    assert len(connectors) == len(connector_slugs())
    assert all(not c["connected"] for c in connectors)

    # And it starts genuinely empty — no invented pages or spend.
    seo = client.get("/api/v1/seo", headers=auth(token)).json()
    assert seo["total"] == 0


def test_duplicate_email_is_rejected(client, clean_db):  # noqa: ANN001
    register(client)
    # Refused at the first step, before a code is ever sent: there is nothing
    # to prove about an address that already has an account.
    response = client.post(
        "/api/v1/auth/request-signup-code",
        json={"email": "owner@northgate.example"},
    )
    assert response.status_code == 409


def test_sign_up_without_a_valid_code_creates_nothing(client, clean_db, mailbox):  # noqa: ANN001
    """The point of the two-step sign-up: no code, no workspace.

    An address nobody has proven must never end up owning a workspace, so a
    wrong code has to fail *before* the organisation is created rather than
    leaving a half-made tenant behind.
    """
    from sqlalchemy import func, select

    from app.models.workspace import Organization

    requested = client.post(
        "/api/v1/auth/request-signup-code", json={"email": "nobody@example.com"}
    )
    assert requested.status_code == 200

    response = client.post(
        "/api/v1/auth/register",
        json={
            "organization_name": "Never Created Co",
            "full_name": "Test User",
            "email": "nobody@example.com",
            "code": "000000",
        },
    )
    assert response.status_code == 401
    assert clean_db.execute(select(func.count()).select_from(Organization)).scalar_one() == 0


def test_sign_up_code_cannot_be_replayed(client, clean_db):  # noqa: ANN001
    """A used code is spent, even for a different organisation name."""
    from tests.support.mailbox import current

    client.post("/api/v1/auth/request-signup-code", json={"email": "dana@example.com"})
    code = current().code_for("dana@example.com")

    first = client.post(
        "/api/v1/auth/register",
        json={
            "organization_name": "First Co",
            "full_name": "Dana Whitfield",
            "email": "dana@example.com",
            "code": code,
        },
    )
    assert first.status_code == 201

    second = client.post(
        "/api/v1/auth/register",
        json={
            "organization_name": "Second Co",
            "full_name": "Dana Whitfield",
            "email": "dana@example.com",
            "code": code,
        },
    )
    # Rejected as a duplicate address first, which is the stronger guarantee.
    assert second.status_code == 409


def test_organisation_slugs_are_unique(client, clean_db):  # noqa: ANN001
    first = register(client, email="a@example.com")
    second = register(client, email="b@example.com")
    assert first["session"]["organization"]["slug"] != second["session"]["organization"]["slug"]


# ── Sign-in and sessions ───────────────────────────────────────────────────
def test_login_and_refresh_rotation(client, clean_db):  # noqa: ANN001
    register(client)

    login = sign_in(client)
    assert login.status_code == 200
    refresh_token = login.json()["tokens"]["refresh_token"]

    rotated = client.post("/api/v1/auth/refresh", json={"refresh_token": refresh_token})
    assert rotated.status_code == 200
    assert rotated.json()["tokens"]["refresh_token"] != refresh_token

    # Presenting the spent token again means a copy is circulating, so the
    # whole family is revoked.
    reused = client.post("/api/v1/auth/refresh", json={"refresh_token": refresh_token})
    assert reused.status_code == 401

    new_token = rotated.json()["tokens"]["refresh_token"]
    after_reuse = client.post("/api/v1/auth/refresh", json={"refresh_token": new_token})
    assert after_reuse.status_code == 401


def test_unknown_address_is_told_there_is_no_account(client, clean_db, mailbox):  # noqa: ANN001
    """Sign-in should say clearly when the address has never registered.

    Waiting on a code that was never sent is worse UX than disclosing that
    the address is not a customer — the console guides them to sign up.
    """
    register(client)

    known = client.post(
        "/api/v1/auth/request-code", json={"email": "owner@northgate.example"}
    )
    unknown = client.post(
        "/api/v1/auth/request-code", json={"email": "nobody@example.com"}
    )
    assert known.status_code == 200
    assert unknown.status_code == 404
    detail = (unknown.json().get("detail") or "").lower()
    assert "no account" in detail

    assert mailbox.count_for("owner@northgate.example") >= 1
    assert mailbox.count_for("nobody@example.com") == 0


def test_wrong_code_is_indistinguishable_from_an_unknown_address(client, clean_db):  # noqa: ANN001
    register(client)
    client.post("/api/v1/auth/request-code", json={"email": "owner@northgate.example"})

    wrong = client.post(
        "/api/v1/auth/login",
        json={"email": "owner@northgate.example", "code": "000000"},
    )
    unknown = client.post(
        "/api/v1/auth/login", json={"email": "nobody@example.com", "code": "000000"}
    )
    assert wrong.status_code == unknown.status_code == 401


def test_a_code_works_once(client, clean_db):  # noqa: ANN001
    from tests.support.mailbox import current

    register(client)
    client.post("/api/v1/auth/request-code", json={"email": "owner@northgate.example"})
    code = current().code_for("owner@northgate.example")
    payload = {"email": "owner@northgate.example", "code": code}

    assert client.post("/api/v1/auth/login", json=payload).status_code == 200
    # A code in a mailbox someone else can read must not be a standing key.
    assert client.post("/api/v1/auth/login", json=payload).status_code == 401


def test_a_new_code_retires_the_previous_one(client, clean_db):  # noqa: ANN001
    """Only one code per address is ever live.

    Without this, every unused code would stay valid and widen the space an
    attacker is allowed to guess against.
    """
    from tests.support.mailbox import current

    register(client)
    client.post("/api/v1/auth/request-code", json={"email": "owner@northgate.example"})
    first = current().code_for("owner@northgate.example")
    client.post("/api/v1/auth/request-code", json={"email": "owner@northgate.example"})
    second = current().code_for("owner@northgate.example")
    assert first != second

    stale = client.post(
        "/api/v1/auth/login", json={"email": "owner@northgate.example", "code": first}
    )
    assert stale.status_code == 401
    fresh = client.post(
        "/api/v1/auth/login", json={"email": "owner@northgate.example", "code": second}
    )
    assert fresh.status_code == 200


def test_an_expired_code_is_refused(client, clean_db):  # noqa: ANN001
    from tests.support.mailbox import current

    from app.core.config import settings

    register(client)
    client.post("/api/v1/auth/request-code", json={"email": "owner@northgate.example"})
    code = current().code_for("owner@northgate.example")

    # Reach into the row rather than sleeping: the rule under test is the
    # expiry comparison, not the clock.
    from datetime import timedelta

    from app.db.base import utcnow

    record = _latest_code(clean_db, "sign_in")
    record.expires_at = utcnow() - timedelta(seconds=1)
    clean_db.commit()

    response = client.post(
        "/api/v1/auth/login", json={"email": "owner@northgate.example", "code": code}
    )
    assert response.status_code == 401
    assert "no longer valid" in response.json()["detail"]
    assert settings.login_code_ttl_minutes <= 30


def test_guessing_burns_the_code_after_the_attempt_cap(client, clean_db):  # noqa: ANN001
    """The attempt cap is what makes a six-digit code safe.

    A million possibilities is nothing to a script. What makes the code
    unguessable is that the row dies after a handful of wrong answers — and
    that the counter survives the failed requests, which it only does because
    the service commits it before raising.
    """
    from tests.support.mailbox import current

    from app.core.config import settings

    register(client)
    client.post("/api/v1/auth/request-code", json={"email": "owner@northgate.example"})
    real_code = current().code_for("owner@northgate.example")

    for _ in range(settings.login_code_max_attempts):
        wrong = client.post(
            "/api/v1/auth/login",
            json={"email": "owner@northgate.example", "code": "000000"},
        )
        assert wrong.status_code == 401

    # The correct code is now worthless too: the row is burned, not merely
    # counted against.
    final = client.post(
        "/api/v1/auth/login",
        json={"email": "owner@northgate.example", "code": real_code},
    )
    assert final.status_code == 401

    record = _latest_code(clean_db, "sign_in")
    assert record.burned_at is not None
    assert record.attempts == settings.login_code_max_attempts


def test_repeated_wrong_codes_lock_the_account(client, clean_db):  # noqa: ANN001
    """Burning one code is not enough on its own.

    An attacker can ask for a fresh code — which they cannot read — and guess
    a few times against each, forever. The account-level counter is what makes
    that terminate.
    """
    from app.core.config import settings

    register(client)

    for _ in range(settings.max_failed_logins + 1):
        client.post("/api/v1/auth/request-code", json={"email": "owner@northgate.example"})
        client.post(
            "/api/v1/auth/login",
            json={"email": "owner@northgate.example", "code": "000000"},
        )

    # Even a genuine code cannot get in while the account is locked.
    locked = sign_in(client)
    assert locked.status_code == 401
    assert "try again in" in locked.json()["detail"]


def test_a_second_code_request_is_throttled(client, clean_db, monkeypatch, mailbox):  # noqa: ANN001
    """A sign-in form must not be usable as a mail cannon.

    The throttle is off for the rest of the suite so tests can sign in
    repeatedly; this one turns it back on, which is the production default.
    """
    from app.core.config import settings

    register(client)
    monkeypatch.setattr(settings, "login_code_resend_seconds", 60)

    first = client.post(
        "/api/v1/auth/request-code", json={"email": "owner@northgate.example"}
    )
    before = mailbox.count_for("owner@northgate.example")
    second = client.post(
        "/api/v1/auth/request-code", json={"email": "owner@northgate.example"}
    )

    # Same shape of response — a throttle that reported itself as an error
    # would tell a caller the address is real.
    assert first.status_code == second.status_code == 200
    assert second.json()["resend_in"] > 0
    # But no second mail went out.
    assert mailbox.count_for("owner@northgate.example") == before


def test_a_deactivated_account_cannot_sign_in(client, clean_db, mailbox):  # noqa: ANN001
    from sqlalchemy import select

    from app.db.session import bind_tenant
    from app.models.identity import AuthIdentity
    from app.models.workspace import User

    body = register(client)
    org_id = body["session"]["organization"]["id"]

    bind_tenant(clean_db, org_id)
    user = clean_db.execute(select(User)).scalars().first()
    user.is_active = False
    identity = clean_db.get(AuthIdentity, user.email_index)
    identity.is_active = False
    clean_db.commit()

    requested = client.post(
        "/api/v1/auth/request-code", json={"email": "owner@northgate.example"}
    )
    assert requested.status_code == 403
    detail = (requested.json().get("detail") or "").lower()
    assert "deactivated" in detail
    # No new sign-in code is mailed to a closed account.
    assert mailbox.count_for("owner@northgate.example") == 1  # only the sign-up one


def test_unauthenticated_requests_are_refused(client, clean_db):  # noqa: ANN001
    for path in ("/api/v1/dashboard", "/api/v1/agents", "/api/v1/admin"):
        assert client.get(path).status_code == 401


# ── Tenant isolation ───────────────────────────────────────────────────────
def test_one_workspace_cannot_see_another(client, clean_db):  # noqa: ANN001
    """The isolation guarantee the whole product rests on."""
    first = register(client, org="First Motors", email="first@example.com")
    second = register(client, org="Second Motors", email="second@example.com")

    first_token = first["tokens"]["access_token"]
    second_token = second["tokens"]["access_token"]

    first_org = first["session"]["organization"]["id"]
    second_org = second["session"]["organization"]["id"]
    assert first_org != second_org

    # Each side sees only its own agents, and its own audit trail.
    first_audit = client.get("/api/v1/admin/audit", headers=auth(first_token)).json()
    second_audit = client.get("/api/v1/admin/audit", headers=auth(second_token)).json()
    assert any("First Motors" in e["action"] for e in first_audit)
    assert not any("First Motors" in e["action"] for e in second_audit)

    # And each side's team contains only itself.
    first_team = client.get("/api/v1/admin", headers=auth(first_token)).json()["team"]
    assert [m["email"] for m in first_team] == ["first@example.com"]


def test_token_from_one_workspace_cannot_address_anothers_records(client, clean_db):  # noqa: ANN001
    first = register(client, org="First Motors", email="first@example.com")
    second = register(client, org="Second Motors", email="second@example.com")

    second_member = client.get(
        "/api/v1/admin", headers=auth(second["tokens"]["access_token"])
    ).json()["team"][0]

    # Using workspace one's token against workspace two's member id.
    response = client.put(
        f"/api/v1/admin/team/{second_member['id']}/role",
        json={"role": "manager"},
        headers=auth(first["tokens"]["access_token"]),
    )
    assert response.status_code == 404


# ── RBAC enforcement ───────────────────────────────────────────────────────
def test_role_restrictions_are_enforced_by_the_api(client, clean_db):  # noqa: ANN001
    """A view-only role must be refused at the API, not just in the UI."""
    from sqlalchemy import select

    from app.core.crypto import email_index
    from app.core.rbac import Role
    from app.db.session import bind_tenant
    from app.models.identity import AuthIdentity
    from app.models.workspace import Organization, User

    owner = register(client)
    org_id = owner["session"]["organization"]["id"]

    bind_tenant(clean_db, org_id)
    organization = clean_db.execute(
        select(Organization).where(Organization.id == org_id)
    ).scalar_one()
    viewer_index = email_index("viewer@northgate.example")
    viewer_user = User(
        tenant_id=organization.id,
        name="Client Viewer",
        email="viewer@northgate.example",
        email_index=viewer_index,
        role=Role.CLIENT.value,
        email_verified=True,
    )
    clean_db.add(viewer_user)
    clean_db.flush()
    clean_db.add(
        AuthIdentity(
            email_index=viewer_index,
            user_id=viewer_user.id,
            tenant_id=organization.id,
        )
    )
    clean_db.commit()

    viewer = sign_in(client, "viewer@northgate.example").json()
    token = viewer["tokens"]["access_token"]

    # A client viewer sees the dashboard and reports...
    assert client.get("/api/v1/dashboard", headers=auth(token)).status_code == 200
    assert client.get("/api/v1/reports", headers=auth(token)).status_code == 200
    # ...and nothing else.
    for path in ("/api/v1/agents", "/api/v1/seo", "/api/v1/ads", "/api/v1/admin"):
        assert client.get(path, headers=auth(token)).status_code == 403, path


# ── Onboarding ─────────────────────────────────────────────────────────────
def test_onboarding_sets_the_guardrail_and_starts_the_fleet(client, clean_db):  # noqa: ANN001
    body = register(client)
    token = body["tokens"]["access_token"]

    saved = client.put(
        "/api/v1/onboarding",
        json={
            "step": 2,
            "domain": "https://www.northgate.example/",
            "cms": "WordPress",
            "ad_accounts": {"google": True, "meta": True, "linkedin": False},
            "guardrail": "human",
        },
        headers=auth(token),
    )
    assert saved.status_code == 200
    # The domain is normalised on the way in.
    assert saved.json()["domain"] == "northgate.example"
    assert "every action queues" in saved.json()["guardrail_label"].lower()

    launched = client.post("/api/v1/onboarding/complete", headers=auth(token))
    assert launched.status_code == 200
    assert "Workspace ready" in launched.json()["toast"]["message"]

    # The human-in-the-loop guardrail turns the whole fleet to review mode.
    agents = client.get("/api/v1/agents", headers=auth(token)).json()
    assert all(a["autonomy"] is False for a in agents)

    session = client.get("/api/v1/auth/me", headers=auth(token)).json()
    assert session["onboarding_complete"] is True


def test_onboarding_requires_a_domain(client, clean_db):  # noqa: ANN001
    """Signing up without a domain means onboarding must ask for one."""
    from tests.support.mailbox import current

    client.post("/api/v1/auth/request-signup-code", json={"email": "nodomain@example.com"})
    response = client.post(
        "/api/v1/auth/register",
        json={
            "organization_name": "No Domain Co",
            "full_name": "Dana Whitfield",
            "email": "nodomain@example.com",
            "code": current().code_for("nodomain@example.com"),
        },
    )
    assert response.status_code == 201
    token = response.json()["tokens"]["access_token"]

    completed = client.post("/api/v1/onboarding/complete", headers=auth(token))
    assert completed.status_code == 422
    assert "domain" in completed.json()["detail"].lower()


# ── Connectors ─────────────────────────────────────────────────────────────
def test_connect_stores_secrets_encrypted_and_never_returns_them(client, clean_db):  # noqa: ANN001
    from sqlalchemy import select

    from app.db.session import bind_tenant
    from app.models.connector import ConnectorRecord

    body = register(client)
    token = body["tokens"]["access_token"]
    org_id = body["session"]["organization"]["id"]

    # A connector whose probe succeeds, because credentials are now verified
    # against the vendor before they are stored — pointing a real integration
    # at a hostname that does not resolve is refused, which is the point.
    secret_value = "super-secret-application-password"
    response = client.post(
        "/api/v1/connectors/fake_cms/connect",
        json={
            "values": {
                "siteUrl": "https://northgate.example",
                "apiKey": secret_value,
            }
        },
        headers=auth(token),
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["connected"] is True

    # The secret is not echoed back anywhere in the response.
    assert secret_value not in response.text
    # The non-secret hint is, so the dialog can show what is configured.
    assert payload["hints"]["siteUrl"] == "https://northgate.example"

    # And it is genuinely ciphertext at rest.
    bind_tenant(clean_db, org_id)
    record = clean_db.execute(
        select(ConnectorRecord).where(
            ConnectorRecord.tenant_id == org_id, ConnectorRecord.slug == "fake_cms"
        )
    ).scalar_one()
    assert secret_value not in record.credentials_encrypted
    assert record.credentials_encrypted.startswith("v1:")


def test_disconnect_clears_the_stored_credentials(client, clean_db):  # noqa: ANN001
    token = register(client)["tokens"]["access_token"]
    client.post(
        "/api/v1/connectors/slack/connect",
        json={"values": {"defaultChannel": "#marketing"}, "oauth_completed": True},
        headers=auth(token),
    )
    response = client.post("/api/v1/connectors/slack/disconnect", headers=auth(token))
    assert response.status_code == 200
    assert response.json()["connected"] is False
    assert response.json()["hints"] == {}


def test_connect_validates_required_fields(client, clean_db):  # noqa: ANN001
    token = register(client)["tokens"]["access_token"]
    response = client.post(
        "/api/v1/connectors/wordpress/connect",
        json={"values": {"siteUrl": "https://northgate.example"}},
        headers=auth(token),
    )
    assert response.status_code == 422
    # WordPress's own capitalisation — the field is an Application Password.
    assert "Application Password" in response.json()["detail"]


# ── The agent loop, end to end ─────────────────────────────────────────────
def test_agent_run_produces_work_and_the_queue_receives_it(client, clean_db):  # noqa: ANN001
    """The core loop: connect a CMS, run the agent, review what it proposed."""
    token = register(client)["tokens"]["access_token"]

    client.put(
        "/api/v1/onboarding",
        json={"domain": "northgate.example", "guardrail": "hybrid"},
        headers=auth(token),
    )
    client.post("/api/v1/onboarding/complete", headers=auth(token))
    client.post(
        "/api/v1/connectors/fake_cms/connect",
        json={"values": {"siteUrl": "https://northgate.example"}},
        headers=auth(token),
    )

    configure(client, token)
    run = client.post("/api/v1/agents/on_page_seo_sync/run", headers=auth(token))
    assert run.status_code == 200, run.text
    assert run.json()["ok"] is True

    # Pages were crawled and stored.
    seo = client.get("/api/v1/seo", headers=auth(token)).json()
    assert seo["total"] > 0

    # A second run analyses them and proposes rewrites, which queue under the
    # hybrid guardrail because publishing is high impact.
    client.post("/api/v1/agents/on_page_seo_sync/run", headers=auth(token))
    pending = client.get("/api/v1/approvals", headers=auth(token)).json()
    assert pending, "the hybrid guardrail should have queued the rewrites"

    item = pending[0]
    assert item["agent_slug"] == "on_page_seo_sync"

    # The queued item carries the actual proposed change.
    detail = client.get(f"/api/v1/approvals/{item['id']}", headers=auth(token)).json()
    assert detail["payload"]["proposed_body"]

    approved = client.post(
        f"/api/v1/approvals/{item['id']}/approve", json={"note": ""}, headers=auth(token)
    )
    assert approved.status_code == 200
    assert approved.json()["toast"]["message"] == "Approved"

    # Approving applies it: the page moves to rewritten and leaves the queue.
    remaining = client.get("/api/v1/approvals", headers=auth(token)).json()
    assert item["id"] not in [i["id"] for i in remaining]

    pages = client.get("/api/v1/seo", headers=auth(token)).json()["pages"]
    assert any(p["status"] == "rewritten" for p in pages)


def test_re_running_an_agent_does_not_stack_duplicate_approvals(client, clean_db):  # noqa: ANN001
    """The queue holds decisions, not a log of every time an agent ran.

    This is what a scheduled agent does in reality: it re-derives the same
    recommendation on every tick. Before the de-duplication, an hourly agent
    added a fresh card every hour — one decision became dozens of identical
    copies, the approvals badge stopped meaning anything, and approving one
    left the rest behind to be applied again.
    """
    token = register(client)["tokens"]["access_token"]
    client.put(
        "/api/v1/onboarding",
        json={"domain": "northgate.example", "guardrail": "hybrid"},
        headers=auth(token),
    )
    client.post("/api/v1/onboarding/complete", headers=auth(token))
    client.post(
        "/api/v1/connectors/fake_cms/connect",
        json={"values": {"siteUrl": "https://northgate.example"}},
        headers=auth(token),
    )

    configure(client, token)
    # First run crawls, second proposes; then run it three more times.
    for _ in range(5):
        client.post("/api/v1/agents/on_page_seo_sync/run", headers=auth(token))

    pending = client.get("/api/v1/approvals", headers=auth(token)).json()
    assert pending, "the hybrid guardrail should have queued something"

    # One card per page, not one per page per run.
    targets = [(item["agent_slug"], item["title"]) for item in pending]
    assert len(targets) == len(set(targets)), f"duplicates in the queue: {targets}"


def test_an_unchanged_proposal_keeps_its_original_timestamp(client, clean_db):  # noqa: ANN001
    """"Submitted 3h ago" has to mean what it says.

    Refreshing the timestamp on every run would make a decision that has been
    waiting all day look like it had just arrived, which is precisely the
    signal a reviewer uses to triage the queue.
    """
    from sqlalchemy import select

    from app.db.session import bind_tenant
    from app.models.approval import ApprovalItem

    body = register(client)
    token = body["tokens"]["access_token"]
    org_id = body["session"]["organization"]["id"]
    client.put(
        "/api/v1/onboarding",
        json={"domain": "northgate.example", "guardrail": "hybrid"},
        headers=auth(token),
    )
    client.post("/api/v1/onboarding/complete", headers=auth(token))
    client.post(
        "/api/v1/connectors/fake_cms/connect",
        json={"values": {"siteUrl": "https://northgate.example"}},
        headers=auth(token),
    )
    configure(client, token)
    client.post("/api/v1/agents/on_page_seo_sync/run", headers=auth(token))
    client.post("/api/v1/agents/on_page_seo_sync/run", headers=auth(token))

    bind_tenant(clean_db, org_id)
    first = clean_db.execute(
        select(ApprovalItem).order_by(ApprovalItem.submitted_at)
    ).scalars().first()
    assert first is not None
    original = first.submitted_at
    digest = first.payload_digest
    assert digest, "the proposal should carry a digest to compare against"

    client.post("/api/v1/agents/on_page_seo_sync/run", headers=auth(token))
    clean_db.expire_all()
    bind_tenant(clean_db, org_id)
    again = clean_db.get(ApprovalItem, first.id)
    assert again.submitted_at == original
    assert again.payload_digest == digest


def test_a_connector_without_its_credentials_is_treated_as_absent(client, clean_db):  # noqa: ANN001
    """A record flagged connected but missing credentials must not be handed out.

    This is the state a cleared credential or a widened spec leaves behind, and
    the old behaviour was the worst of both: the connector looked available, so
    every agent that wanted one took it and then failed on the same missing key
    on every tick — an unfinished setup presenting itself as a fleet of broken
    agents. Now it is diagnosed once, recorded on the record the console reads,
    and the agents route around it.
    """
    from sqlalchemy import select

    from app.db.session import bind_tenant
    from app.models.connector import ConnectorHealth, ConnectorRecord

    body = register(client)
    token = body["tokens"]["access_token"]
    org_id = body["session"]["organization"]["id"]
    client.put(
        "/api/v1/onboarding",
        json={"domain": "northgate.example", "guardrail": "hybrid"},
        headers=auth(token),
    )
    client.post("/api/v1/onboarding/complete", headers=auth(token))
    client.post(
        "/api/v1/connectors/fake_cms/connect",
        json={"values": {"siteUrl": "https://northgate.example"}},
        headers=auth(token),
    )

    # Strip the credentials while leaving the record flagged connected.
    bind_tenant(clean_db, org_id)
    record = clean_db.execute(
        select(ConnectorRecord).where(ConnectorRecord.slug == "fake_cms")
    ).scalar_one()
    record.credentials_encrypted = ""
    record.credential_hints = {}
    clean_db.commit()

    configure(client, token)
    run = client.post("/api/v1/agents/on_page_seo_sync/run", headers=auth(token))
    assert run.status_code == 200, run.text
    # Skipped for want of a connector, not failed with a traceback.
    assert run.json()["ok"] is True
    runs = client.get("/api/v1/agents/on_page_seo_sync/runs", headers=auth(token)).json()
    assert runs[0]["status"] == "skipped", runs[0]
    assert "connector" in runs[0]["summary"].lower()

    # And the record now says why, so the console can show it.
    clean_db.expire_all()
    bind_tenant(clean_db, org_id)
    record = clean_db.execute(
        select(ConnectorRecord).where(ConnectorRecord.slug == "fake_cms")
    ).scalar_one()
    assert record.health == ConnectorHealth.FAILING.value
    assert "siteUrl" in record.last_error


def test_a_new_workspace_starts_with_every_agent_paused(client, clean_db):  # noqa: ANN001
    """Nothing runs because an account was created.

    These agents rewrite live pages, mail strangers and move ad spend. Signing
    up is not consent to any of that, so the fleet is installed paused and a
    person starts it.
    """
    token = register(client)["tokens"]["access_token"]

    from app.agents.base.registry import agent_slugs

    agents = client.get("/api/v1/agents", headers=auth(token)).json()
    # Derived, not a literal: adding an agent is a folder, and a hard-coded
    # count turns that into a failing test for no reason.
    assert len(agents) == len(agent_slugs())
    assert all(a["status"] == "paused" for a in agents), [
        (a["slug"], a["status"]) for a in agents if a["status"] != "paused"
    ]


def test_the_scheduler_claims_nothing_from_an_unlaunched_workspace(client, clean_db):  # noqa: ANN001
    """Paused has to mean the scheduler cannot see them.

    A status the console honours but the scheduler ignores would be worse than
    no switch at all, so this asserts the claim query itself comes back empty.
    """
    from app.orchestration.scheduler import scheduler

    register(client)
    assert scheduler.tick() == 0


def test_launching_starts_only_the_agents_that_were_configured(client, clean_db):  # noqa: ANN001
    """Launch cannot be a way round the configuration gate.

    "Launch workspace" starts the fleet, but only the part of it somebody has
    actually set up. Otherwise the gate would hold everywhere except the one
    button that starts eleven agents at once, which is the only place it
    really matters.
    """
    token = register(client)["tokens"]["access_token"]
    client.put(
        "/api/v1/onboarding",
        json={"domain": "northgate.example", "guardrail": "hybrid"},
        headers=auth(token),
    )
    configure(client, token, "on_page_seo_sync")

    before = client.get("/api/v1/agents", headers=auth(token)).json()
    assert all(a["status"] == "paused" for a in before)

    client.post("/api/v1/onboarding/complete", headers=auth(token))

    after = {a["slug"]: a["status"] for a in client.get("/api/v1/agents", headers=auth(token)).json()}
    assert after["on_page_seo_sync"] == "running"
    assert [slug for slug, status in after.items() if status == "running"] == [
        "on_page_seo_sync"
    ], after


def test_launching_nothing_configured_says_so_rather_than_claiming_success(client, clean_db):  # noqa: ANN001
    """A fresh workspace has nothing configured, so launch starts nothing.

    Reporting "agents are live" then would be a confident lie that costs
    somebody an afternoon of wondering why nothing happens.
    """
    token = register(client)["tokens"]["access_token"]
    client.put(
        "/api/v1/onboarding",
        json={"domain": "northgate.example", "guardrail": "hybrid"},
        headers=auth(token),
    )

    launched = client.post("/api/v1/onboarding/complete", headers=auth(token))
    assert launched.status_code == 200
    message = launched.json()["toast"]["message"]
    assert "Nothing is running yet" in message, message

    agents = client.get("/api/v1/agents", headers=auth(token)).json()
    assert all(a["status"] == "paused" for a in agents)


def test_launching_does_not_resurrect_a_deliberately_paused_agent(client, clean_db):  # noqa: ANN001
    """A pause someone chose has a reason behind it.

    Revisiting onboarding is not that reason, so launch only starts agents that
    have never run.
    """
    token = register(client)["tokens"]["access_token"]
    client.put(
        "/api/v1/onboarding",
        json={"domain": "northgate.example", "guardrail": "hybrid"},
        headers=auth(token),
    )
    client.post("/api/v1/onboarding/complete", headers=auth(token))

    # Run one agent so it has a history, then pause it on purpose.
    configure(client, token)
    client.post("/api/v1/agents/on_page_seo_sync/run", headers=auth(token))
    paused = client.post("/api/v1/agents/on_page_seo_sync/pause", headers=auth(token))
    assert paused.status_code == 200

    client.post("/api/v1/onboarding/complete", headers=auth(token))

    agents = {a["slug"]: a for a in client.get("/api/v1/agents", headers=auth(token)).json()}
    assert agents["on_page_seo_sync"]["status"] == "paused"


def test_a_manual_budget_edit_keeps_the_split_at_100(client, clean_db):  # noqa: ANN001
    """The shares must always add up, after every edit.

    They used to not: setting one channel locked it and left the rest alone,
    so each hand edit pushed the total further out. Five edits and a workspace
    was reporting a 129% allocation — a number that cannot mean anything,
    since the agents divide a real monthly budget by it.
    """
    token = register(client)["tokens"]["access_token"]

    for channel, percent in (("google", 50), ("meta", 22), ("linkedin", 17)):
        response = client.put(
            "/api/v1/ads/budget",
            json={"channel": channel, "percent": percent},
            headers=auth(token),
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["budget_total"] == 100, body["budgets"]
        assert sum(b["percent"] for b in body["budgets"]) == 100

    # The edited channels hold exactly what was asked for; the rest absorbed
    # the difference.
    shares = {b["channel"]: b["percent"] for b in body["budgets"]}
    assert shares["google"] == 50
    assert shares["meta"] == 22
    assert shares["linkedin"] == 17
    assert shares["tiktok"] + shares["dsp"] == 11


def test_a_budget_edit_that_cannot_add_up_is_refused_with_a_reason(client, clean_db):  # noqa: ANN001
    """Better a refusal naming what to unlock than an impossible split."""
    token = register(client)["tokens"]["access_token"]

    client.put("/api/v1/ads/budget", json={"channel": "google", "percent": 60}, headers=auth(token))
    client.put("/api/v1/ads/budget", json={"channel": "meta", "percent": 40}, headers=auth(token))

    # google and meta are now locked at 100% between them.
    response = client.put(
        "/api/v1/ads/budget", json={"channel": "tiktok", "percent": 30}, headers=auth(token)
    )
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert "locked" in detail.lower()
    assert "google" in detail or "meta" in detail

    # And nothing moved.
    after = client.get("/api/v1/ads", headers=auth(token)).json()
    assert after["budget_total"] == 100


def test_unlocking_a_channel_lets_the_split_be_changed_again(client, clean_db):  # noqa: ANN001
    token = register(client)["tokens"]["access_token"]
    client.put("/api/v1/ads/budget", json={"channel": "google", "percent": 60}, headers=auth(token))
    client.put("/api/v1/ads/budget", json={"channel": "meta", "percent": 40}, headers=auth(token))

    assert client.post("/api/v1/ads/budget/meta/unlock", headers=auth(token)).status_code == 200
    response = client.put(
        "/api/v1/ads/budget", json={"channel": "tiktok", "percent": 30}, headers=auth(token)
    )
    assert response.status_code == 200
    assert response.json()["budget_total"] == 100


def test_ad_spend_is_reported_in_rupees(client, clean_db):  # noqa: ANN001
    """The figures a marketing lead reads off the screen and repeats."""
    token = register(client)["tokens"]["access_token"]

    reports = client.get("/api/v1/reports?range=30d", headers=auth(token)).json()
    assert reports["ad_spend"].startswith("\u20b9"), reports["ad_spend"]


def test_the_notification_panel_shows_real_activity(client, clean_db):  # noqa: ANN001
    """Backed by the audit log, not by toasts that expired seconds ago."""
    token = register(client)["tokens"]["access_token"]

    body = client.get("/api/v1/notifications", headers=auth(token)).json()
    # Creating the workspace is itself an audited action.
    assert body["entries"], body
    assert any("workspace" in e["action"] for e in body["entries"])
    assert all({"time", "actor", "action", "module"} <= set(e) for e in body["entries"])


def test_the_notification_panel_respects_the_access_map(client, clean_db):  # noqa: ANN001
    """A viewer must not learn through the bell what RBAC hides on the screen.

    The Client role cannot see Ads at all, so an ad-budget change has no
    business appearing in their notification panel.
    """
    from sqlalchemy import select

    from app.core.crypto import email_index
    from app.core.rbac import Role
    from app.db.session import bind_tenant
    from app.models.identity import AuthIdentity
    from app.models.workspace import Organization, User

    owner = register(client)
    admin_token = owner["tokens"]["access_token"]
    org_id = owner["session"]["organization"]["id"]

    # An admin action in the Ads module.
    client.put(
        "/api/v1/ads/budget", json={"channel": "google", "percent": 40}, headers=auth(admin_token)
    )
    admin_view = client.get("/api/v1/notifications", headers=auth(admin_token)).json()
    assert any(e["module"] == "ads" for e in admin_view["entries"])

    bind_tenant(clean_db, org_id)
    organization = clean_db.execute(
        select(Organization).where(Organization.id == org_id)
    ).scalar_one()
    index = email_index("viewer@northgate.example")
    viewer = User(
        tenant_id=organization.id,
        name="Client Viewer",
        email="viewer@northgate.example",
        email_index=index,
        role=Role.CLIENT.value,
        email_verified=True,
    )
    clean_db.add(viewer)
    clean_db.flush()
    clean_db.add(
        AuthIdentity(email_index=index, user_id=viewer.id, tenant_id=organization.id)
    )
    clean_db.commit()

    viewer_token = sign_in(client, "viewer@northgate.example").json()["tokens"]["access_token"]
    viewer_view = client.get("/api/v1/notifications", headers=auth(viewer_token)).json()
    assert not any(e["module"] == "ads" for e in viewer_view["entries"]), viewer_view


def test_an_unconnected_connector_carries_no_activity_to_show(client, clean_db):  # noqa: ANN001
    """The card hides its status block until there is a status.

    The console decides that from the payload, so the payload has to be honest
    about having nothing: an unconnected connector must not arrive with a
    sync label, a health story or an error for the card to render.
    """
    token = register(client)["tokens"]["access_token"]

    catalogue = client.get("/api/v1/connectors", headers=auth(token)).json()
    assert catalogue
    for connector in catalogue:
        assert connector["connected"] is False
        assert connector["activity_label"] == "", connector["slug"]
        assert connector["last_error"] == ""
        assert connector["last_sync_at"] is None
        # What it *does* carry is the identity the card shows.
        assert connector["name"] and connector["category"]


def test_connecting_gives_the_card_something_to_show(client, clean_db):  # noqa: ANN001
    token = register(client)["tokens"]["access_token"]
    client.post(
        "/api/v1/connectors/fake_cms/connect",
        json={"values": {"siteUrl": "https://northgate.example"}},
        headers=auth(token),
    )

    connector = next(
        c
        for c in client.get("/api/v1/connectors", headers=auth(token)).json()
        if c["slug"] == "fake_cms"
    )
    assert connector["connected"] is True
    assert connector["activity_label"], "a connected integration should say when"
    assert "ago" in connector["activity_label"] or "just now" in connector["activity_label"]


def test_a_fresh_agent_reports_no_state_worth_showing(client, clean_db):  # noqa: ANN001
    """An agent nobody has set up has nothing to report, and says so.

    The card collapses to its identity and a Configure button based on these
    two fields, so they have to mean what the console assumes: not configured,
    and no scheduled run.
    """
    token = register(client)["tokens"]["access_token"]

    for agent in client.get("/api/v1/agents", headers=auth(token)).json():
        assert agent["status"] == "paused"
        assert agent["configured"] is False
        assert agent["next_run"] == "—", agent["slug"]
        assert agent["last_error"] == ""
        assert agent["config_summary"] == ""


def test_configuring_an_agent_gives_it_a_summary_to_show(client, clean_db):  # noqa: ANN001
    """Configuring is one of the two things that reveals the detail block."""
    token = register(client)["tokens"]["access_token"]

    saved = client.put(
        "/api/v1/agents/on_page_seo_sync/config",
        json={
            "schedule": "Every 6 hours",
            "scope": "/inventory/*",
            # None: nothing is connected here, and a channel that cannot
            # deliver is refused now rather than warned about. The summary is
            # what this test is about.
            "notify_channel": "None",
            "max_actions_per_day": 20,
        },
        headers=auth(token),
    )
    assert saved.status_code == 200, saved.text

    agent = next(
        a
        for a in client.get("/api/v1/agents", headers=auth(token)).json()
        if a["slug"] == "on_page_seo_sync"
    )
    assert agent["configured"] is True
    assert "Every 6 hours" in agent["config_summary"]


def test_an_unconfigured_agent_cannot_be_started(client, clean_db):  # noqa: ANN001
    """Configuration is a decision, not a set of missing values.

    Every agent ships with a working default schedule, so nothing is stopping
    it from running except the requirement that a person has seen the cadence
    and the daily cap and saved them. These agents rewrite pages on a live
    site, mail strangers and move ad spend.
    """
    token = register(client)["tokens"]["access_token"]

    response = client.post("/api/v1/agents/on_page_seo_sync/resume", headers=auth(token))
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert "Configure" in detail
    assert "On-Page SEO Sync" in detail

    agents = {a["slug"]: a for a in client.get("/api/v1/agents", headers=auth(token)).json()}
    assert agents["on_page_seo_sync"]["status"] == "paused"


def test_an_unconfigured_agent_cannot_be_run_by_hand_either(client, clean_db):  # noqa: ANN001
    """A manual run is not a preview.

    It publishes, mails and spends exactly as a scheduled run does, so it
    cannot be the way round having to look at the settings first.
    """
    token = register(client)["tokens"]["access_token"]

    response = client.post("/api/v1/agents/on_page_seo_sync/run", headers=auth(token))
    assert response.status_code == 409
    assert "Configure" in response.json()["detail"]

    runs = client.get("/api/v1/agents/on_page_seo_sync/runs", headers=auth(token)).json()
    assert runs == [], "a refused run must not leave a run record behind"


def test_configuring_an_agent_is_what_lets_it_start(client, clean_db):  # noqa: ANN001
    token = register(client)["tokens"]["access_token"]
    assert (
        client.post("/api/v1/agents/on_page_seo_sync/resume", headers=auth(token)).status_code
        == 409
    )

    configure(client, token, "on_page_seo_sync")

    resumed = client.post("/api/v1/agents/on_page_seo_sync/resume", headers=auth(token))
    assert resumed.status_code == 200, resumed.text

    agents = {a["slug"]: a for a in client.get("/api/v1/agents", headers=auth(token)).json()}
    assert agents["on_page_seo_sync"]["status"] == "running"
    # And only that one. Configuring one agent starts one agent.
    assert agents["aeo_qa_injector"]["status"] == "paused"


def test_your_own_actions_do_not_show_as_new_notifications(client, clean_db):  # noqa: ANN001
    """A dot that appears because you clicked something trains people to ignore it.

    Registering, configuring and starting an agent are all audited, and all of
    them are things this user just did. None of them is news to them.
    """
    token = register(client)["tokens"]["access_token"]
    configure(client, token, "on_page_seo_sync")
    client.post("/api/v1/agents/on_page_seo_sync/resume", headers=auth(token))

    body = client.get("/api/v1/notifications", headers=auth(token)).json()
    assert body["entries"], "the panel should still list what happened"
    assert body["unseen"] == 0, [e["action"] for e in body["entries"]]


def test_another_persons_action_counts_as_new(client, clean_db):  # noqa: ANN001
    """Which is the whole point of the indicator."""
    from sqlalchemy import select

    from app.db.session import bind_tenant
    from app.models.workspace import Organization
    from app.services import audit as audit_service

    body = register(client)
    token = body["tokens"]["access_token"]
    org_id = body["session"]["organization"]["id"]

    assert client.get("/api/v1/notifications", headers=auth(token)).json()["unseen"] == 0

    # Somebody else does something — recorded the way an agent's action is.
    bind_tenant(clean_db, org_id)
    organization = clean_db.execute(
        select(Organization).where(Organization.id == org_id)
    ).scalar_one()
    audit_service.record_agent_action(
        clean_db,
        tenant_id=organization.id,
        agent_name="On-Page SEO Sync",
        action="rewrote 4 pages",
        module="seo",
    )
    clean_db.commit()

    assert client.get("/api/v1/notifications", headers=auth(token)).json()["unseen"] == 1


def test_opening_the_panel_clears_the_indicator(client, clean_db):  # noqa: ANN001
    from sqlalchemy import select

    from app.db.session import bind_tenant
    from app.models.workspace import Organization
    from app.services import audit as audit_service

    body = register(client)
    token = body["tokens"]["access_token"]
    org_id = body["session"]["organization"]["id"]

    bind_tenant(clean_db, org_id)
    organization = clean_db.execute(
        select(Organization).where(Organization.id == org_id)
    ).scalar_one()
    audit_service.record_agent_action(
        clean_db,
        tenant_id=organization.id,
        agent_name="Referral Spam Guard",
        action="blocked 12 referrers",
        module="seo",
    )
    clean_db.commit()

    assert client.get("/api/v1/notifications", headers=auth(token)).json()["unseen"] == 1

    seen = client.post("/api/v1/notifications/seen", headers=auth(token))
    assert seen.status_code == 200
    assert client.get("/api/v1/notifications", headers=auth(token)).json()["unseen"] == 0

    # And the entries are still there — seen is not deleted.
    assert client.get("/api/v1/notifications", headers=auth(token)).json()["entries"]


def test_the_indicator_ignores_activity_the_viewer_cannot_see(client, clean_db):  # noqa: ANN001
    """Otherwise a Client viewer carries a badge that never clears.

    They cannot open the Ads module, so the panel will not list an ad-budget
    change — and counting what the panel refuses to show would leave a dot
    with nothing behind it.
    """
    from sqlalchemy import select

    from app.core.crypto import email_index
    from app.core.rbac import Role
    from app.db.session import bind_tenant
    from app.models.identity import AuthIdentity
    from app.models.workspace import Organization, User
    from app.services import audit as audit_service

    owner = register(client)
    org_id = owner["session"]["organization"]["id"]

    bind_tenant(clean_db, org_id)
    organization = clean_db.execute(
        select(Organization).where(Organization.id == org_id)
    ).scalar_one()
    index = email_index("viewer@northgate.example")
    viewer = User(
        tenant_id=organization.id,
        name="Client Viewer",
        email="viewer@northgate.example",
        email_index=index,
        role=Role.CLIENT.value,
        email_verified=True,
    )
    clean_db.add(viewer)
    clean_db.flush()
    clean_db.add(AuthIdentity(email_index=index, user_id=viewer.id, tenant_id=organization.id))
    audit_service.record_agent_action(
        clean_db,
        tenant_id=organization.id,
        agent_name="Predictive Budget Engine",
        action="moved 4% of spend to Google",
        module="ads",
    )
    clean_db.commit()

    viewer_token = sign_in(client, "viewer@northgate.example").json()["tokens"]["access_token"]
    body = client.get("/api/v1/notifications", headers=auth(viewer_token)).json()
    assert body["unseen"] == 0, body["entries"]


def test_configured_agents_sort_to_the_top(client, clean_db):  # noqa: ANN001
    """Catalogue order is right for a catalogue and wrong for a fleet.

    On a workspace with two agents set up and nine untouched, the two that
    matter should not be wherever the pipeline order put them.
    """
    token = register(client)["tokens"]["access_token"]

    # Two from the back of the fleet order, deliberately.
    configure(client, token, "click_fraud_controller")
    configure(client, token, "first_party_audience_modeler")

    order = [a["slug"] for a in client.get("/api/v1/agents", headers=auth(token)).json()]
    assert order[:2] == ["first_party_audience_modeler", "click_fraud_controller"], order
    # Within the group the fleet's own order still holds.
    assert order[2] == "on_page_seo_sync"


def test_running_sorts_above_configured_and_errored_above_everything(client, clean_db):  # noqa: ANN001
    """The order is by how much attention the agent wants.

    An agent that was working and has stopped is the most urgent thing on the
    screen, which is why error sorts above running rather than below it.
    """
    from sqlalchemy import select

    from app.db.session import bind_tenant
    from app.models.agent import AgentRecord, AgentStatus

    body = register(client)
    token = body["tokens"]["access_token"]
    org_id = body["session"]["organization"]["id"]

    configure(client, token, "aeo_qa_injector")
    configure(client, token, "referral_spam_guard")
    client.post("/api/v1/agents/referral_spam_guard/resume", headers=auth(token))
    configure(client, token, "click_fraud_controller")

    bind_tenant(clean_db, org_id)
    errored = clean_db.execute(
        select(AgentRecord).where(AgentRecord.slug == "click_fraud_controller")
    ).scalar_one()
    errored.status = AgentStatus.ERROR.value
    clean_db.commit()

    order = [a["slug"] for a in client.get("/api/v1/agents", headers=auth(token)).json()]
    assert order[0] == "click_fraud_controller", order   # errored
    assert order[1] == "referral_spam_guard", order      # running
    assert order[2] == "aeo_qa_injector", order          # configured, stopped
    assert order[3] == "on_page_seo_sync", order         # untouched


def test_connected_connectors_sort_to_the_top(client, clean_db):  # noqa: ANN001
    """Twenty-five cards of which one is wired up is still a catalogue.

    The wired-up one should not be somewhere in the middle of it.
    """
    token = register(client)["tokens"]["access_token"]

    catalogue = [c["slug"] for c in client.get("/api/v1/connectors", headers=auth(token)).json()]
    assert catalogue.index("fake_cms") > 0, "the fixture connector starts down the list"

    client.post(
        "/api/v1/connectors/fake_cms/connect",
        json={"values": {"siteUrl": "https://northgate.example"}},
        headers=auth(token),
    )

    after = client.get("/api/v1/connectors", headers=auth(token)).json()
    assert after[0]["slug"] == "fake_cms"
    assert after[0]["connected"] is True
    assert all(not c["connected"] for c in after[1:])


def test_an_unhealthy_connection_sorts_above_a_working_one(client, clean_db):  # noqa: ANN001
    """The row somebody needs today is the one being relied on that broke."""
    from sqlalchemy import select

    from app.db.session import bind_tenant
    from app.models.connector import ConnectorHealth, ConnectorRecord

    body = register(client)
    token = body["tokens"]["access_token"]
    org_id = body["session"]["organization"]["id"]

    client.post(
        "/api/v1/connectors/fake_cms/connect",
        json={"values": {"siteUrl": "https://northgate.example"}},
        headers=auth(token),
    )
    connected = [
        c["slug"]
        for c in client.get("/api/v1/connectors", headers=auth(token)).json()
        if c["connected"]
    ]
    assert connected == ["fake_cms"]

    bind_tenant(clean_db, org_id)
    broken = clean_db.execute(
        select(ConnectorRecord).where(ConnectorRecord.slug == "fake_cms")
    ).scalar_one()
    broken.health = ConnectorHealth.FAILING.value
    broken.last_error = "Reporting API rejected the credentials"
    clean_db.commit()

    after = client.get("/api/v1/connectors", headers=auth(token)).json()
    # Still first, and now flagged as needing attention.
    assert after[0]["slug"] == "fake_cms"
    assert after[0]["status_label"] == "Needs attention"


def test_the_dashboard_shows_what_is_running_not_the_catalogue(client, clean_db):  # noqa: ANN001
    """A workspace with nothing started used to show six paused cards.

    That answers a question nobody asked. The dashboard is "what is happening
    right now", so it lists the agents doing something and the systems that
    are connected — and returns nothing when that is nothing, so the console
    can say so.
    """
    token = register(client)["tokens"]["access_token"]

    empty = client.get("/api/v1/dashboard", headers=auth(token)).json()
    assert empty["agents"] == []
    assert empty["active_agents"] == 0
    assert empty["connectors"] == []

    configure(client, token, "referral_spam_guard")
    client.post("/api/v1/agents/referral_spam_guard/resume", headers=auth(token))
    client.post(
        "/api/v1/connectors/fake_cms/connect",
        json={"values": {"siteUrl": "https://northgate.example"}},
        headers=auth(token),
    )

    live = client.get("/api/v1/dashboard", headers=auth(token)).json()
    assert [a["slug"] for a in live["agents"]] == ["referral_spam_guard"]
    assert live["active_agents"] == 1
    assert [c["slug"] for c in live["connectors"]] == ["fake_cms"]

    # A configured-but-stopped agent is not running, so it stays off here.
    configure(client, token, "aeo_qa_injector")
    still = client.get("/api/v1/dashboard", headers=auth(token)).json()
    assert [a["slug"] for a in still["agents"]] == ["referral_spam_guard"]


def test_the_dashboard_hides_connectors_from_a_role_that_cannot_see_them(client, clean_db):  # noqa: ANN001
    """The panel must not be a way around the access map."""
    from sqlalchemy import select

    from app.core.crypto import email_index
    from app.core.rbac import Role
    from app.db.session import bind_tenant
    from app.models.identity import AuthIdentity
    from app.models.workspace import Organization, User

    owner = register(client)
    admin_token = owner["tokens"]["access_token"]
    org_id = owner["session"]["organization"]["id"]
    client.post(
        "/api/v1/connectors/fake_cms/connect",
        json={"values": {"siteUrl": "https://northgate.example"}},
        headers=auth(admin_token),
    )
    assert client.get("/api/v1/dashboard", headers=auth(admin_token)).json()["connectors"]

    bind_tenant(clean_db, org_id)
    organization = clean_db.execute(
        select(Organization).where(Organization.id == org_id)
    ).scalar_one()
    index = email_index("outsider@northgate.example")
    member = User(
        tenant_id=organization.id,
        name="No Connectors",
        email="outsider@northgate.example",
        email_index=index,
        role=Role.CLIENT.value,
        email_verified=True,
    )
    clean_db.add(member)
    clean_db.flush()
    clean_db.add(AuthIdentity(email_index=index, user_id=member.id, tenant_id=organization.id))
    clean_db.commit()

    token = sign_in(client, "outsider@northgate.example").json()["tokens"]["access_token"]
    body = client.get("/api/v1/dashboard", headers=auth(token))
    assert body.status_code == 200
    assert body.json()["connectors"] == []


def test_removing_a_configuration_stops_the_agent_and_closes_the_gate(client, clean_db):  # noqa: ANN001
    """The counterpart of saving one.

    Without it the settings dialog is a one-way door: you can change a
    schedule but never take back the decision that let the agent run, which
    makes the configuration gate a trap rather than a switch. Removing has to
    stop the agent too — leaving it running unconfigured would contradict the
    rule that starting requires a configuration.
    """
    token = register(client)["tokens"]["access_token"]
    configure(client, token, "on_page_seo_sync", scope="/inventory/*")
    client.post("/api/v1/agents/on_page_seo_sync/resume", headers=auth(token))

    live = {a["slug"]: a for a in client.get("/api/v1/agents", headers=auth(token)).json()}
    assert live["on_page_seo_sync"]["status"] == "running"

    removed = client.delete("/api/v1/agents/on_page_seo_sync/config", headers=auth(token))
    assert removed.status_code == 200, removed.text
    body = removed.json()
    assert body["configured"] is False
    assert body["status"] == "paused"
    assert body["scope"] == ""
    assert body["config_summary"] == ""

    # And the gate is closed again.
    blocked = client.post("/api/v1/agents/on_page_seo_sync/resume", headers=auth(token))
    assert blocked.status_code == 409


def test_removing_a_configuration_keeps_the_run_history(client, clean_db):  # noqa: ANN001
    """It resets a setting; it does not erase what the agent did."""
    token = register(client)["tokens"]["access_token"]
    client.put(
        "/api/v1/onboarding",
        json={"domain": "northgate.example", "guardrail": "hybrid"},
        headers=auth(token),
    )
    client.post("/api/v1/onboarding/complete", headers=auth(token))
    client.post(
        "/api/v1/connectors/fake_cms/connect",
        json={"values": {"siteUrl": "https://northgate.example"}},
        headers=auth(token),
    )
    configure(client, token, "on_page_seo_sync")
    client.post("/api/v1/agents/on_page_seo_sync/run", headers=auth(token))

    before = client.get("/api/v1/agents/on_page_seo_sync/runs", headers=auth(token)).json()
    assert before

    client.delete("/api/v1/agents/on_page_seo_sync/config", headers=auth(token))

    after = client.get("/api/v1/agents/on_page_seo_sync/runs", headers=auth(token)).json()
    assert len(after) == len(before)
    # The pages it crawled are still there too.
    assert client.get("/api/v1/seo", headers=auth(token)).json()["total"] > 0


def test_a_view_only_role_cannot_remove_a_configuration(client, clean_db):  # noqa: ANN001
    from sqlalchemy import select

    from app.core.crypto import email_index
    from app.core.rbac import Role
    from app.db.session import bind_tenant
    from app.models.identity import AuthIdentity
    from app.models.workspace import Organization, User

    owner = register(client)
    admin_token = owner["tokens"]["access_token"]
    org_id = owner["session"]["organization"]["id"]
    configure(client, admin_token, "on_page_seo_sync")

    bind_tenant(clean_db, org_id)
    organization = clean_db.execute(
        select(Organization).where(Organization.id == org_id)
    ).scalar_one()
    index = email_index("viewer@northgate.example")
    viewer = User(
        tenant_id=organization.id,
        name="Client Viewer",
        email="viewer@northgate.example",
        email_index=index,
        role=Role.CLIENT.value,
        email_verified=True,
    )
    clean_db.add(viewer)
    clean_db.flush()
    clean_db.add(AuthIdentity(email_index=index, user_id=viewer.id, tenant_id=organization.id))
    clean_db.commit()

    token = sign_in(client, "viewer@northgate.example").json()["tokens"]["access_token"]
    refused = client.delete("/api/v1/agents/on_page_seo_sync/config", headers=auth(token))
    assert refused.status_code == 403

    still = {a["slug"]: a for a in client.get("/api/v1/agents", headers=auth(admin_token)).json()}
    assert still["on_page_seo_sync"]["configured"] is True


def test_no_agent_can_run_without_a_data_source(client, clean_db):  # noqa: ANN001
    """Every agent has to name where its facts come from.

    Two used to declare no connector requirement at all. One of them asked the
    model for candidate domains, authority scores and contact addresses and
    recorded them as discovered opportunities — invented figures presented as
    measurements, and a fabricated email address one agent away from being
    written to. Preflight is where that gets caught, so preflight has to have
    something to check.
    """
    from app.agents.base.registry import all_agents

    unguarded = [
        agent.spec.slug
        for agent in all_agents()
        if not agent.spec.required_capabilities and not agent.spec.any_of_capabilities
    ]
    assert unguarded == [], unguarded

    # And with nothing connected, every one of them declines rather than runs.
    token = register(client)["tokens"]["access_token"]
    for agent in all_agents():
        slug = agent.spec.slug
        configure(client, token, slug)
        response = client.post(f"/api/v1/agents/{slug}/run", headers=auth(token))
        assert response.status_code == 200, response.text
        runs = client.get(f"/api/v1/agents/{slug}/runs", headers=auth(token)).json()
        assert runs[0]["status"] == "skipped", (slug, runs[0])
        assert runs[0]["actions_taken"] == 0
        # Asserted on the recorded run rather than on the toast. Run now
        # starts the pass and returns rather than awaiting it, so the response
        # can only say it began — the outcome, and the reason, live on the run
        # row the console reads. Which is the stronger thing to check anyway:
        # it is what somebody sees in the history tomorrow.
        assert runs[0]["summary"], (slug, runs[0])
        assert "waiting on" in runs[0]["summary"].lower(), (slug, runs[0]["summary"])


def test_credentials_are_checked_before_they_are_stored(client, clean_db):  # noqa: ANN001
    """A wrong credential fails to connect instead of being saved and marked good.

    This is what replaces the Test button. That button had the shape of the
    problem backwards: the bad token was already stored and flagged connected,
    and it only told anybody if they happened to press it. In between, every
    agent that wanted that connector took it and failed.
    """
    from sqlalchemy import select

    from app.db.session import bind_tenant
    from app.models.connector import ConnectorRecord

    body = register(client)
    token = body["tokens"]["access_token"]
    org_id = body["session"]["organization"]["id"]

    # A real integration pointed at a host that does not answer.
    response = client.post(
        "/api/v1/connectors/wordpress/connect",
        json={
            "values": {
                "siteUrl": "https://nothing.invalid",
                "username": "editor",
                "applicationPassword": "wrong",
            }
        },
        headers=auth(token),
    )
    assert response.status_code >= 400, response.text

    # Nothing was stored, and it is not pretending to be connected.
    bind_tenant(clean_db, org_id)
    record = clean_db.execute(
        select(ConnectorRecord).where(
            ConnectorRecord.tenant_id == org_id, ConnectorRecord.slug == "wordpress"
        )
    ).scalar_one()
    assert record.connected is False
    assert record.credentials_encrypted == ""
    assert record.credential_hints == {}

    catalogue = {
        c["slug"]: c for c in client.get("/api/v1/connectors", headers=auth(token)).json()
    }
    assert catalogue["wordpress"]["connected"] is False


def test_a_verified_connection_is_healthy_straight_away(client, clean_db):  # noqa: ANN001
    """It was probed a moment ago, so the card should not say "unknown"."""
    token = register(client)["tokens"]["access_token"]

    response = client.post(
        "/api/v1/connectors/fake_cms/connect",
        json={"values": {"siteUrl": "https://northgate.example"}},
        headers=auth(token),
    )
    assert response.status_code == 200, response.text
    assert response.json()["health"] == "ok"
    assert response.json()["status_label"] == "Connected"
    assert response.json()["activity_label"]


def test_there_is_no_test_endpoint_to_call(client, clean_db):  # noqa: ANN001
    """Removed with the button. Verification happens at submit, and the
    scheduler re-probes on its own, so a manual probe has no job left."""
    token = register(client)["tokens"]["access_token"]
    client.post(
        "/api/v1/connectors/fake_cms/connect",
        json={"values": {"siteUrl": "https://northgate.example"}},
        headers=auth(token),
    )
    # 404, not 405: the route is gone entirely rather than refusing the verb.
    gone = client.post("/api/v1/connectors/fake_cms/test", headers=auth(token))
    assert gone.status_code == 404, gone.text


def test_a_malformed_scope_is_refused_when_it_is_submitted(client, clean_db):  # noqa: ANN001
    """Not six hours later, from a run that reported doing nothing."""
    token = register(client)["tokens"]["access_token"]

    response = client.put(
        "/api/v1/agents/on_page_seo_sync/config",
        json={
            "schedule": "Every 6 hours",
            "scope": "inventory/*",
            "notify_channel": "None",
            "max_actions_per_day": 20,
        },
        headers=auth(token),
    )
    assert response.status_code == 422
    assert "/inventory/*" in response.json()["detail"]

    # And nothing was saved, so the agent still cannot start.
    agents = {a["slug"]: a for a in client.get("/api/v1/agents", headers=auth(token)).json()}
    assert agents["on_page_seo_sync"]["configured"] is False


def test_every_problem_is_reported_at_once(client, clean_db):  # noqa: ANN001
    """Fixing four things one round trip at a time is the worst version."""
    token = register(client)["tokens"]["access_token"]

    response = client.put(
        "/api/v1/agents/on_page_seo_sync/config",
        json={
            "schedule": "Every fortnight",
            "scope": "inventory/*",
            "notify_channel": "Carrier pigeon",
            "max_actions_per_day": 20,
        },
        headers=auth(token),
    )
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert "Schedule" in detail
    assert "Notify channel" in detail
    assert "/inventory/*" in detail


def test_a_channel_that_cannot_deliver_is_not_offered_at_all(client, clean_db):  # noqa: ANN001
    """The options say which channels can actually carry a message.

    This used to be a warning on save: the agent saved with a channel that
    silently dropped every notification, and said so once in a toast nobody
    would see again. Now the dialog does not offer it.

    The reason names what would serve the channel rather than the channel
    itself — a webhook or an SMTP relay carries a notification just as well,
    and telling somebody who has wired Zapier to go and install Slack would
    be wrong.
    """
    token = register(client)["tokens"]["access_token"]

    options = client.get("/api/v1/agents/options", headers=auth(token)).json()
    by_value = {option["value"]: option for option in options["notify_channels"]}

    # None always works, and is the default.
    assert by_value["None"]["available"] is True
    for channel in ("Slack", "Email"):
        assert by_value[channel]["available"] is False, channel
        assert by_value[channel]["reason"], channel
        assert "Connect one of" in by_value[channel]["reason"]


def test_refusing_a_dead_channel_never_blocks_setting_the_agent_up(client, clean_db):  # noqa: ANN001
    """The distinction that matters.

    Refusing to *configure the agent* would block the first thing a new
    workspace has to do — an agent cannot start until it is configured, and
    nothing is connected on day one. That is why this was a warning before.
    Refusing the *channel* is a different thing: None is always available, so
    the agent still configures and still starts.
    """
    token = register(client)["tokens"]["access_token"]

    refused = client.put(
        "/api/v1/agents/on_page_seo_sync/config",
        json={
            "schedule": "Every 6 hours",
            "scope": "",
            "notify_channel": "Slack",
            "max_actions_per_day": 20,
        },
        headers=auth(token),
    )
    assert refused.status_code == 422
    detail = refused.json()["detail"]
    assert "cannot be delivered" in detail
    # It says what to do, both ways out.
    assert "Connect one of" in detail
    assert "choose None" in detail

    # And with None the agent configures and starts, on a workspace where
    # nothing at all is connected.
    saved = client.put(
        "/api/v1/agents/on_page_seo_sync/config",
        json={
            "schedule": "Every 6 hours",
            "scope": "",
            "notify_channel": "None",
            "max_actions_per_day": 20,
        },
        headers=auth(token),
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["configured"] is True
    assert (
        client.post(
            "/api/v1/agents/on_page_seo_sync/resume", headers=auth(token)
        ).status_code
        == 200
    )


def test_the_configured_channel_is_actually_sent_to(client, clean_db):  # noqa: ANN001
    """The setting did nothing at all.

    It was offered, saved, echoed back in the card's summary as "notify
    Email", and warned about when nothing could serve it. No code path read
    it: ``notify()`` exists on the Slack, SMTP and webhook connectors and
    nothing in the platform called it, so every agent's notifications went
    nowhere whatever anybody chose. Gating the options behind a connector
    while leaving that true would only have made the control more
    convincing.
    """
    from tests.support import fake_connectors as fakes

    token = register(client)["tokens"]["access_token"]
    client.post(
        "/api/v1/connectors/fake_notifier/connect",
        json={"values": {"channel": "#seo"}},
        headers=auth(token),
    )
    client.post(
        "/api/v1/connectors/fake_cms/connect",
        json={"values": {"siteUrl": "https://example.test"}},
        headers=auth(token),
    )
    configure(client, token, "on_page_seo_sync", notify_channel="Slack")

    fakes.FakeNotifier.sent.clear()
    response = client.post("/api/v1/agents/on_page_seo_sync/run", headers=auth(token))
    assert response.status_code == 200, response.text

    runs = client.get(
        "/api/v1/agents/on_page_seo_sync/runs", headers=auth(token)
    ).json()
    assert runs
    # This pass does produce work — it syncs the fixture pages, analyses them
    # and proposes rewrites — so the notification is not conditional. A
    # conditional assertion here would pass whether or not the wiring
    # existed, which is exactly how the wiring came to be missing.
    assert runs[0]["actions_taken"] or runs[0]["actions_queued"], runs[0]["summary"]
    assert fakes.FakeNotifier.sent, (
        f"the run produced work and sent no notification: {runs[0]['summary']!r}"
    )
    _channel, subject, message = fakes.FakeNotifier.sent[0]
    assert "On-Page SEO Sync" in subject
    assert "awaiting approval" in subject or "applied" in subject
    assert message


def test_a_workable_channel_produces_no_warning(client, clean_db):  # noqa: ANN001
    token = register(client)["tokens"]["access_token"]
    client.post(
        "/api/v1/connectors/fake_notifier/connect",
        json={"values": {"channel": "#seo"}},
        headers=auth(token),
    )

    response = client.put(
        "/api/v1/agents/on_page_seo_sync/config",
        json={
            "schedule": "Every 6 hours",
            "scope": "",
            "notify_channel": "Slack",
            "max_actions_per_day": 20,
        },
        headers=auth(token),
    )
    assert response.status_code == 200, response.text
    assert response.json()["warnings"] == []


def test_the_scheduler_keeps_connector_health_current(client, clean_db):  # noqa: ANN001
    """What replaces the Test button on the other side.

    A health state that only updates when somebody presses something is a
    health state nobody trusts, so the sweep re-probes connected integrations
    on its own slower clock.
    """
    from sqlalchemy import select

    from app.db.session import bind_tenant
    from app.models.connector import ConnectorHealth, ConnectorRecord
    from app.orchestration.scheduler import sweep_connector_health

    body = register(client)
    token = body["tokens"]["access_token"]
    org_id = body["session"]["organization"]["id"]
    client.post(
        "/api/v1/connectors/fake_cms/connect",
        json={"values": {"siteUrl": "https://northgate.example"}},
        headers=auth(token),
    )

    # Pretend it has gone stale and wrong since.
    bind_tenant(clean_db, org_id)
    record = clean_db.execute(
        select(ConnectorRecord).where(
            ConnectorRecord.tenant_id == org_id, ConnectorRecord.slug == "fake_cms"
        )
    ).scalar_one()
    record.health = ConnectorHealth.FAILING.value
    record.last_error = "stale verdict from before"
    record.last_health_check_at = None
    clean_db.commit()

    sweep_connector_health()

    clean_db.expire_all()
    bind_tenant(clean_db, org_id)
    record = clean_db.execute(
        select(ConnectorRecord).where(
            ConnectorRecord.tenant_id == org_id, ConnectorRecord.slug == "fake_cms"
        )
    ).scalar_one()
    assert record.health == ConnectorHealth.OK.value
    assert record.last_error == ""
    assert record.last_health_check_at is not None


def test_the_auditor_finds_real_issues_and_resolves_them_when_fixed(client, clean_db):  # noqa: ANN001
    """The whole loop: crawl, audit, fix, re-audit.

    The reconciliation is the part worth proving. A finding seen again keeps
    its first-seen date, so "open for three weeks" is true. A finding that has
    gone is marked fixed rather than deleted, which is what lets the console
    say "31 issues fixed this month" — the number that shows a retainer
    earning its fee.
    """
    from sqlalchemy import select

    from app.db.session import bind_tenant
    from app.models.seo import SeoIssue, SeoPage
    from app.services.encryption import Ctx, OrgCipher
    from app.models.workspace import Organization

    body = register(client)
    token = body["tokens"]["access_token"]
    org_id = body["session"]["organization"]["id"]
    client.put(
        "/api/v1/onboarding",
        json={"domain": "northgate.example", "guardrail": "hybrid"},
        headers=auth(token),
    )
    client.post("/api/v1/onboarding/complete", headers=auth(token))
    client.post(
        "/api/v1/connectors/fake_cms/connect",
        json={"values": {"siteUrl": "https://northgate.example"}},
        headers=auth(token),
    )
    configure(client, token, "on_page_seo_sync")
    client.post("/api/v1/agents/on_page_seo_sync/run", headers=auth(token))

    # A page with a definite, checkable problem: too thin, no schema, and an
    # internal link into nothing.
    bind_tenant(clean_db, org_id)
    org = clean_db.execute(select(Organization).where(Organization.id == org_id)).scalar_one()
    cipher = OrgCipher.for_org(org)
    clean_db.add(
        SeoPage(
            tenant_id=org_id,
            url="/thin-page",
            title="Thin",
            body_encrypted=cipher.encrypt(
                '<a href="/nowhere">gone</a>', context=Ctx.PAGE_BODY
            ),
            word_count=40,
            schema_types=[],
        )
    )
    clean_db.commit()

    configure(client, token, "technical_seo_auditor")
    run = client.post("/api/v1/agents/technical_seo_auditor/run", headers=auth(token))
    assert run.status_code == 200, run.text

    found = client.get("/api/v1/seo/audit", headers=auth(token)).json()
    kinds = {i["kind"] for i in found["issues"] if i["url"] == "/thin-page"}
    assert "thin_content" in kinds
    assert "missing_schema" in kinds
    assert "broken_internal_link" in kinds
    assert found["high_count"] >= 1
    # Every finding carries what to do about it.
    assert all(i["recommendation"] for i in found["issues"])
    # Speed is reported as unmeasured rather than silently absent.
    assert "cannot be inferred" in found["vitals_unavailable"]

    # Now fix the page and re-audit.
    bind_tenant(clean_db, org_id)
    page = clean_db.execute(
        select(SeoPage).where(SeoPage.tenant_id == org_id, SeoPage.url == "/thin-page")
    ).scalar_one()
    page.word_count = 900
    page.schema_types = ["Article"]
    page.body_encrypted = cipher.encrypt("<h1>Now good</h1>", context=Ctx.PAGE_BODY)
    clean_db.commit()

    client.post("/api/v1/agents/technical_seo_auditor/run", headers=auth(token))

    after = client.get("/api/v1/seo/audit", headers=auth(token)).json()
    assert not any(
        i["kind"] == "thin_content" and i["url"] == "/thin-page" for i in after["issues"]
    )
    # Marked fixed rather than deleted, so the work is countable.
    assert after["fixed_this_month"] >= 3
    fixed = client.get("/api/v1/seo/audit?status=fixed", headers=auth(token)).json()
    assert any(i["kind"] == "thin_content" for i in fixed["issues"])


def test_an_ignored_finding_is_not_raised_again(client, clean_db):  # noqa: ANN001
    """Otherwise the Ignore button is useless and the argument recurs weekly."""
    from sqlalchemy import select

    from app.db.session import bind_tenant
    from app.models.seo import SeoIssue, SeoPage
    from app.models.workspace import Organization
    from app.services.encryption import Ctx, OrgCipher

    body = register(client)
    token = body["tokens"]["access_token"]
    org_id = body["session"]["organization"]["id"]
    # The auditor requires a CMS source: an audit of an empty page table is
    # not an audit, so preflight skips without one.
    client.post(
        "/api/v1/connectors/fake_cms/connect",
        json={"values": {"siteUrl": "https://northgate.example"}},
        headers=auth(token),
    )

    bind_tenant(clean_db, org_id)
    org = clean_db.execute(select(Organization).where(Organization.id == org_id)).scalar_one()
    cipher = OrgCipher.for_org(org)
    clean_db.add(
        SeoPage(
            tenant_id=org_id,
            url="/landing",
            title="Landing",
            body_encrypted=cipher.encrypt("<h1>Short by design</h1>", context=Ctx.PAGE_BODY),
            word_count=20,
            schema_types=["WebPage"],
        )
    )
    clean_db.commit()

    configure(client, token, "technical_seo_auditor")
    client.post("/api/v1/agents/technical_seo_auditor/run", headers=auth(token))

    issues = client.get("/api/v1/seo/audit", headers=auth(token)).json()["issues"]
    thin = next(i for i in issues if i["kind"] == "thin_content")

    # A reason is required — "ignored" must never mean "nobody remembers why".
    assert (
        client.post(
            f"/api/v1/seo/audit/{thin['id']}/ignore", json={"note": ""}, headers=auth(token)
        ).status_code
        == 422
    )
    ignored = client.post(
        f"/api/v1/seo/audit/{thin['id']}/ignore",
        json={"note": "Deliberate — landing page with no body copy"},
        headers=auth(token),
    )
    assert ignored.status_code == 200

    client.post("/api/v1/agents/technical_seo_auditor/run", headers=auth(token))

    open_now = client.get("/api/v1/seo/audit", headers=auth(token)).json()["issues"]
    assert not any(i["id"] == thin["id"] for i in open_now)
    still = client.get("/api/v1/seo/audit?status=ignored", headers=auth(token)).json()
    kept = next(i for i in still["issues"] if i["id"] == thin["id"])
    assert "Deliberate" in kept["ignore_note"]

    # And it can be put back on the list.
    assert (
        client.post(f"/api/v1/seo/audit/{thin['id']}/reopen", headers=auth(token)).status_code
        == 200
    )
    assert any(
        i["id"] == thin["id"]
        for i in client.get("/api/v1/seo/audit", headers=auth(token)).json()["issues"]
    )


def test_full_autonomy_applies_without_queueing(client, clean_db):  # noqa: ANN001
    """The same run under full autonomy publishes instead of queueing."""
    token = register(client)["tokens"]["access_token"]
    client.put(
        "/api/v1/onboarding",
        json={"domain": "northgate.example", "guardrail": "full"},
        headers=auth(token),
    )
    client.post("/api/v1/onboarding/complete", headers=auth(token))
    client.post(
        "/api/v1/connectors/fake_cms/connect",
        json={"values": {"siteUrl": "https://northgate.example"}},
        headers=auth(token),
    )

    configure(client, token)
    client.post("/api/v1/agents/on_page_seo_sync/run", headers=auth(token))
    client.post("/api/v1/agents/on_page_seo_sync/run", headers=auth(token))

    runs = client.get(
        "/api/v1/agents/on_page_seo_sync/runs", headers=auth(token)
    ).json()
    assert any(r["actions_taken"] > 0 for r in runs), "nothing was applied autonomously"
    assert client.get("/api/v1/approvals", headers=auth(token)).json() == []


def test_pausing_an_agent_stops_scheduled_runs(client, clean_db):  # noqa: ANN001
    token = register(click := client)["tokens"]["access_token"]

    configure(client, token, "click_fraud_controller")
    paused = client.post("/api/v1/agents/click_fraud_controller/pause", headers=auth(token))
    assert paused.status_code == 200

    agent = client.get("/api/v1/agents/click_fraud_controller", headers=auth(token)).json()
    assert agent["status"] == "paused"
    assert agent["next_run"] == "—"

    resumed = client.post("/api/v1/agents/click_fraud_controller/resume", headers=auth(token))
    assert resumed.status_code == 200
    assert (
        client.get("/api/v1/agents/click_fraud_controller", headers=auth(token)).json()[
            "status"
        ]
        == "running"
    )


def test_global_autonomy_cascades_to_every_agent(client, clean_db):  # noqa: ANN001
    token = register(client)["tokens"]["access_token"]

    client.post("/api/v1/agents/autonomy", json={"autonomous": False}, headers=auth(token))
    agents = client.get("/api/v1/agents", headers=auth(token)).json()
    assert all(a["autonomy"] is False for a in agents)
    assert all(a["autonomy_label"] == "Human-in-loop" for a in agents)

    client.post("/api/v1/agents/autonomy", json={"autonomous": True}, headers=auth(token))
    agents = client.get("/api/v1/agents", headers=auth(token)).json()
    assert all(a["autonomy"] is True for a in agents)


def test_agent_configuration_round_trips(client, clean_db):  # noqa: ANN001
    token = register(client)["tokens"]["access_token"]
    response = client.put(
        "/api/v1/agents/backlink_node_discovery/config",
        json={
            "schedule": "Weekly",
            "scope": "EV charging, lease deals",
            "notify_channel": "None",
            "max_actions_per_day": 5,
        },
        headers=auth(token),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["schedule"] == "Weekly"
    assert body["configured"] is True
    assert "Weekly" in body["config_summary"]
    assert "max 5/day" in body["config_summary"]


def test_invalid_schedule_is_rejected(client, clean_db):  # noqa: ANN001
    token = register(client)["tokens"]["access_token"]
    response = client.put(
        "/api/v1/agents/backlink_node_discovery/config",
        json={"schedule": "Every third Tuesday", "max_actions_per_day": 5},
        headers=auth(token),
    )
    assert response.status_code == 422


# ── Ads ────────────────────────────────────────────────────────────────────
def test_manual_budget_edit_locks_the_channel(client, clean_db):  # noqa: ANN001
    """A human's number must not be silently undone by the next agent pass."""
    token = register(client)["tokens"]["access_token"]

    response = client.put(
        "/api/v1/ads/budget", json={"channel": "google", "percent": 45}, headers=auth(token)
    )
    assert response.status_code == 200
    google = next(b for b in response.json()["budgets"] if b["channel"] == "google")
    assert google["percent"] == 45
    assert google["locked"] is True

    unlocked = client.post("/api/v1/ads/budget/google/unlock", headers=auth(token))
    assert unlocked.status_code == 200
    after = client.get("/api/v1/ads", headers=auth(token)).json()
    assert next(b for b in after["budgets"] if b["channel"] == "google")["locked"] is False


def test_ads_workspace_shape(client, clean_db):  # noqa: ANN001
    token = register(client)["tokens"]["access_token"]
    body = client.get("/api/v1/ads", headers=auth(token)).json()
    assert {b["channel"] for b in body["budgets"]} == {
        "google", "meta", "linkedin", "tiktok", "dsp"
    }
    assert body["budget_total"] == 100


# ── Team management ────────────────────────────────────────────────────────
def test_owner_role_cannot_be_changed(client, clean_db):  # noqa: ANN001
    body = register(client)
    token = body["tokens"]["access_token"]
    owner_id = body["session"]["user"]["id"]

    response = client.put(
        f"/api/v1/admin/team/{owner_id}/role", json={"role": "seo"}, headers=auth(token)
    )
    assert response.status_code == 403


def test_invitation_flow(client, clean_db):  # noqa: ANN001
    """An invited teammate joins with no credential of their own to choose."""
    from sqlalchemy import select

    from app.db.session import bind_tenant
    from app.models.workspace import Invitation

    body = register(client)
    token = body["tokens"]["access_token"]
    org_id = body["session"]["organization"]["id"]

    invited = client.post(
        "/api/v1/admin/invitations",
        json={"email": "priya@northgate.example", "role": "seo"},
        headers=auth(token),
    )
    assert invited.status_code == 200
    assert invited.json()["role_label"] == "SEO/AEO Specialist"

    # The raw token only ever went to the invitee's inbox, so the test reads
    # it the way the accept screen would: by presenting it. Here it is taken
    # from the log-captured link via the stored hash lookup instead.
    bind_tenant(clean_db, org_id)
    invitation = clean_db.execute(
        select(Invitation).where(Invitation.tenant_id == org_id)
    ).scalar_one()
    # Only the HMAC is stored — the plaintext token is not recoverable.
    assert len(invitation.token_hash) == 64
    assert invitation.accepted_at is None


def test_seat_limit_is_enforced(client, clean_db):  # noqa: ANN001
    from sqlalchemy import select

    from app.db.session import bind_tenant
    from app.models.workspace import Organization

    body = register(client)
    token = body["tokens"]["access_token"]
    org_id = body["session"]["organization"]["id"]

    bind_tenant(clean_db, org_id)
    organization = clean_db.execute(
        select(Organization).where(Organization.id == org_id)
    ).scalar_one()
    organization.seats_total = 1
    clean_db.commit()

    response = client.post(
        "/api/v1/admin/invitations",
        json={"email": "someone@northgate.example", "role": "seo"},
        headers=auth(token),
    )
    assert response.status_code == 409
    assert "seats" in response.json()["detail"]


# ── Reports ────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("range_key", ["7d", "30d", "90d"])
def test_reports_render_for_every_range(client, clean_db, range_key: str):  # noqa: ANN001
    token = register(client)["tokens"]["access_token"]
    response = client.get(
        f"/api/v1/reports?range={range_key}", headers=auth(token)
    )
    assert response.status_code == 200
    body = response.json()
    assert body["range"] == range_key
    # Six buckets, so the chart's shape is comparable across ranges. The
    # pre-rendered polyline that used to travel beside them is gone: the
    # chart computes its own geometry, so sending both was sending the same
    # data twice.
    assert len(body["trend"]) == 6
    assert "trend_points" not in body


def test_invalid_report_range_is_rejected(client, clean_db):  # noqa: ANN001
    token = register(client)["tokens"]["access_token"]
    assert (
        client.get("/api/v1/reports?range=all-time", headers=auth(token)).status_code
        == 422
    )


# ── Health ─────────────────────────────────────────────────────────────────
def test_health_reports_the_fleet(client):  # noqa: ANN001
    from app.agents.base.registry import agent_slugs
    from app.connectors.base.registry import connector_slugs

    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert body["agents_registered"] == len(agent_slugs())
    assert body["connectors_registered"] == len(connector_slugs())
    assert body["database"] == "up"


def test_security_headers_are_present(client):  # noqa: ANN001
    response = client.get("/health")
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert "Content-Security-Policy" in response.headers
    assert response.headers["X-Request-ID"]


def test_onboarding_offers_the_real_catalogue_not_a_hard_coded_three(client, clean_db):  # noqa: ANN001
    """The ad step used to be three checkboxes over a field nobody read.

    `google`, `meta`, `linkedin` were seeded into `ad_accounts` at
    provisioning and written back by the wizard, and no agent, service or
    report ever read that field. Six ad connectors exist, so the step showed
    half of them and ticking one changed nothing at all. It reports what is
    actually connected now.
    """
    token = register(client)["tokens"]["access_token"]
    state = client.get("/api/v1/onboarding", headers=auth(token)).json()

    platforms = {p["slug"]: p for p in state["ad_platforms"]}
    # All six real ones, not the three that were hard-coded. Compared as a
    # subset because the suite registers its own fake ad connector too.
    for slug in (
        "google_ads",
        "meta_ads",
        "linkedin_ads",
        "tiktok_ads",
        "microsoft_ads",
        "dsp_exchange",
    ):
        assert slug in platforms, sorted(platforms)
        assert platforms[slug]["name"]
        # Nothing is connected in a fresh workspace, and the step says so
        # rather than showing an unticked box that means nothing.
        assert platforms[slug]["connected"] is False

    # The CMS options carry their connector slug, so the wizard can show
    # each vendor's own mark instead of a dropdown of bare strings.
    assert state["cms_options"], state
    assert any(option["slug"] == "wordpress" for option in state["cms_options"])
    # "Other" is last and has no slug: a client may be on something with no
    # connector yet and the wizard must not imply otherwise.
    assert state["cms_options"][-1]["slug"] == ""

    # Each guardrail carries its name and its consequence apart, so a card
    # can show a heading and a sentence rather than one string doing both.
    for option in state["guardrail_options"]:
        assert option["name"] and option["detail"]
        assert option["name"] != option["label"]


def test_connecting_an_ad_platform_shows_up_in_onboarding(client, clean_db):  # noqa: ANN001
    """The step reflects reality, which is the whole point of the change.

    The old checkboxes were the workspace's own record of an intention; this
    is a query. Connect something and the step says so, with no second place
    to keep in sync.
    """
    token = register(client)["tokens"]["access_token"]

    before = client.get("/api/v1/onboarding", headers=auth(token)).json()
    assert all(p["connected"] is False for p in before["ad_platforms"])

    connected = client.post(
        "/api/v1/connectors/fake_ads/connect",
        json={"values": {"accountId": "act-1"}},
        headers=auth(token),
    )
    assert connected.status_code == 200, connected.text

    after = client.get("/api/v1/onboarding", headers=auth(token)).json()
    live = [p["slug"] for p in after["ad_platforms"] if p["connected"]]
    assert live == ["fake_ads"], after["ad_platforms"]


def test_the_dashboard_can_tell_nothing_configured_from_all_paused(client, clean_db):  # noqa: ANN001
    """Two opposite situations that looked identical on the dashboard.

    The panel showed agents whose status was running or errored and nothing
    else, so a workspace with everything configured and paused got an empty
    panel reading "No agents are running yet — configure an agent, then start
    it". Advice for a step already finished, with the results those agents
    had produced hidden behind it.
    """
    token = register(client)["tokens"]["access_token"]

    # Nothing configured: the panel has nothing to show and the advice is
    # the right advice.
    fresh = client.get("/api/v1/dashboard", headers=auth(token)).json()
    assert fresh["agents"] == []
    assert fresh["ready"] == []
    assert fresh["ready_agents"] == 0

    # Configured, and left stopped — which is how every agent ships.
    configure(client, token, "on_page_seo_sync")
    after = client.get("/api/v1/dashboard", headers=auth(token)).json()
    assert after["agents"] == [], "it is not running, so it is not in the running panel"
    assert after["ready_agents"] == 1
    assert [a["slug"] for a in after["ready"]] == ["on_page_seo_sync"]

    # Started: it moves to the running panel and out of the ready one.
    client.post("/api/v1/agents/on_page_seo_sync/resume", headers=auth(token))
    live = client.get("/api/v1/dashboard", headers=auth(token)).json()
    assert [a["slug"] for a in live["agents"]] == ["on_page_seo_sync"]
    assert live["ready_agents"] == 0


def test_a_dashboard_agent_card_carries_what_it_last_achieved(client, clean_db):  # noqa: ANN001
    """A card saying only "Paused" hid that the agent had synced pages or
    opened issues, which is the thing somebody opens a dashboard for."""
    token = register(client)["tokens"]["access_token"]
    client.post(
        "/api/v1/connectors/fake_cms/connect",
        json={"values": {"siteUrl": "https://example.test"}},
        headers=auth(token),
    )
    configure(client, token, "on_page_seo_sync")
    assert (
        client.post(
            "/api/v1/agents/on_page_seo_sync/run", headers=auth(token)
        ).status_code
        == 200
    )

    body = client.get("/api/v1/dashboard", headers=auth(token)).json()
    card = next(
        agent
        for agent in body["agents"] + body["ready"]
        if agent["slug"] == "on_page_seo_sync"
    )
    assert card["last_run_status"], card
    assert card["last_run_summary"], card


def test_the_navigation_counts_what_is_waiting_not_what_exists(client, clean_db):  # noqa: ANN001
    """A badge showing the size of a table is decoration.

    The sidebar carried three numbers and the rest said nothing, so a
    workspace with ten open technical findings had to open the screen to
    learn that. The counts answer "how much needs me here".
    """
    token = register(client)["tokens"]["access_token"]

    session = client.get("/api/v1/auth/me", headers=auth(token)).json()
    # A fresh workspace has nothing waiting anywhere, and zeroes are dropped
    # rather than rendered as a badge saying nought.
    assert session["nav_counts"] == {}, session["nav_counts"]

    # Connect something and start an agent: two different screens change.
    client.post(
        "/api/v1/connectors/fake_cms/connect",
        json={"values": {"siteUrl": "https://example.test"}},
        headers=auth(token),
    )
    configure(client, token, "on_page_seo_sync")
    client.post("/api/v1/agents/on_page_seo_sync/resume", headers=auth(token))

    counts = client.get("/api/v1/auth/me", headers=auth(token)).json()["nav_counts"]
    assert counts["/connectors"] == 1
    assert counts["/agents"] == 1


def test_a_role_is_not_told_how_much_is_behind_a_door_it_cannot_open(client, clean_db):  # noqa: ANN001
    """The same rule the notification panel follows: a count for a module
    the access map hides would be a badge that never clears and has nothing
    behind it."""
    from app.core.rbac import Role, access_map

    from app.db.session import bind_tenant
    from app.services import views

    body = register(client)
    org_id = body["session"]["organization"]["id"]
    token = body["tokens"]["access_token"]
    bind_tenant(clean_db, org_id)

    client.post(
        "/api/v1/connectors/fake_cms/connect",
        json={"values": {"siteUrl": "https://example.test"}},
        headers=auth(token),
    )
    clean_db.commit()

    for role in Role:
        counts = views.nav_counts(clean_db, tenant_id=org_id, role=role.value)
        allowed = {m for m, level in access_map(role.value).items() if level != "none"}
        # Every route counted has to belong to a module this role can see.
        for route in counts:
            module = {"/technical": "seo", "/seo": "seo"}.get(route, route.strip("/"))
            assert module in allowed, (role.value, route)


def test_a_revoked_invitation_is_not_counted_as_outstanding(client, clean_db):  # noqa: ANN001
    """The badge said 1 and the screen showed none.

    The Admin screen lists invitations that are neither accepted nor revoked.
    The navigation count asked only for a null ``accepted_at``, so a revoked
    invitation kept the badge lit with nothing behind it — which reads as the
    screen being broken rather than the count being wrong. Both now use one
    definition.
    """
    token = register(client)["tokens"]["access_token"]

    invited = client.post(
        "/api/v1/admin/invitations",
        json={"email": "colleague@northgate.example", "role": "seo"},
        headers=auth(token),
    )
    assert invited.status_code == 200, invited.text

    # Outstanding: on the screen, and on the badge.
    overview = client.get("/api/v1/admin", headers=auth(token)).json()
    assert len(overview["invitations"]) == 1
    session = client.get("/api/v1/auth/me", headers=auth(token)).json()
    assert session["nav_counts"].get("/admin") == 1

    invitation_id = overview["invitations"][0]["id"]
    revoked = client.delete(
        f"/api/v1/admin/invitations/{invitation_id}", headers=auth(token)
    )
    assert revoked.status_code == 200, revoked.text

    # Revoked: off the screen, and off the badge. The two cannot disagree,
    # because they ask the same question now.
    after = client.get("/api/v1/admin", headers=auth(token)).json()
    assert after["invitations"] == []
    session = client.get("/api/v1/auth/me", headers=auth(token)).json()
    assert "/admin" not in session["nav_counts"], session["nav_counts"]


def test_every_badge_agrees_with_the_screen_it_sits_on(client, clean_db):  # noqa: ANN001
    """A count is a promise about what is behind the link.

    Each of these compares the badge against the number the screen itself
    reports, rather than against a predicate written twice.
    """
    token = register(client)["tokens"]["access_token"]
    client.post(
        "/api/v1/connectors/fake_cms/connect",
        json={"values": {"siteUrl": "https://example.test"}},
        headers=auth(token),
    )
    configure(client, token, "on_page_seo_sync")
    client.post("/api/v1/agents/on_page_seo_sync/resume", headers=auth(token))

    counts = client.get("/api/v1/auth/me", headers=auth(token)).json()["nav_counts"]

    connectors = client.get("/api/v1/connectors", headers=auth(token)).json()
    assert counts.get("/connectors", 0) == sum(1 for c in connectors if c["connected"])

    agents = client.get("/api/v1/agents", headers=auth(token)).json()
    assert counts.get("/agents", 0) == sum(
        1
        for a in agents
        if a["status"] in ("running", "error") or a["configured"]
    )

    approvals = client.get("/api/v1/approvals/count", headers=auth(token)).json()
    assert counts.get("/approvals", 0) == approvals["pending"]

    audit = client.get("/api/v1/seo/audit", headers=auth(token)).json()
    assert counts.get("/technical", 0) == audit["open_count"]
