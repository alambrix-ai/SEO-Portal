#!/usr/bin/env python3
"""Create automarket_owner / automarket_app on an existing test database.

Used by CI (and local suite setup) so pytest can connect as the non-owner
app role. Passwords come from the environment — never from argv (argv is
visible in ``ps``).

  AUTOMARKET_OWNER_PASSWORD=... AUTOMARKET_APP_PASSWORD=... \\
  ENSURE_ROLES_ADMIN_URL=postgresql://synaprank:...@127.0.0.1/automarket_test \\
    python scripts/ensure_test_roles.py
"""
from __future__ import annotations

import os
import sys

from psycopg import connect, sql

_MIN = 16


def main() -> int:
    admin_url = os.environ.get("ENSURE_ROLES_ADMIN_URL") or os.environ.get(
        "TEST_ADMIN_DATABASE_URL", ""
    )
    owner_password = os.environ.get("AUTOMARKET_OWNER_PASSWORD", "")
    app_password = os.environ.get("AUTOMARKET_APP_PASSWORD", "")
    if not admin_url:
        print("ENSURE_ROLES_ADMIN_URL is required", file=sys.stderr)
        return 2
    if len(owner_password) < _MIN or len(app_password) < _MIN:
        print(
            f"AUTOMARKET_OWNER_PASSWORD and AUTOMARKET_APP_PASSWORD must be "
            f"at least {_MIN} characters",
            file=sys.stderr,
        )
        return 2
    if owner_password == app_password:
        print("owner and app passwords must differ", file=sys.stderr)
        return 2

    # libpq accepts postgresql://; strip SQLAlchemy driver suffix if present.
    dsn = admin_url.replace("postgresql+psycopg://", "postgresql://", 1)

    with connect(dsn, autocommit=True) as conn:
        with conn.cursor() as cur:
            for role, password in (
                ("automarket_owner", owner_password),
                ("automarket_app", app_password),
            ):
                cur.execute(
                    "SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)
                )
                if cur.fetchone() is None:
                    cur.execute(
                        sql.SQL(
                            "CREATE ROLE {} LOGIN PASSWORD {} "
                            "NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE"
                        ).format(sql.Identifier(role), sql.Literal(password))
                    )
                cur.execute(
                    sql.SQL(
                        "ALTER ROLE {} NOSUPERUSER NOBYPASSRLS "
                        "NOCREATEDB NOCREATEROLE"
                    ).format(sql.Identifier(role))
                )

            cur.execute("SELECT current_database()")
            dbname = cur.fetchone()[0]
            cur.execute(
                sql.SQL("ALTER DATABASE {} OWNER TO automarket_owner").format(
                    sql.Identifier(dbname)
                )
            )
            cur.execute(
                sql.SQL("GRANT CONNECT ON DATABASE {} TO automarket_app").format(
                    sql.Identifier(dbname)
                )
            )
            cur.execute("ALTER SCHEMA public OWNER TO automarket_owner")
            cur.execute("GRANT USAGE ON SCHEMA public TO automarket_app")
            cur.execute("GRANT CREATE ON SCHEMA public TO automarket_owner")
            cur.execute("REVOKE CREATE ON SCHEMA public FROM automarket_app")
            cur.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
            cur.execute(
                "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES "
                "IN SCHEMA public TO automarket_app"
            )
            cur.execute(
                "GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public "
                "TO automarket_app"
            )
            cur.execute(
                "ALTER DEFAULT PRIVILEGES FOR ROLE automarket_owner "
                "IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE "
                "ON TABLES TO automarket_app"
            )
            cur.execute(
                "ALTER DEFAULT PRIVILEGES FOR ROLE automarket_owner "
                "IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES "
                "TO automarket_app"
            )
            cur.execute(
                """
                DO $$ BEGIN
                  IF to_regclass('public.audit_log') IS NOT NULL THEN
                    REVOKE UPDATE, DELETE, TRUNCATE ON audit_log
                      FROM automarket_app;
                  END IF;
                END $$
                """
            )
            cur.execute(
                """
                SELECT rolname, rolsuper, rolbypassrls
                FROM pg_roles
                WHERE rolname IN ('automarket_owner', 'automarket_app')
                ORDER BY 1
                """
            )
            for row in cur.fetchall():
                print(f"{row[0]}: superuser={row[1]} bypassrls={row[2]}")
                if row[1] or row[2]:
                    print("role still privileged — aborting", file=sys.stderr)
                    return 1
    print("test roles ready")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
