"""PostgreSQL engine, session factory, and per-session tenant pinning.

Connections are made over TLS (``sslmode`` from settings); the pool is
pre-pinged and recycled so a proxy or failover dropping idle connections does
not surface as a request error.
"""
from __future__ import annotations

import re

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import TypeVar

from sqlalchemy import create_engine, event, select, text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.orm import Session, sessionmaker

T = TypeVar("T")

from app.core.config import settings
from app.core.logging import get_logger

log = get_logger(__name__)

# A database whose name says it is safe to destroy. Deliberately narrow: the
# default is to refuse.
_DISPOSABLE_NAME = re.compile(r"(^|[_-])(test|tests|tmp|temp|scratch)([_-]|$)", re.I)


def _build_engine() -> Engine:
    connect_args: dict = {
        # Fail fast rather than hanging a worker when the database is away.
        "connect_timeout": 10,
        "application_name": f"{settings.app_name} api",
    }
    if settings.postgres_sslrootcert:
        connect_args["sslrootcert"] = settings.postgres_sslrootcert
    return create_engine(
        settings.sqlalchemy_url,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_pre_ping=True,
        pool_recycle=settings.db_pool_recycle_seconds,
        echo=settings.db_echo,
        connect_args=connect_args,
        future=True,
    )


engine = _build_engine()

SessionLocal = sessionmaker(
    bind=engine, autoflush=False, expire_on_commit=False, class_=Session
)

# ── Tenant pinning for row-level security ──────────────────────────────────
# The pin is transaction-local (``set_config(..., true)``), so a pooled
# connection can never carry one request's organisation into the next.
#
# Transaction-local also means it is *lost on commit* — and a request that
# commits and then reads (register, then render the new session; update a
# budget, then return the workspace) would find nothing, because RLS would
# reject every row. So the chosen organisation is remembered on the session
# and re-applied whenever a new transaction begins.
_SET_TENANT = text("SELECT set_config('app.current_tenant', :tenant_id, true)")
_CLEAR_TENANT = text("SELECT set_config('app.current_tenant', '', true)")
_TENANT_KEY = "automarket_tenant_id"


def assert_disposable(bind: Engine | Connection | None = None) -> str:
    """Refuse to continue unless the target database is a throwaway.

    Every destructive schema operation goes through this — the test suite's
    setup and teardown, and anything else that calls ``drop_all``.

    It exists because of a real incident: a diagnostic script set
    ``DATABASE_URL`` to the test database *after* ``app.core.config`` had
    already read the environment, so the engine was still pointed at the
    development database. The script then dropped the schema, and a working
    workspace went with it. Nothing in the code objected, because
    ``Base.metadata.drop_all`` does what it is told.

    The rule is the database *name*, not the environment variable, because the
    name is what actually determines which rows get dropped. A DSN that does
    not name itself disposable is treated as somebody's real data.
    """
    target = bind if bind is not None else engine
    url = getattr(target, "url", None) or getattr(getattr(target, "engine", None), "url", None)
    name = (url.database if url else "") or ""
    if not _DISPOSABLE_NAME.search(name):
        raise RuntimeError(
            f"Refusing a destructive schema operation on {name!r}: the database "
            "name does not mark it as disposable. Point DATABASE_URL or "
            "TEST_DATABASE_URL at a database whose name contains 'test', "
            "'tmp' or 'scratch' — and set it before importing app.core.config, "
            "or the engine will still be built from .env."
        )
    return name


def bind_tenant(db: Session, tenant_id: str | None) -> None:
    """Pin (or clear) the organisation every RLS policy will check.

    Survives commits within the same session: see :func:`_reapply_tenant`.
    """
    if not settings.db_enforce_rls:
        return

    db.info[_TENANT_KEY] = str(tenant_id) if tenant_id else None
    if tenant_id:
        db.execute(_SET_TENANT, {"tenant_id": str(tenant_id)})
    else:
        db.execute(_CLEAR_TENANT)


