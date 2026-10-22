"""Review fixes for sign-in: atomic counters, one refusal message, a lockout
that cannot be used to shut the owner out, row-locked refresh rotation, and
audited session events.

The unit tests drive the service with small fakes and check the SQL that is
sent. The ``requires_db`` tests race real sessions against PostgreSQL and
skip without ``TEST_DATABASE_URL``.
"""
from __future__ import annotations

import threading
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.dialects import postgresql

from app.core.config import settings
from app.core.crypto import hash_token
from app.core.exceptions import AuthError
from app.db.base import utcnow
from app.models.identity import CodePurpose, LoginCode
from app.models.workspace import RefreshToken, User
from app.services import auth as auth_service
from app.services.auth import CODE_REJECTED_MESSAGE, CodeRejected
from tests.conftest import requires_db

RIGHT = "482913"
WRONG = "000000"


@pytest.fixture(autouse=True)
def _forget_sign_in_sources():  # noqa: ANN202
    auth_service.reset_source_failures()
    yield
    auth_service.reset_source_failures()


# ── Fakes ──────────────────────────────────────────────────────────────────
class _Result:
    def __init__(self, value: Any) -> None:
        self.value = value

    def scalar_one(self) -> Any:
        assert self.value is not None
        return self.value

    def scalar_one_or_none(self) -> Any:
        return self.value


class _ScriptedDb:
    """Returns the scripted values from ``execute`` in order and keeps the SQL."""

    def __init__(self, *returns: Any, objects: dict | None = None) -> None:
        self.returns = list(returns)
        self.statements: list[Any] = []
        self.commits = 0
        self.flushes = 0
        self.added: list[Any] = []
        self.objects = objects or {}

    def execute(self, stmt: Any) -> _Result:
        self.statements.append(stmt)
        return _Result(self.returns.pop(0) if self.returns else None)

    def commit(self) -> None:
        self.commits += 1

    def flush(self) -> None:
        self.flushes += 1

    def add(self, obj: Any) -> None:
        self.added.append(obj)

    def get(self, model: type, key: str) -> Any:
        return self.objects.get((model, key))

    def sql(self, index: int) -> str:
        return str(
            self.statements[index].compile(
                dialect=postgresql.dialect(), compile_kwargs={"literal_binds": False}
            )
        )


def _code(**overrides: Any) -> LoginCode:
    now = utcnow()
    fields: dict[str, Any] = {
        "id": "code-1",
        "email_index": "idx-priya",
        "purpose": CodePurpose.SIGN_IN.value,
        "code_hash": hash_token(RIGHT),
        "expires_at": now + timedelta(minutes=10),
        "attempts": 0,
        "consumed_at": None,
        "burned_at": None,
        "created_at": now,
    }
    fields.update(overrides)
    return LoginCode(**fields)


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


def _consume(monkeypatch: pytest.MonkeyPatch, db: _ScriptedDb, record: LoginCode | None,
             submitted: str, issued_after: datetime | None = None) -> None:
    monkeypatch.setattr(auth_service, "_last_request", lambda _db, _index, _purpose: record)
    auth_service.consume_code(
        db,  # type: ignore[arg-type]
        address="priya@northgate.example",
        purpose=CodePurpose.SIGN_IN,
        submitted=submitted,
        issued_after=issued_after,
    )


# ── Atomic attempt counting: the SQL itself ────────────────────────────────
def test_a_code_attempt_is_one_atomic_update_returning(monkeypatch: pytest.MonkeyPatch) -> None:
    db = _ScriptedDb(1)
    with pytest.raises(CodeRejected):
        _consume(monkeypatch, db, _code(), WRONG)

    sql = db.sql(0)
    assert sql.startswith("UPDATE login_codes SET attempts=(login_codes.attempts + ")
    # Liveness and the cap are in the WHERE clause, so the database decides.
    assert "login_codes.attempts < " in sql
    assert "login_codes.consumed_at IS NULL" in sql
    assert "login_codes.burned_at IS NULL" in sql
    assert "login_codes.expires_at > " in sql
    assert sql.rstrip().endswith("RETURNING login_codes.attempts")
    cap = [v for k, v in db.statements[0].compile().params.items() if k.startswith("attempts_")]
    assert settings.login_code_max_attempts in cap


