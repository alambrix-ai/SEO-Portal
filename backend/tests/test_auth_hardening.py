"""Auth hardening: no account enumeration, audited sign-in events, re-invites,
signing out of every workspace, and an address-wide lockout.

The unit tests at the top run without a database: they drive the service
functions with small fakes. The ``requires_db`` tests at the bottom run the
same rules end to end through the API and skip without ``TEST_DATABASE_URL``.
"""
from __future__ import annotations

import logging
import re
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from app.core.exceptions import ConflictError
from app.db.base import utcnow
from app.models.identity import CodePurpose
from app.models.workspace import Invitation, User
from app.services import auth as auth_service
from tests.conftest import requires_db


@pytest.fixture(autouse=True)
def _forget_sign_in_sources():  # noqa: ANN202
    """Per-network wrong-code counts live in process memory; start each test clean."""
    auth_service.reset_source_failures()
    yield
    auth_service.reset_source_failures()


# ── Fakes ──────────────────────────────────────────────────────────────────
class _FakeSession:
    """Just enough of a Session for code paths that only add and flush."""

    def __init__(self) -> None:
        self.added: list[Any] = []
        self.flushes = 0
        self.commits = 0

    def add(self, obj: Any) -> None:
        self.added.append(obj)

    def flush(self) -> None:
        self.flushes += 1

    def commit(self) -> None:
        self.commits += 1


def _user(**overrides: Any) -> User:
    fields: dict[str, Any] = {
        "id": "user-1",
        "tenant_id": "org-1",
        "name": "Priya Shah",
        "email": "priya@northgate.example",
        "email_index": "idx-priya",
        "role": "seo",
        "is_owner": False,
        "is_active": True,
        "failed_login_count": 0,
        "locked_until": None,
        "email_verified": True,
    }
    fields.update(overrides)
    return User(**fields)


def _invitation(**overrides: Any) -> Invitation:
    fields: dict[str, Any] = {
        "tenant_id": "org-1",
        "email": "priya@northgate.example",
        "email_index": "idx-priya",
        "role": "manager",
    }
    fields.update(overrides)
    return Invitation(**fields)


# ── Re-invite: the branch logic ────────────────────────────────────────────
def test_a_first_invitation_makes_a_new_seat() -> None:
    user, is_new = auth_service.seat_for_invitation(
        None, invitation=_invitation(), typed_name="  Priya  ", fallback_name=""
    )
    assert is_new is True
    assert user.tenant_id == "org-1"
    assert user.email_index == "idx-priya"
    assert user.role == "manager"
    assert user.name == "Priya"
    assert user.email_verified is True


def test_a_new_seat_falls_back_to_another_seats_name_then_the_mailbox() -> None:
    named, _ = auth_service.seat_for_invitation(
        None, invitation=_invitation(), typed_name="", fallback_name="Priya S"
    )
    assert named.name == "Priya S"
    bare, _ = auth_service.seat_for_invitation(
        None, invitation=_invitation(), typed_name="", fallback_name=""
    )
    assert bare.name == "priya"


def test_reinviting_a_deactivated_member_reuses_their_row() -> None:
    """The bug: a second users row for (tenant, email) hit uq_users_tenant_email."""
    old = _user(
        is_active=False,
        role="client",
        is_owner=True,
        failed_login_count=5,
        locked_until=utcnow() + timedelta(minutes=5),
    )
    user, is_new = auth_service.seat_for_invitation(
        old, invitation=_invitation(role="manager"), typed_name="", fallback_name="x"
    )
    assert is_new is False
    assert user is old
    assert user.is_active is True
    # The invitation decides the role, and never hands back ownership.
    assert user.role == "manager"
    assert user.is_owner is False
    assert user.failed_login_count == 0
    assert user.locked_until is None
    # No name typed: the seat keeps the name it had.
    assert user.name == "Priya Shah"


def test_a_typed_name_replaces_the_old_one_on_rejoining() -> None:
    old = _user(is_active=False)
    user, _ = auth_service.seat_for_invitation(
        old, invitation=_invitation(), typed_name="Priya Kapoor", fallback_name=""
    )
    assert user.name == "Priya Kapoor"


