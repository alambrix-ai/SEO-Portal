"""Make ``audit_log`` append-only inside the database.

The application only ever inserts audit rows (``app.services.audit``). These
two layers make that a property of the database, so a bug, a compromised
process or a hand-run statement cannot rewrite history:

* **A trigger** rejects every ``UPDATE`` and ``DELETE`` on ``audit_log``, row
  by row, for every role, including the table owner and superusers.
* **A statement trigger** rejects ``TRUNCATE`` the same way (row triggers do
  not fire on it). Added by migration 0023.
* **A ``REVOKE``** removes ``UPDATE``, ``DELETE`` and ``TRUNCATE`` from the
  application role (``automarket_app``). It is guarded, so it is skipped where
  that role does not exist (local development, CI). It takes effect once the
  service connects as that role (``scripts/bootstrap.sql``).

Consequences, by design:

* Deleting an organisation cascades into ``audit_log`` and is therefore
  refused. A GDPR hard-delete needs a controlled purge path run by the owner
  role, which drops the trigger inside its own transaction. That path is out
  of scope here.
* Nobody can ``TRUNCATE`` the table while the triggers are enabled. The
  owner, and only the owner, can ``ALTER TABLE audit_log DISABLE TRIGGER``
  inside its own transaction first; the test fixtures do exactly that to
  reset the schema between tests.

The SQL is kept here rather than only in the migration, so the tests can
install the guard on a schema built by ``create_all``.
"""
from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.engine import Connection

TABLE = "audit_log"
FUNCTION = "audit_log_append_only"
TRIGGER = "trg_audit_log_append_only"
TRUNCATE_TRIGGER = "trg_audit_log_no_truncate"
APP_ROLE = "automarket_app"

#: Raised by the trigger: insufficient_privilege, the same class of error the
#: REVOKE produces, so callers see one kind of failure either way.
ERRCODE = "42501"


def _guarded_for_role(statement: str) -> str:
    """Run ``statement`` only if the application role exists."""
    return (
        "DO $$ BEGIN "
        f"IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{APP_ROLE}') THEN "
        f"{statement}; "
        "END IF; "
        "END $$"
    )


def install_statements() -> list[str]:
    return [
        (
            f"CREATE OR REPLACE FUNCTION {FUNCTION}() RETURNS trigger "
            "LANGUAGE plpgsql AS $$ BEGIN "
            f"RAISE EXCEPTION '{TABLE} is append-only: % is not allowed', TG_OP "
            f"USING ERRCODE = '{ERRCODE}'; "
            "END $$"
        ),
        f"DROP TRIGGER IF EXISTS {TRIGGER} ON {TABLE}",
        (
            f"CREATE TRIGGER {TRIGGER} BEFORE UPDATE OR DELETE ON {TABLE} "
            f"FOR EACH ROW EXECUTE FUNCTION {FUNCTION}()"
        ),
        _guarded_for_role(f"REVOKE UPDATE, DELETE, TRUNCATE ON {TABLE} FROM {APP_ROLE}"),
    ]


def truncate_install_statements() -> list[str]:
    """The BEFORE TRUNCATE guard (migration 0023). Reuses the function from
    :func:`install_statements`, whose message names the operation."""
    return [
        f"DROP TRIGGER IF EXISTS {TRUNCATE_TRIGGER} ON {TABLE}",
        (
            f"CREATE TRIGGER {TRUNCATE_TRIGGER} BEFORE TRUNCATE ON {TABLE} "
            f"FOR EACH STATEMENT EXECUTE FUNCTION {FUNCTION}()"
        ),
    ]


def truncate_uninstall_statements() -> list[str]:
    return [f"DROP TRIGGER IF EXISTS {TRUNCATE_TRIGGER} ON {TABLE}"]


def uninstall_statements() -> list[str]:
    """Reverse of :func:`install_statements`, restoring the earlier grants."""
    return [
        f"DROP TRIGGER IF EXISTS {TRIGGER} ON {TABLE}",
        f"DROP FUNCTION IF EXISTS {FUNCTION}()",
        _guarded_for_role(f"GRANT UPDATE, DELETE ON {TABLE} TO {APP_ROLE}"),
    ]


def install(conn: Connection) -> None:
    """Every guard, as migrations 0020 and 0023 leave the schema."""
    for statement in [*install_statements(), *truncate_install_statements()]:
        conn.execute(text(statement))


def uninstall(conn: Connection) -> None:
    for statement in [*truncate_uninstall_statements(), *uninstall_statements()]:
        conn.execute(text(statement))