def test_a_failed_login_count_is_one_atomic_update_returning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = _user()
    db = _ScriptedDb(1)
    monkeypatch.setattr(auth_service.audit, "record", lambda _db, **_entry: None)

    auth_service._note_failed_attempt(db, user)  # type: ignore[arg-type]

    sql = db.sql(0)
    assert sql.startswith(
        "UPDATE users SET failed_login_count=(users.failed_login_count + "
    )
    assert sql.rstrip().endswith("RETURNING users.failed_login_count")
    assert user.failed_login_count == 1


def test_the_lock_follows_the_count_the_database_returned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The in-memory copy said 0; the database said the cap. The database wins."""
    user = _user(failed_login_count=0)
    db = _ScriptedDb(settings.max_failed_logins)
    locks: list[datetime | None] = []
    monkeypatch.setattr(auth_service.audit, "record", lambda _db, **_entry: None)
    monkeypatch.setattr(
        auth_service, "_set_address_lock",
        lambda _db, _user, until, **_kw: locks.append(until) or 1,
    )

    auth_service._note_failed_attempt(db, user)  # type: ignore[arg-type]

    assert len(locks) == 1 and locks[0] is not None


def test_a_lost_race_for_the_last_attempt_is_refused_and_not_counted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The UPDATE matched no row: another request took the last attempt."""
    db = _ScriptedDb(None)
    with pytest.raises(CodeRejected) as caught:
        _consume(monkeypatch, db, _code(attempts=4), RIGHT)
    assert caught.value.counted is False
    assert len(db.statements) == 1  # the hash was never compared


def test_the_last_wrong_attempt_burns_the_code(monkeypatch: pytest.MonkeyPatch) -> None:
    record = _code()
    db = _ScriptedDb(settings.login_code_max_attempts, "code-1")
    with pytest.raises(CodeRejected) as caught:
        _consume(monkeypatch, db, record, WRONG)
    assert caught.value.counted is True
    burn = db.sql(1)
    assert burn.startswith("UPDATE login_codes SET burned_at=")
    assert "login_codes.burned_at IS NULL" in burn
    assert record.burned_at is not None


def test_a_right_code_is_spent_once_atomically(monkeypatch: pytest.MonkeyPatch) -> None:
    record = _code()
    db = _ScriptedDb(1, "code-1")
    _consume(monkeypatch, db, record, RIGHT)
    spend = db.sql(1)
    assert spend.startswith("UPDATE login_codes SET consumed_at=")
    assert "login_codes.consumed_at IS NULL" in spend
    assert record.consumed_at is not None


def test_a_right_code_spent_by_a_parallel_request_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _ScriptedDb(1, None)
    with pytest.raises(CodeRejected) as caught:
        _consume(monkeypatch, db, _code(), RIGHT)
    assert caught.value.counted is False


# ── One refusal message ────────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("record", "returns", "submitted"),
    [
        (None, (), RIGHT),  # unknown / never issued
        (_code(expires_at=utcnow() - timedelta(seconds=1)), (), RIGHT),  # stale
        (_code(burned_at=utcnow()), (), RIGHT),  # burned
        (_code(consumed_at=utcnow()), (), RIGHT),  # already used
        (_code(), (1,), WRONG),  # wrong, attempts left
        (_code(), (settings.login_code_max_attempts, "code-1"), WRONG),  # last attempt
    ],
)
def test_every_code_refusal_says_the_same_thing(
    monkeypatch: pytest.MonkeyPatch, record: LoginCode | None, returns: tuple, submitted: str
) -> None:
    with pytest.raises(AuthError) as caught:
        _consume(monkeypatch, _ScriptedDb(*returns), record, submitted)
    assert caught.value.message == CODE_REJECTED_MESSAGE
    assert caught.value.status_code == 401
    for leak in ("attempt", "left", "locked", "minutes", "Too many"):
        assert leak not in caught.value.message


