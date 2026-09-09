"""Test fixtures.

Tests that need a database use the real PostgreSQL schema — SQLite would not
exercise JSONB, row-level security, or the upsert the metrics service relies
on, so testing against it would prove the wrong thing.

Set ``TEST_DATABASE_URL`` to a throwaway database. Without it, the
database-backed tests skip and the pure-logic tests still run.
"""
from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import asynccontextmanager

import pytest

# Test settings must be in place before app.core.config is imported anywhere.
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("SECRET_KEY", "test-secret-key-at-least-32-characters-long!!")
os.environ.setdefault(
    "MASTER_ENCRYPTION_KEY", "YXV0b21hcmtldC10ZXN0LW1hc3Rlci1rZXktMzJieXQ="
)
os.environ.setdefault(
    "BLIND_INDEX_KEY", "YXV0b21hcmtldC10ZXN0LWJsaW5kLWluZGV4LTMyYnk="
)
os.environ.setdefault("RATE_LIMIT_ENABLED", "false")
# Sign in twice in one test and the real 60-second resend throttle would
# suppress the second code, so it is off by default here. The tests that
# exercise the throttle turn it back on explicitly — see
# test_api_flows.py::test_a_second_code_request_is_throttled.
os.environ.setdefault("LOGIN_CODE_RESEND_SECONDS", "0")
os.environ.setdefault("SCHEDULER_ENABLED", "false")
# The suite installs a fake provider, so no real key is used — but the
# settings still have to be valid, and LLM_MODEL deliberately has no default.
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key-not-used")
os.environ.setdefault("LLM_PROVIDER", "anthropic")
os.environ.setdefault("LLM_MODEL", "test-model-not-called")

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "")
if TEST_DATABASE_URL:
    os.environ["DATABASE_URL"] = TEST_DATABASE_URL

requires_db = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="Set TEST_DATABASE_URL to a throwaway PostgreSQL database",
)


def pytest_report_header() -> str:
    """Say, at the top of every run, whether half the suite is dormant.

    Without TEST_DATABASE_URL every database-backed test skips, and a skip
    is quiet: the run says "294 passed" and looks green. That is how the SEO
    pipeline's missing read step survived — the tests that would have caught
    it were never executed here, and nothing in the output said so.
    """
    if TEST_DATABASE_URL:
        target = TEST_DATABASE_URL.rsplit("/", 1)[-1]
        return f"database tests: ON ({target})"
    return (
        "database tests: SKIPPED — no TEST_DATABASE_URL. The API, scheduler, "
        "RLS and agent-pipeline tests will not run. Set it to a throwaway "
        "database whose name contains 'test' — for example "
        "postgresql+psycopg://user:pass@127.0.0.1:5432/automarket_test"
    )


@pytest.fixture(autouse=True)
def fake_llm():  # noqa: ANN201
    """Install the deterministic provider for every test.

    The application has exactly one provider — Anthropic — so nothing can be
    faked by configuration. Tests inject the double directly instead, which
    also means no test can accidentally spend money.
    """
    from app import llm
    from tests.support.fake_llm import FakeLLMProvider

    llm.set_provider(FakeLLMProvider())
    yield
    llm.reset_provider()


@pytest.fixture(autouse=True)
def fake_connectors():  # noqa: ANN201
    """Register the fake connectors, and clear what they recorded."""
    from tests.support import fake_connectors as fakes

    fakes.install()
    fakes.reset_recordings()
    yield
    fakes.reset_recordings()


@pytest.fixture(autouse=True)
def mailbox(monkeypatch):  # noqa: ANN001, ANN201
    """Capture outbound mail, and read the sign-in codes out of it.

    Autouse, so no test can reach a real SMTP server, and so a test that
    forgets to assert on mail still cannot send any. Tests that need to
    complete a login ask for this fixture and take the code from it — which is
    the only way, since the product stores nothing but an HMAC of it.
    """
    from tests.support.mailbox import install

    return install(monkeypatch)


@pytest.fixture(scope="session")
def engine():  # noqa: ANN201
    from app.db.session import engine as app_engine

    return app_engine


@pytest.fixture(scope="session")
def schema(engine) -> Iterator[None]:  # noqa: ANN001
    """Create the schema once for the session, and tear it down after."""
    from app.db.base import Base
    from app.db.rls import apply_policies
    from app.db.session import assert_disposable
    import app.models  # noqa: F401

    # Checked against the *engine's* database name rather than the environment
    # variable that was meant to set it. Those two came apart once and took a
    # working workspace with them.
    assert_disposable(engine)

    with engine.begin() as conn:
        Base.metadata.drop_all(bind=conn)
        Base.metadata.create_all(bind=conn)
        apply_policies(conn)
    yield
    with engine.begin() as conn:
        Base.metadata.drop_all(bind=conn)