def current_tenant(db: Session) -> str | None:
    return db.info.get(_TENANT_KEY)


@event.listens_for(Session, "after_begin")
def _reapply_tenant(session: Session, transaction, connection) -> None:  # noqa: ANN001
    """Re-apply the session's organisation to each new transaction.

    Without this, the first commit in a request silently drops the pin and
    every later read comes back empty.
    """
    if not settings.db_enforce_rls:
        return
    tenant_id = session.info.get(_TENANT_KEY)
    if tenant_id:
        connection.execute(_SET_TENANT, {"tenant_id": tenant_id})


def get_db() -> Iterator[Session]:
    """FastAPI dependency: one session per request, always closed.

    The tenant is pinned later by ``app.api.deps.get_current_user``, once the
    access token has been verified.
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def session_scope(tenant_id: str | None = None) -> Iterator[Session]:
    """Transactional session for the scheduler, agents and CLI entry points."""
    db = SessionLocal()
    try:
        bind_tenant(db, tenant_id)
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def run_per_organization(
    db: Session,
    handler: Callable[[Session, str], T],
    *,
    should_stop: Callable[[], bool] | None = None,
    label: str = "operation",
    only_active: bool = True,
) -> list[T]:
    """Run ``handler(db, tenant_id)`` once per organisation, safely.

    Background jobs that touch every workspace — the agent scheduler, the
    metric rollup, approval expiry, catalogue back-fill — all need the same
    thing, and it is easy to get wrong in the same way:

    ``SET LOCAL`` scopes the tenant pin to the current transaction, so work
    has to be **committed while its own organisation is still pinned**. A loop
    that pins each tenant and defers to one commit at the end will flush every
    tenant's dirty rows under whichever pin was set last; row-level security
    then correctly rejects all the others, the writes silently vanish, and the
    flush fails on the row-count mismatch.

    So this commits between organisations, clears the identity map so nothing
    can leak into the next pin, and isolates failures: one workspace erroring
    is rolled back and logged, and the rest still run.

    Returns the handler's result for each organisation that succeeded.
    """
    from app.models.workspace import Organization

    stmt = select(Organization.id).order_by(Organization.created_at)
    if only_active:
        stmt = stmt.where(Organization.is_active.is_(True))
    tenant_ids = list(db.execute(stmt).scalars())

    results: list[T] = []
    for tenant_id in tenant_ids:
        if should_stop is not None and should_stop():
            break
        try:
            bind_tenant(db, tenant_id)
            result = handler(db, tenant_id)
            db.commit()
        except Exception:  # noqa: BLE001 - one workspace must not stop the rest
            db.rollback()
            log.exception("%s failed for workspace %s", label, tenant_id)
            continue
        finally:
            # Nothing may stay in the identity map: the next iteration runs
            # under a different pin, and a lingering dirty row would flush
            # under it.
            db.expunge_all()
        results.append(result)

    return results


def check_connection() -> None:
    """Verify the database is reachable, and warn if the link is unencrypted."""
    with engine.connect() as conn:
        version = conn.execute(text("SHOW server_version")).scalar_one()
        row = conn.execute(
            text("SELECT ssl FROM pg_stat_ssl WHERE pid = pg_backend_pid()")
        ).first()
        encrypted = bool(row and row[0])
        log.info("PostgreSQL %s reachable (TLS=%s)", version, "on" if encrypted else "off")
        if encrypted:
            return
        # Neon (and some other poolers) terminate TLS before the backend, so
        # pg_stat_ssl can read false even when the client connected with
        # sslmode=require. Trust the configured mode in that case.
        if settings.effective_sslmode in ("require", "verify-ca", "verify-full"):
            log.warning(
                "pg_stat_ssl reports no TLS, but sslmode=%s — treating the "
                "link as encrypted (common with managed poolers)",
                settings.effective_sslmode,
            )
            return
        if settings.is_production:
            raise RuntimeError(
                "Database connection is not encrypted. Set POSTGRES_SSLMODE=require "
                "(or stricter) and give the server a certificate."
            )