# ── authenticate(): what is counted, and the pause's exception ─────────────
class _AuthWorld:
    """Wires authenticate() to fakes and records what it did."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, user: User | None,
                 consume_error: CodeRejected | None = None) -> None:
        self.consumed: list[dict] = []
        self.failures: list[str | None] = []
        monkeypatch.setattr(auth_service, "find_user_by_email", lambda _db, _a: user)

        def fake_consume(_db: Any, **kwargs: Any) -> None:
            self.consumed.append(kwargs)
            if consume_error is not None:
                raise consume_error

        monkeypatch.setattr(auth_service, "consume_code", fake_consume)
        monkeypatch.setattr(
            auth_service, "_note_failed_attempt",
            lambda _db, _u, *, ip_address=None: self.failures.append(ip_address),
        )
        monkeypatch.setattr(auth_service, "_set_address_lock", lambda *_a, **_k: 1)
        monkeypatch.setattr(
            auth_service, "get_organization", lambda _db, tid: SimpleNamespace(id=tid)
        )
        monkeypatch.setattr(
            auth_service, "_issue_tokens",
            lambda *_a, **_k: auth_service.TokenPair("a", "r", 60),
        )
        monkeypatch.setattr(auth_service.audit, "record", lambda _db, **_e: None)

    def run(self, db: _ScriptedDb, ip: str | None = "203.0.113.9") -> auth_service.AuthResult:
        return auth_service.authenticate(
            db, email="priya@northgate.example", code=RIGHT, ip_address=ip  # type: ignore[arg-type]
        )


@pytest.mark.parametrize("who", ["unknown", "deactivated"])
def test_unknown_and_closed_addresses_get_the_same_refusal_uncounted(
    monkeypatch: pytest.MonkeyPatch, who: str
) -> None:
    user = None if who == "unknown" else _user(is_active=False)
    world = _AuthWorld(monkeypatch, user)
    with pytest.raises(CodeRejected) as caught:
        world.run(_ScriptedDb())
    assert caught.value.message == CODE_REJECTED_MESSAGE
    assert world.consumed == [] and world.failures == []


def test_a_stale_code_does_not_count_towards_the_pause(monkeypatch: pytest.MonkeyPatch) -> None:
    """Anyone can send a stale code for free; counting it paused anyone's account."""
    world = _AuthWorld(monkeypatch, _user(), consume_error=CodeRejected(counted=False))
    db = _ScriptedDb()
    with pytest.raises(CodeRejected):
        world.run(db)
    assert world.failures == []
    assert db.commits == 1


def test_a_wrong_guess_at_a_live_code_is_counted(monkeypatch: pytest.MonkeyPatch) -> None:
    world = _AuthWorld(monkeypatch, _user(), consume_error=CodeRejected(counted=True))
    db = _ScriptedDb()
    with pytest.raises(CodeRejected):
        world.run(db)
    assert world.failures == ["203.0.113.9"]
    assert db.commits == 1  # the attempt outlives the failed request


def test_an_unpaused_address_gets_the_normal_attempt_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    world = _AuthWorld(monkeypatch, _user())
    world.run(_ScriptedDb())
    assert world.consumed[0]["issued_after"] is None


def test_a_paused_address_lets_a_freshly_mailed_code_in_from_another_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    until = utcnow() + timedelta(minutes=10)
    user = _user(locked_until=until)
    world = _AuthWorld(monkeypatch, user)

    result = world.run(_ScriptedDb(), ip="198.51.100.20")

    assert result.user is user
    (call,) = world.consumed
    # Only codes mailed after the pause began, with one try (see consume_code).
    assert call["issued_after"] == until - timedelta(minutes=settings.lockout_minutes)