def test_an_invitation_cannot_take_over_an_active_seat() -> None:
    active = _user(role="seo")
    with pytest.raises(ConflictError):
        auth_service.seat_for_invitation(
            active, invitation=_invitation(role="admin"), typed_name="", fallback_name=""
        )
    # Nothing about the live seat changed.
    assert active.role == "seo"


# ── Request-code: one answer for everyone ──────────────────────────────────
def _patch_code_request(
    monkeypatch: pytest.MonkeyPatch, user: User | None
) -> list[tuple[str, dict]]:
    """Make request_sign_in_code run with no database; record mail sends."""
    sent: list[tuple[str, dict]] = []
    monkeypatch.setattr(auth_service, "find_user_by_email", lambda db, address: user)
    monkeypatch.setattr(auth_service, "_last_request", lambda db, index, purpose: None)
    monkeypatch.setattr(
        auth_service.email_service,
        "send_login_code",
        lambda **mail: sent.append(("sign_in", mail)) or True,
    )
    return sent


@pytest.mark.parametrize(
    "who",
    ["unknown", "deactivated"],
)
def test_request_code_response_is_byte_identical(
    monkeypatch: pytest.MonkeyPatch, who: str
) -> None:
    """The route's whole answer, serialised, does not depend on the account."""
    from fastapi import BackgroundTasks

    from app.api.v1.auth import SIGN_IN_CODE_REQUESTED, request_code
    from app.schemas.auth import RequestCodeRequest

    request = SimpleNamespace(headers={"user-agent": "pytest"})
    payload = RequestCodeRequest(email="priya@northgate.example")

    _patch_code_request(monkeypatch, _user())
    real_tasks = BackgroundTasks()
    real = request_code(payload, _FakeSession(), request, "203.0.113.9", real_tasks)

    other = None if who == "unknown" else _user(is_active=False)
    _patch_code_request(monkeypatch, other)
    other_tasks = BackgroundTasks()
    fake = request_code(payload, _FakeSession(), request, "203.0.113.9", other_tasks)

    assert real.model_dump_json().encode() == fake.model_dump_json().encode()
    assert real.detail == SIGN_IN_CODE_REQUESTED
    # Only the real account has a mail queued, and it is queued, not sent
    # inline — inline delivery would make the response slower for real
    # accounts and so give the answer away by timing.
    assert len(real_tasks.tasks) == 1
    assert other_tasks.tasks == []


def test_unknown_address_gets_a_burned_row_and_no_mail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent = _patch_code_request(monkeypatch, None)
    db = _FakeSession()
    scheduled: list[tuple] = []

    challenge = auth_service.request_sign_in_code(
        db,
        email="Nobody@Example.com",
        schedule_mail=lambda *args: scheduled.append(args),
    )

    assert sent == [] and scheduled == []
    (row,) = db.added
    # Burned at birth: it holds the throttle arithmetic, and can never be redeemed.
    assert row.burned_at is not None
    assert row.purpose == CodePurpose.SIGN_IN.value
    assert challenge.expires_in > 0


def test_a_real_account_mail_is_sent_by_the_scheduled_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent = _patch_code_request(monkeypatch, _user())
    scheduled: list[tuple] = []

    auth_service.request_sign_in_code(
        _FakeSession(),
        email="priya@northgate.example",
        schedule_mail=lambda *args: scheduled.append(args),
    )
    assert sent == []  # not inline
    (task,) = scheduled
    task[0](*task[1:])
    assert len(sent) == 1
    assert sent[0][1]["to"] == "priya@northgate.example"


