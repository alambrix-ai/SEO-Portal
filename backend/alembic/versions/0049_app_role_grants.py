"""Ensure non-superuser app/owner roles and DML grants.

Revision ID: 0049_app_role_grants
Revises: 0048_ensure_users_custom_role_fk
Create Date: 2026-10-23

The runtime role must never be a superuser, have BYPASSRLS, or own
RLS-protected tables — all of those defeat row-level security even with
FORCE. Prefer ``scripts/bootstrap.sql`` for first-time role creation
(needs a true superuser on many managed hosts). This revision:

* creates ``automarket_owner`` / ``automarket_app`` only when missing **and**
  ``AUTOMARKET_OWNER_PASSWORD`` / ``AUTOMARKET_APP_PASSWORD`` are set (never
  hardcodes a password);
* hardens both roles to ``NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE``;
* re-applies schema DML grants and default privileges idempotently;
* keeps ``audit_log`` append-only for the app role.

Downgrade revokes the grants this revision applies; it does not drop the
roles (that would strand deployments that already point ``DATABASE_URL`` at
``automarket_app``).
"""
from __future__ import annotations

import os
from collections.abc import Sequence

from alembic import op
from sqlalchemy import text

revision: str = "0049_app_role_grants"
down_revision: str | None = "0048_ensure_users_custom_role_fk"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OWNER_ROLE = "automarket_owner"
APP_ROLE = "automarket_app"
_MIN_PASSWORD_LEN = 16


def _role_exists(bind, name: str) -> bool:  # noqa: ANN001
    return bool(
        bind.execute(
            text("SELECT 1 FROM pg_roles WHERE rolname = :name"),
            {"name": name},
        ).scalar()
    )


def _create_role_if_missing(bind, name: str, password: str) -> None:  # noqa: ANN001
    """Create a login role with a password from the environment.

    Uses a driver-level literal so the password is never interpolated into a
    logged SQL string by Alembic's compiler. Statement logging may still
    capture it on the server; operators should prefer bootstrap.sql with
    log_statement=none for first create.
    """
    if _role_exists(bind, name):
        return
    if len(password) < _MIN_PASSWORD_LEN:
        raise RuntimeError(
            f"Refusing to create {name}: password from the environment is "
            f"shorter than {_MIN_PASSWORD_LEN} characters"
        )
    # psycopg3 sql composition — password is a bound literal, not concatenated.
    from psycopg import sql

    raw = bind.connection.driver_connection
    raw.execute(
        sql.SQL(
            "CREATE ROLE {} LOGIN PASSWORD {} "
            "NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE"
        ).format(sql.Identifier(name), sql.Literal(password))
    )


def _harden_role(bind, name: str) -> None:  # noqa: ANN001
    if not _role_exists(bind, name):
        return
    # Identifiers only; role names are constants in this module.
    bind.execute(
        text(
            f"ALTER ROLE {name} NOSUPERUSER NOBYPASSRLS "
            "NOCREATEDB NOCREATEROLE"
        )
    )


def _apply_grants(bind) -> None:  # noqa: ANN001
    if not _role_exists(bind, APP_ROLE):
        return
    statements = [
        f"GRANT USAGE ON SCHEMA public TO {APP_ROLE}",
        (
            f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES "
            f"IN SCHEMA public TO {APP_ROLE}"
        ),
        f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {APP_ROLE}",
        f"REVOKE CREATE ON SCHEMA public FROM {APP_ROLE}",
    ]
    if _role_exists(bind, OWNER_ROLE):
        statements.extend(
            [
                (
                    f"ALTER DEFAULT PRIVILEGES FOR ROLE {OWNER_ROLE} "
                    f"IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE "
                    f"ON TABLES TO {APP_ROLE}"
                ),
                (
                    f"ALTER DEFAULT PRIVILEGES FOR ROLE {OWNER_ROLE} "
                    f"IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES "
                    f"TO {APP_ROLE}"
                ),
            ]
        )
    for statement in statements:
        bind.execute(text(statement))
    # Append-only audit log: never hand UPDATE/DELETE/TRUNCATE back to the app.
    bind.execute(
        text(
            f"""
            DO $$ BEGIN
              IF to_regclass('public.audit_log') IS NOT NULL THEN
                REVOKE UPDATE, DELETE, TRUNCATE ON audit_log FROM {APP_ROLE};
              END IF;
            END $$
            """
        )
    )


def _revoke_grants(bind) -> None:  # noqa: ANN001
    if not _role_exists(bind, APP_ROLE):
        return
    statements = [
        (
            f"REVOKE SELECT, INSERT, UPDATE, DELETE ON ALL TABLES "
            f"IN SCHEMA public FROM {APP_ROLE}"
        ),
        f"REVOKE USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public FROM {APP_ROLE}",
        f"REVOKE USAGE ON SCHEMA public FROM {APP_ROLE}",
    ]
    if _role_exists(bind, OWNER_ROLE):
        statements.extend(
            [
                (
                    f"ALTER DEFAULT PRIVILEGES FOR ROLE {OWNER_ROLE} "
                    f"IN SCHEMA public REVOKE SELECT, INSERT, UPDATE, DELETE "
                    f"ON TABLES FROM {APP_ROLE}"
                ),
                (
                    f"ALTER DEFAULT PRIVILEGES FOR ROLE {OWNER_ROLE} "
                    f"IN SCHEMA public REVOKE USAGE, SELECT ON SEQUENCES "
                    f"FROM {APP_ROLE}"
                ),
            ]
        )
    for statement in statements:
        bind.execute(text(statement))


def upgrade() -> None:
    bind = op.get_bind()
    owner_password = os.environ.get("AUTOMARKET_OWNER_PASSWORD", "")
    app_password = os.environ.get("AUTOMARKET_APP_PASSWORD", "")
    if owner_password and app_password:
        if owner_password == app_password:
            raise RuntimeError(
                "AUTOMARKET_OWNER_PASSWORD and AUTOMARKET_APP_PASSWORD must differ"
            )
        _create_role_if_missing(bind, OWNER_ROLE, owner_password)
        _create_role_if_missing(bind, APP_ROLE, app_password)
    elif owner_password or app_password:
        raise RuntimeError(
            "Set both AUTOMARKET_OWNER_PASSWORD and AUTOMARKET_APP_PASSWORD "
            "to create missing roles, or neither to only harden and grant"
        )
    _harden_role(bind, OWNER_ROLE)
    _harden_role(bind, APP_ROLE)
    _apply_grants(bind)


def downgrade() -> None:
    bind = op.get_bind()
    _revoke_grants(bind)