def test_the_network_that_caused_the_pause_gets_no_fresh_code_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = _user(locked_until=utcnow() + timedelta(minutes=10))
    world = _AuthWorld(monkeypatch, user, consume_error=CodeRejected(counted=True))
    attacker = "203.0.113.9"
    for _ in range(settings.max_failed_logins):
        with pytest.raises(CodeRejected):
            world.run(_ScriptedDb(), ip=attacker)
    tried = len(world.consumed)

    # The attacker's network is now paused: refused before the code is touched,
    # so it cannot burn the owner's fresh code either.
    with pytest.raises(CodeRejected) as caught:
        world.run(_ScriptedDb(), ip=attacker)
    assert caught.value.message == CODE_REJECTED_MESSAGE
    assert len(world.consumed) == tried

    # The same /64 is the same network for IPv6.
    world2 = _AuthWorld(monkeypatch, user, consume_error=CodeRejected(counted=True))
    for _ in range(settings.max_failed_logins):
        with pytest.raises(CodeRejected):
            world2.run(_ScriptedDb(), ip="2001:db8:1:2::1")
    with pytest.raises(CodeRejected):
        world2.run(_ScriptedDb(), ip="2001:db8:1:2:ffff::9")
    assert len(world2.consumed) == settings.max_failed_logins


def test_a_fresh_code_gets_exactly_one_try_while_paused(monkeypatch: pytest.MonkeyPatch) -> None:
    lock_started = utcnow() - timedelta(minutes=1)
    db = _ScriptedDb(1, "code-1")
    with pytest.raises(CodeRejected) as caught:
        _consume(monkeypatch, db, _code(created_at=utcnow()), WRONG, issued_after=lock_started)
    assert caught.value.counted is True
    cap = [v for k, v in db.statements[0].compile().params.items() if k.startswith("attempts_")]
    assert 1 in cap
    assert db.sql(1).startswith("UPDATE login_codes SET burned_at=")  # one try, then dead


def test_a_code_from_before_the_pause_is_refused_untouched(monkeypatch: pytest.MonkeyPatch) -> None:
    lock_started = utcnow() - timedelta(minutes=1)
    db = _ScriptedDb()
    with pytest.raises(CodeRejected) as caught:
        _consume(
            monkeypatch, db, _code(created_at=lock_started - timedelta(minutes=2)), RIGHT,
            issued_after=lock_started,
        )
    assert caught.value.counted is False
    assert db.statements == []


# ── The per-network table is bounded ───────────────────────────────────────
def test_the_per_network_table_is_capped() -> None:
    table = auth_service._SourceFailures(max_entries=3)
    for n in range(10):
        table.note_failure((f"idx-{n}", "203.0.113.9"))
    assert len(table) == 3
    # The least recently seen were evicted, the newest kept.
    assert table._entries.keys() == {("idx-7", "203.0.113.9"), ("idx-8", "203.0.113.9"),
                                     ("idx-9", "203.0.113.9")}