@pytest.fixture
def db(schema) -> Iterator:  # noqa: ANN001, ANN201
    """A session per test, rolled back afterwards."""
    from app.db.session import SessionLocal

    session = SessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


@pytest.fixture
def clean_db(schema) -> Iterator:  # noqa: ANN001, ANN201
    """A session that truncates every table first, for isolation."""
    from sqlalchemy import text

    from app.db.base import Base
    from app.db.session import SessionLocal

    session = SessionLocal()
    tables = ", ".join(t.name for t in reversed(Base.metadata.sorted_tables))
    session.execute(text(f"TRUNCATE {tables} CASCADE"))
    session.commit()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


@pytest.fixture
def client(schema) -> Iterator:  # noqa: ANN001, ANN201
    """A TestClient with the app's lifespan skipped.

    The scheduler and the startup connectivity probe are not what these tests
    are about, and starting them would make every test slower and flakier.
    """
    from fastapi.testclient import TestClient

    from app.main import create_app

    app = create_app()
    app.router.lifespan_context = _noop_lifespan
    with TestClient(app) as test_client:
        yield test_client


@asynccontextmanager
async def _noop_lifespan(app):  # noqa: ANN001, ANN201
    """Replaces the app's lifespan so tests skip startup entirely."""
    yield


@pytest.fixture
def org(clean_db):  # noqa: ANN001, ANN201
    """A provisioned organisation with one admin, ready to use."""
    from app.core.crypto import email_index
    from app.core.rbac import Role
    from app.db.base import new_id
    from app.db.session import bind_tenant
    from app.models.identity import AuthIdentity
    from app.models.workspace import Organization, User
    from app.services.encryption import OrgCipher
    from app.services.provisioning import provision_organization

    org_id = new_id()
    organization = Organization(
        id=org_id,
        name="Test Motors",
        slug=f"test-motors-{org_id[:8]}",
        primary_domain="testmotors.example",
        wrapped_dek=OrgCipher.provision(org_id),
    )
    clean_db.add(organization)
    clean_db.flush()
    bind_tenant(clean_db, organization.id)

    admin = User(
        tenant_id=organization.id,
        name="Test Admin",
        email="admin@testmotors.example",
        email_index=email_index("admin@testmotors.example"),
        role=Role.ADMIN.value,
        is_owner=True,
        email_verified=True,
    )
    clean_db.add(admin)
    clean_db.flush()
    clean_db.add(
        AuthIdentity(
            email_index=admin.email_index,
            user_id=admin.id,
            tenant_id=organization.id,
        )
    )
    provision_organization(clean_db, organization)
    clean_db.commit()

    return organization


@pytest.fixture
def second_org(clean_db, org):  # noqa: ANN001, ANN201
    """A second provisioned organisation.

    Anything that walks every workspace — the scheduler, the metric rollup,
    approval expiry, catalogue back-fill — behaves differently with one
    organisation than with two, because a per-transaction tenant pin only
    conflicts once there is a second tenant to conflict with. A single-tenant
    test cannot see that class of bug at all, so cross-tenant behaviour is
    tested against this fixture rather than ``org`` alone.
    """
    from app.core.crypto import email_index
    from app.core.rbac import Role
    from app.db.base import new_id
    from app.db.session import bind_tenant
    from app.models.identity import AuthIdentity
    from app.models.workspace import Organization, User
    from app.services.encryption import OrgCipher
    from app.services.provisioning import provision_organization

    org_id = new_id()
    organization = Organization(
        id=org_id,
        name="Second Motors",
        slug=f"second-motors-{org_id[:8]}",
        primary_domain="secondmotors.example",
        wrapped_dek=OrgCipher.provision(org_id),
    )
    clean_db.add(organization)
    clean_db.flush()
    bind_tenant(clean_db, organization.id)

    admin = User(
        tenant_id=organization.id,
        name="Second Admin",
        email="admin@secondmotors.example",
        email_index=email_index("admin@secondmotors.example"),
        role=Role.ADMIN.value,
        is_owner=True,
        email_verified=True,
    )
    clean_db.add(admin)
    clean_db.flush()
    clean_db.add(
        AuthIdentity(
            email_index=admin.email_index,
            user_id=admin.id,
            tenant_id=organization.id,
        )
    )
    provision_organization(clean_db, organization)
    clean_db.commit()

    return organization