def test_a_failed_deferred_mail_is_logged_without_address_or_code(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def broken(**_mail: object) -> bool:
        raise RuntimeError("could not deliver to priya@northgate.example, code: 482913")

    with caplog.at_level(logging.WARNING, logger=auth_service.log.name):
        auth_service._deliver_code_quietly(
            broken, "sign_in", {"to": "priya@northgate.example", "code": "482913"}
        )

    text = caplog.text
    assert "Could not send a code email" in text
    assert "priya@northgate.example" not in text
    assert "482913" not in text


def test_an_invalid_address_is_still_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core.exceptions import InvalidInputError

    _patch_code_request(monkeypatch, None)
    with pytest.raises(InvalidInputError):
        auth_service.request_sign_in_code(_FakeSession(), email="not-an-address")


# ── Failed codes and the address-wide lockout ──────────────────────────────
class _Scalar:
    def __init__(self, value: Any) -> None:
        self.value = value

    def scalar_one(self) -> Any:
        assert self.value is not None
        return self.value

    def scalar_one_or_none(self) -> Any:
        return self.value


class _SeatSession(_FakeSession):
    """Serves users rows by id, as ``db.get(User, id)`` would.

    Also answers the atomic ``UPDATE users SET failed_login_count =
    failed_login_count + 1 … RETURNING`` the way PostgreSQL would, and keeps
    every statement so a test can inspect the SQL.
    """

    def __init__(self, seats: list[User]) -> None:
        super().__init__()
        self.seats = {seat.id: seat for seat in seats}
        self.db_counts = {seat.id: seat.failed_login_count for seat in seats}
        self.statements: list[Any] = []

    def get(self, model: type, key: str) -> Any:
        assert model is User
        return self.seats.get(key)

    def execute(self, stmt: Any) -> _Scalar:
        self.statements.append(stmt)
        params = stmt.compile().params
        (seat_id,) = [v for v in params.values() if v in self.db_counts]
        self.db_counts[seat_id] += 1
        return _Scalar(self.db_counts[seat_id])


def _patch_lock_world(
    monkeypatch: pytest.MonkeyPatch, seats: list[User]
) -> tuple[list[dict], list[str | None]]:
    audits: list[dict] = []
    pins: list[str | None] = []
    memberships = [SimpleNamespace(user_id=s.id, tenant_id=s.tenant_id) for s in seats]
    monkeypatch.setattr(
        auth_service, "list_memberships", lambda db, email_index_value: memberships
    )
    monkeypatch.setattr(auth_service, "bind_tenant", lambda db, tenant: pins.append(tenant))
    monkeypatch.setattr(
        auth_service.audit, "record", lambda db, **entry: audits.append(entry)
    )
    return audits, pins


def test_a_wrong_code_is_audited_without_the_code(monkeypatch: pytest.MonkeyPatch) -> None:
    user = _user()
    audits, _ = _patch_lock_world(monkeypatch, [user])

    auth_service._note_failed_attempt(_SeatSession([user]), user, ip_address="203.0.113.9")

    assert user.failed_login_count == 1
    (entry,) = audits
    assert entry["tenant_id"] == "org-1"
    assert entry["actor_id"] == "user-1"
    assert entry["module"] == auth_service.AUTH_AUDIT_MODULE
    assert entry["context"]["event"] == "sign_in_failed"
    assert entry["ip_address"] == "203.0.113.9"
    # Nothing that looks like a code is recorded.
    assert not re.search(r"\b\d{6}\b", repr(entry))


def test_lockout_covers_every_seat_behind_the_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Locking one seat would let guessing continue against the next."""
    from app.core.config import settings

    here = _user(failed_login_count=settings.max_failed_logins - 1)
    elsewhere = _user(id="user-2", tenant_id="org-2")
    audits, pins = _patch_lock_world(monkeypatch, [here, elsewhere])

    auth_service._note_failed_attempt(_SeatSession([here, elsewhere]), here)

    assert here.locked_until is not None and here.locked_until > utcnow()
    assert elsewhere.locked_until == here.locked_until
    assert here.failed_login_count == elsewhere.failed_login_count == 0
    lock_rows = [a for a in audits if a["context"].get("event") == "sign_in_locked"]
    # One lock row in each workspace's own audit log.
    assert sorted(a["tenant_id"] for a in lock_rows) == ["org-1", "org-2"]
    # The caller's tenant pin is restored afterwards.
    assert pins[-1] == "org-1"


def test_a_paused_seat_reports_when_the_pause_began() -> None:
    """The pause start is what decides which codes are fresh enough to try once."""
    from app.core.config import settings

    until = utcnow() + timedelta(minutes=10)
    paused = _user(locked_until=until)
    assert auth_service._address_lock_started(paused) == until - timedelta(
        minutes=settings.lockout_minutes
    )
    assert auth_service._address_lock_started(_user()) is None
    expired = _user(locked_until=utcnow() - timedelta(seconds=1))
    assert auth_service._address_lock_started(expired) is None


def test_the_seat_that_tripped_the_lock_is_locked_without_a_membership_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_set_address_lock used to reach seats only through memberships."""
    here = _user()
    elsewhere = _user(id="user-2", tenant_id="org-2")
    audits, pins = _patch_lock_world(monkeypatch, [elsewhere])  # no row for ``here``
    until = utcnow() + timedelta(minutes=15)

    changed = auth_service._set_address_lock(
        _SeatSession([here, elsewhere]), here, until
    )

    assert changed == 2
    assert here.locked_until == until and elsewhere.locked_until == until
    assert sorted(a["tenant_id"] for a in audits) == ["org-1", "org-2"]
    assert pins[-1] == "org-1"


def test_lifting_the_lock_clears_every_seat(monkeypatch: pytest.MonkeyPatch) -> None:
    stale = utcnow() - timedelta(minutes=1)
    here = _user(locked_until=stale)
    elsewhere = _user(id="user-2", tenant_id="org-2", locked_until=stale)
    audits, pins = _patch_lock_world(monkeypatch, [here, elsewhere])

    changed = auth_service._set_address_lock(_SeatSession([here, elsewhere]), here, None)

    assert changed == 2
    assert here.locked_until is None and elsewhere.locked_until is None
    assert audits == []  # lifting is not a security event
    assert pins[-1] == "org-1"


# ── Database-backed: end to end ────────────────────────────────────────────
def _audit_rows(db: Any, tenant_id: str, event: str) -> list:
    from sqlalchemy import select

    from app.db.session import bind_tenant
    from app.models.workspace import AuditLogEntry

    bind_tenant(db, tenant_id)
    db.expire_all()
    rows = db.execute(
        select(AuditLogEntry).where(AuditLogEntry.tenant_id == tenant_id)
    ).scalars()
    return [r for r in rows if (r.context or {}).get("event") == event]


def _invite_token(mailbox: Any, address: str) -> str:
    match = re.search(r"[?&]token=([A-Za-z0-9_\-]+)", mailbox.latest(address).text)
    assert match, "No invitation link in the mail"
    return match.group(1)


@requires_db
def test_request_code_is_byte_identical_over_http(client, clean_db, mailbox):  # noqa: ANN001
    from tests.test_api_flows import register

    register(client)
    known = client.post("/api/v1/auth/request-code", json={"email": "owner@northgate.example"})
    unknown = client.post("/api/v1/auth/request-code", json={"email": "ghost@example.com"})

    assert known.status_code == unknown.status_code == 200
    assert known.content == unknown.content
    assert mailbox.count_for("ghost@example.com") == 0


@requires_db
def test_sign_in_events_are_audited(client, clean_db, mailbox):  # noqa: ANN001
    from app.core.config import settings
    from tests.test_api_flows import register, sign_in

    body = register(client)
    org_id = body["session"]["organization"]["id"]

    ok = sign_in(client)
    assert ok.status_code == 200
    assert _audit_rows(clean_db, org_id, "sign_in_succeeded")

    for _ in range(settings.max_failed_logins):
        client.post("/api/v1/auth/request-code", json={"email": "owner@northgate.example"})
        client.post(
            "/api/v1/auth/login",
            json={"email": "owner@northgate.example", "code": "000000"},
        )
    failed = _audit_rows(clean_db, org_id, "sign_in_failed")
    assert len(failed) >= settings.max_failed_logins
    assert _audit_rows(clean_db, org_id, "sign_in_locked")
    for row in failed:
        assert "000000" not in repr(row.context) and "000000" not in row.action


@requires_db
def test_refresh_token_reuse_is_audited(client, clean_db):  # noqa: ANN001
    from tests.test_api_flows import register, sign_in

    body = register(client)
    org_id = body["session"]["organization"]["id"]
    refresh_token = sign_in(client).json()["tokens"]["refresh_token"]

    assert client.post("/api/v1/auth/refresh", json={"refresh_token": refresh_token}).status_code == 200
    assert client.post("/api/v1/auth/refresh", json={"refresh_token": refresh_token}).status_code == 401

    (row,) = _audit_rows(clean_db, org_id, "refresh_token_reuse")
    assert refresh_token not in repr(row.context) and refresh_token not in row.action


@requires_db
def test_sign_out_everywhere_covers_every_workspace(client, clean_db):  # noqa: ANN001
    from sqlalchemy import select

    from app.models.workspace import RefreshToken
    from tests.test_api_flows import auth, register

    first = register(client, email="multi@northgate.example")
    second = client.post(
        "/api/v1/workspaces",
        json={"name": "Second Desk", "primary_domain": "second.example"},
        headers=auth(first["tokens"]["access_token"]),
    )
    assert second.status_code == 200, second.text
    second_refresh = second.json()["tokens"]["refresh_token"]

    out = client.post(
        "/api/v1/auth/sign-out-everywhere",
        headers=auth(second.json()["tokens"]["access_token"]),
    )
    assert out.status_code == 200

    # The first workspace's session is gone too, not just the caller's.
    first_refresh = first["tokens"]["refresh_token"]
    assert client.post("/api/v1/auth/refresh", json={"refresh_token": first_refresh}).status_code == 401
    assert client.post("/api/v1/auth/refresh", json={"refresh_token": second_refresh}).status_code == 401
    clean_db.expire_all()
    live = clean_db.execute(
        select(RefreshToken).where(RefreshToken.revoked_at.is_(None))
    ).scalars().all()
    assert live == []


@requires_db
def test_reinviting_a_deactivated_member_leaves_one_row(client, clean_db, mailbox):  # noqa: ANN001
    from sqlalchemy import func, select

    from app.core.crypto import email_index
    from app.db.session import bind_tenant
    from tests.test_api_flows import auth, register

    body = register(client)
    token = body["tokens"]["access_token"]
    org_id = body["session"]["organization"]["id"]
    invitee = "priya@northgate.example"

    def invite_and_accept(role: str) -> dict:
        invited = client.post(
            "/api/v1/admin/invitations",
            json={"email": invitee, "role": role},
            headers=auth(token),
        )
        assert invited.status_code == 200, invited.text
        accepted = client.post(
            "/api/v1/auth/accept-invitation",
            json={"token": _invite_token(mailbox, invitee), "full_name": "Priya Shah"},
        )
        assert accepted.status_code == 201, accepted.text
        return accepted.json()

    joined = invite_and_accept("seo")
    member_id = joined["session"]["user"]["id"]

    off = client.post(f"/api/v1/admin/team/{member_id}/deactivate", headers=auth(token))
    assert off.status_code == 200, off.text

    rejoined = invite_and_accept("manager")
    assert rejoined["session"]["user"]["id"] == member_id
    assert rejoined["session"]["user"]["role"] == "manager"

    bind_tenant(clean_db, org_id)
    rows = clean_db.execute(
        select(func.count())
        .select_from(User)
        .where(User.tenant_id == org_id, User.email_index == email_index(invitee))
    ).scalar_one()
    assert rows == 1


@requires_db
def test_an_active_member_cannot_accept_a_second_invitation(client, clean_db, mailbox):  # noqa: ANN001
    from tests.test_api_flows import auth, register

    token = register(client)["tokens"]["access_token"]
    invitee = "priya@northgate.example"
    client.post(
        "/api/v1/admin/invitations",
        json={"email": invitee, "role": "seo"},
        headers=auth(token),
    )
    stale_link = _invite_token(mailbox, invitee)
    first = client.post(
        "/api/v1/auth/accept-invitation",
        json={"token": stale_link, "full_name": "Priya Shah"},
    )
    assert first.status_code == 201
    # Re-inviting an active member is refused before a link is even sent.
    again = client.post(
        "/api/v1/admin/invitations",
        json={"email": invitee, "role": "admin"},
        headers=auth(token),
    )
    assert again.status_code == 409