def test_the_per_network_pause_needs_the_full_count_and_expires(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    table = auth_service._SourceFailures(max_entries=10)
    key = ("idx-priya", "203.0.113.9")
    for _ in range(settings.max_failed_logins - 1):
        assert table.note_failure(key) is False
    assert table.is_paused(key) is False
    assert table.note_failure(key) is True
    assert table.is_paused(key) is True
    assert table.is_paused(("idx-priya", "198.51.100.20")) is False

    later = auth_service.time.monotonic() + settings.lockout_minutes * 60 + 1
    monkeypatch.setattr(auth_service.time, "monotonic", lambda: later)
    assert table.is_paused(key) is False


# ── Refresh rotation and session audit ─────────────────────────────────────
def test_refresh_rotation_locks_the_token_row() -> None:
    db = _ScriptedDb(None)
    with pytest.raises(AuthError):
        auth_service.refresh_session(db, refresh_token="not-a-token")  # type: ignore[arg-type]
    assert "FOR UPDATE" in db.sql(0)


def _token(**overrides: Any) -> RefreshToken:
    fields: dict[str, Any] = {
        "id": "rt-1",
        "tenant_id": "org-1",
        "user_id": "user-1",
        "token_hash": hash_token("raw-refresh-secret"),
        "family_id": "fam-1",
        "expires_at": utcnow() + timedelta(days=1),
        "revoked_at": None,
        "replaced_by": None,
    }
    fields.update(overrides)
    return RefreshToken(**fields)


def _capture_audit(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    rows: list[dict] = []
    monkeypatch.setattr(auth_service.audit, "record", lambda _db, **entry: rows.append(entry))
    monkeypatch.setattr(auth_service, "bind_tenant", lambda _db, _tenant: None)
    return rows


def test_logout_is_audited_without_the_token(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = _capture_audit(monkeypatch)
    monkeypatch.setattr(auth_service, "_revoke_family", lambda _db, _fam, _why: 2)
    db = _ScriptedDb(_token(), objects={(User, "user-1"): _user()})

    auth_service.logout(db, refresh_token="raw-refresh-secret", ip_address="203.0.113.9")  # type: ignore[arg-type]

    (row,) = rows
    assert row["context"]["event"] == "signed_out"
    assert row["tenant_id"] == "org-1" and row["ip_address"] == "203.0.113.9"
    assert "raw-refresh-secret" not in repr(row)
    assert hash_token("raw-refresh-secret") not in repr(row)


def test_logout_of_an_unknown_token_writes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = _capture_audit(monkeypatch)
    auth_service.logout(_ScriptedDb(None), refresh_token="nope")  # type: ignore[arg-type]
    assert rows == []


def test_an_expired_refresh_is_audited_and_committed(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = _capture_audit(monkeypatch)
    record = _token(expires_at=utcnow() - timedelta(seconds=1))
    db = _ScriptedDb(record, objects={(User, "user-1"): _user()})

    with pytest.raises(AuthError):
        auth_service.refresh_session(db, refresh_token="raw-refresh-secret")  # type: ignore[arg-type]

    (row,) = rows
    assert row["context"] == {"event": "refresh_failed", "family_id": "fam-1", "reason": "expired"}
    assert db.commits == 1 and record.revoked_reason == "expired"


def test_a_refresh_for_a_closed_account_is_audited(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = _capture_audit(monkeypatch)
    db = _ScriptedDb(_token(), objects={(User, "user-1"): _user(is_active=False)})

    with pytest.raises(AuthError):
        auth_service.refresh_session(db, refresh_token="raw-refresh-secret")  # type: ignore[arg-type]

    (row,) = rows
    assert row["context"]["reason"] == "inactive"
    assert db.commits == 1


def test_a_workspace_switch_is_audited_in_the_target_workspace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = _capture_audit(monkeypatch)
    actor = _user()
    seat = _user(id="user-2", tenant_id="org-2")
    monkeypatch.setattr(
        auth_service, "find_membership",
        lambda _db, **_k: SimpleNamespace(is_active=True, user_id="user-2"),
    )
    monkeypatch.setattr(auth_service, "get_organization", lambda _db, tid: SimpleNamespace(id=tid))
    monkeypatch.setattr(auth_service, "set_last_active", lambda _db, _u: None)
    monkeypatch.setattr(
        auth_service, "_issue_tokens", lambda *_a, **_k: auth_service.TokenPair("a", "r", 60)
    )
    db = _ScriptedDb(objects={(User, "user-2"): seat})

    auth_service.switch_workspace(db, actor=actor, organization_id="org-2", ip_address="203.0.113.9")  # type: ignore[arg-type]

    (row,) = rows
    assert row["tenant_id"] == "org-2" and row["context"] == {"event": "workspace_switched"}
    # Nothing about the workspace they came from leaks into this one's log.
    assert "org-1" not in repr(row)


# ── Against a real database: racing sessions ───────────────────────────────
def _race(n: int, work: Any) -> list[Any]:
    barrier = threading.Barrier(n)
    results: list[Any] = [None] * n

    def run(i: int) -> None:
        barrier.wait(timeout=30)
        results[i] = work()

    threads = [threading.Thread(target=run, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert not any(t.is_alive() for t in threads)
    return results


def _registered_with_live_code(client: Any) -> str:
    from tests.support.mailbox import current
    from tests.test_api_flows import register

    register(client)
    address = "owner@northgate.example"
    assert client.post("/api/v1/auth/request-code", json={"email": address}).status_code == 200
    return current().code_for(address)


@requires_db
def test_parallel_wrong_guesses_cannot_beat_the_attempt_cap(client, clean_db, mailbox):  # noqa: ANN001
    """The HIGH finding: parallel guesses each read the same count and all passed."""
    from sqlalchemy import select

    from app.core.crypto import email_index
    from app.db.session import SessionLocal, bind_tenant

    _registered_with_live_code(client)
    address = "owner@northgate.example"
    workers = settings.login_code_max_attempts * 3

    def guess() -> bool | None:
        db = SessionLocal()
        try:
            auth_service.authenticate(db, email=address, code=WRONG, ip_address=None)
            return None
        except CodeRejected as exc:
            return exc.counted
        finally:
            db.rollback()
            db.close()

    outcomes = _race(workers, guess)

    assert outcomes.count(True) == settings.login_code_max_attempts
    assert outcomes.count(None) == 0
    clean_db.expire_all()
    code = clean_db.execute(
        select(LoginCode)
        .where(LoginCode.purpose == CodePurpose.SIGN_IN.value)
        .order_by(LoginCode.created_at.desc())
    ).scalars().first()
    assert code.attempts == settings.login_code_max_attempts
    assert code.burned_at is not None

    from app.models.identity import AuthIdentity

    identity = clean_db.get(AuthIdentity, email_index(address))
    bind_tenant(clean_db, identity.tenant_id)
    user = clean_db.get(User, identity.user_id, populate_existing=True)
    # Every counted guess reached the account counter: none was lost.
    assert user.failed_login_count == settings.login_code_max_attempts


@requires_db
def test_parallel_failed_logins_are_all_counted(client, clean_db, mailbox):  # noqa: ANN001
    from app.core.crypto import email_index
    from app.db.session import SessionLocal, bind_tenant
    from app.models.identity import AuthIdentity

    _registered_with_live_code(client)
    identity = clean_db.get(AuthIdentity, email_index("owner@northgate.example"))
    workers = settings.max_failed_logins - 1  # below the cap: no lock resets the count

    def fail_once() -> int:
        db = SessionLocal()
        try:
            bind_tenant(db, identity.tenant_id)
            user = db.get(User, identity.user_id)
            auth_service._note_failed_attempt(db, user)
            db.commit()
            return user.failed_login_count
        finally:
            db.close()

    seen = _race(workers, fail_once)

    # Each request saw its own value from RETURNING; nothing was overwritten.
    assert sorted(seen) == list(range(1, workers + 1))
    bind_tenant(clean_db, identity.tenant_id)
    user = clean_db.get(User, identity.user_id, populate_existing=True)
    assert user.failed_login_count == workers


@requires_db
def test_a_right_code_redeems_once_under_a_race(client, clean_db, mailbox):  # noqa: ANN001
    from app.db.session import SessionLocal

    code = _registered_with_live_code(client)

    def redeem() -> bool:
        db = SessionLocal()
        try:
            auth_service.consume_code(
                db, address="owner@northgate.example", purpose=CodePurpose.SIGN_IN,
                submitted=code,
            )
            db.commit()
            return True
        except CodeRejected:
            db.commit()
            return False
        finally:
            db.close()

    assert _race(6, redeem).count(True) == 1


@requires_db
def test_stale_codes_cannot_pause_an_account(client, clean_db, mailbox):  # noqa: ANN001
    """Wrong codes with no live code behind them used to lock anyone out."""
    from tests.test_api_flows import sign_in

    _registered_with_live_code(client)
    address = "owner@northgate.example"
    # Burn the live code, then keep sending codes at a dead one.
    for _ in range(settings.login_code_max_attempts + settings.max_failed_logins * 2):
        response = client.post("/api/v1/auth/login", json={"email": address, "code": WRONG})
        assert response.status_code == 401
        assert response.json()["detail"] == CODE_REJECTED_MESSAGE

    # Neither the address nor this network was paused: the owner gets in.
    assert sign_in(client).status_code == 200


@requires_db
def test_logout_is_in_the_audit_log(client, clean_db):  # noqa: ANN001
    from tests.test_auth_hardening import _audit_rows
    from tests.test_api_flows import register

    body = register(client)
    org_id = body["session"]["organization"]["id"]
    refresh_token = body["tokens"]["refresh_token"]
    assert client.post("/api/v1/auth/logout", json={"refresh_token": refresh_token}).status_code == 200
    (row,) = _audit_rows(clean_db, org_id, "signed_out")
    assert refresh_token not in repr(row.context)
