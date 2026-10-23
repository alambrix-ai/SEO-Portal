# Non-superuser runtime database role

**Status:** implemented on `cursor/non-superuser-db-role-1439` (Alembic head `0049_app_role_grants`)  
**Goal:** the API, Celery workers and the integration test suite must never connect to Postgres as a superuser or as the table owner. Both bypass row-level security even when policies use `FORCE ROW LEVEL SECURITY`.

## Why

Postgres superusers always bypass RLS. Table owners bypass RLS unless `FORCE` is set — and even with `FORCE`, a mistaken policy or ownership change is enough to leak tenants. The suite previously had to add explicit `tenant_id` filters because CI connected as `POSTGRES_USER` (a superuser). That hid real isolation failures.

## Role model

| Role | Connects via | Privileges |
|---|---|---|
| `automarket_owner` | `MIGRATION_DATABASE_URL` / `DATABASE_ADMIN_URL` (Alembic / schema fixture only) | Owns `public`, runs migrations, `NOSUPERUSER`, `NOBYPASSRLS` |
| `automarket_app` | `DATABASE_URL` / `TEST_DATABASE_URL` | DML only (`SELECT/INSERT/UPDATE/DELETE`), `USAGE` on sequences, default privileges for future objects, **not** table owner, `NOSUPERUSER`, `NOBYPASSRLS` |

Passwords never live in code or migrations. Bootstrap takes them from psql `-v` / env. Migration `0049` only creates roles when missing and when `AUTOMARKET_*_PASSWORD` env vars are present; otherwise it hardens attributes and re-applies grants idempotently.

## What shipped

1. **Alembic `0049_app_role_grants`** — optional role create from env, harden, grants, reversible revoke.
2. **Config** — `MIGRATION_DATABASE_URL` with alias `DATABASE_ADMIN_URL`; Alembic warns when falling back to `DATABASE_URL`.
3. **Startup guard** — ownership check covers every RLS-scoped table; refuse privileged roles in all environments unless `ALLOW_PRIVILEGED_DB_ROLE=true` (loud warning). Celery `worker_process_init` runs the same check.
4. **Tests** — owner DSN for schema/TRUNCATE; app DSN for `SessionLocal`; CI bootstraps both via `scripts/ensure_test_roles.py`; `tests/test_rls_enforcement.py` proves cross-tenant reads are blocked without an app-level `tenant_id` filter.
5. **Docs / compose / `.env*.example`** — this runbook plus deployment notes.

## Production rollout runbook

Do this in a maintenance window. Keep the previous `DATABASE_URL` handy so you can roll back the env vars without a schema change.

1. **Create roles** (once per cluster), as a PostgreSQL administrator:

   ```bash
   export AUTOMARKET_OWNER_PASSWORD="$(openssl rand -hex 32)"
   export AUTOMARKET_APP_PASSWORD="$(openssl rand -hex 32)"
   # store both in the secret manager, then:
   psql -h <host> -U <admin> -d postgres \
     -v owner_password="$AUTOMARKET_OWNER_PASSWORD" \
     -v app_password="$AUTOMARKET_APP_PASSWORD" \
     -f backend/scripts/bootstrap.sql
   ```

   On an existing database that already has the schema, you can instead run
   migration `0049` with both password env vars set (creates roles if missing,
   then grants), or re-run the grant section of `bootstrap.sql` after
   `\connect` to the app database.

2. **Reassign ownership** of existing objects to `automarket_owner` if tables
   were created by another role (skip when bootstrap already set the owner):

   ```sql
   REASSIGN OWNED BY <old_runtime_role> TO automarket_owner;
   -- or, per schema:
   ALTER SCHEMA public OWNER TO automarket_owner;
   -- then re-run grants (bootstrap.sql or alembic upgrade to 0049)
   ```

3. **Set secrets** in the platform (Render / compose / k8s):

   | Variable | Value |
   |---|---|
   | `MIGRATION_DATABASE_URL` | `postgresql+psycopg://automarket_owner:…@host/db?sslmode=require` (migrate job only) |
   | `DATABASE_URL` | `postgresql+psycopg://automarket_app:…@host/db?sslmode=require` (api, workers, beat) |
   | `ALLOW_PRIVILEGED_DB_ROLE` | `false` |

4. **Migrate** with the owner DSN: `alembic upgrade head` (compose `migrate` service, or the release job). Confirm head is `0049_app_role_grants` or later.

5. **Verify** before flipping traffic:

   ```sql
   SELECT rolname, rolsuper, rolbypassrls
   FROM pg_roles
   WHERE rolname IN ('automarket_owner', 'automarket_app');
   -- both rows: rolsuper=false, rolbypassrls=false

   SELECT c.relname, pg_get_userbyid(c.relowner) AS owner
   FROM pg_class c
   JOIN pg_namespace n ON n.oid = c.relnamespace
   WHERE n.nspname = 'public' AND c.relkind = 'r'
   ORDER BY 1;
   -- no RLS table owned by automarket_app
   ```

   Connect as the app role and confirm:

   ```sql
   SELECT current_user;                    -- automarket_app
   SHOW row_security;                      -- on
   -- with no app.current_tenant pin, tenant tables return zero rows
   ```

6. **Restart** API and workers. Startup must succeed without
   `ALLOW_PRIVILEGED_DB_ROLE`. If it refuses, the role is still privileged —
   fix the DSN; do not flip the escape hatch in production.

7. **Roll back** (env only): point `DATABASE_URL` back at the previous role,
   restart. Schema grants from `0049` can stay; downgrade only if you need to
   revoke them (`alembic downgrade 0048_ensure_users_custom_role_fk`).

## Out of scope

- Changing `main` without Dishant's approval.
- Renaming `automarket_app` / `automarket_owner`.
