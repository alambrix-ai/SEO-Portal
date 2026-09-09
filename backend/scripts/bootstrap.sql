-- One-time database bootstrap. Run as a superuser, before the first migration.
--
--   psql -h <host> -U postgres -f scripts/bootstrap.sql
--
-- This is separate from the Alembic migration on purpose: creating databases,
-- roles and extensions needs privileges the application role should not have,
-- and a migration that assumes superuser fails on every managed Postgres
-- (RDS, Cloud SQL, Neon, Supabase) where nobody is one.

-- ── Roles ───────────────────────────────────────────────────────────────────
-- Two roles, deliberately:
--
--   automarket_owner  owns the schema and runs migrations
--   automarket_app    what the service connects as, and owns nothing
--
-- The split matters for tenant isolation: a table's owner bypasses row-level
-- security unless it is FORCE'd. The policies do force it, but running the
-- application as a non-owner means a mistake in one policy cannot expose
-- another organisation's rows.
--
-- Replace both passwords before running this anywhere real.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'automarket_owner') THEN
        CREATE ROLE automarket_owner LOGIN PASSWORD 'change-me-owner';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'automarket_app') THEN
        CREATE ROLE automarket_app LOGIN PASSWORD 'change-me-app';
    END IF;
END
$$;

-- ── Database ────────────────────────────────────────────────────────────────
-- CREATE DATABASE cannot run inside a transaction block or a DO block, so it
-- is guarded with \gexec instead of an IF NOT EXISTS.
SELECT 'CREATE DATABASE automarket OWNER automarket_owner'
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = 'automarket')
\gexec

\connect automarket

-- ── Extensions ──────────────────────────────────────────────────────────────
-- pgcrypto is not required (ids are generated in the application), but it is
-- useful for ad-hoc administration and for `gen_random_uuid()` in later
-- migrations. citext gives case-insensitive slug comparison.
CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS citext;

-- ── Schema ownership and grants ─────────────────────────────────────────────
ALTER SCHEMA public OWNER TO automarket_owner;

-- The app role may read and write data, and nothing else: no DDL, no
-- ownership, no ability to disable a policy.
GRANT USAGE ON SCHEMA public TO automarket_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO automarket_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO automarket_app;

-- Tables created by future migrations inherit the same grants, so a new table
-- never silently locks the application out.
ALTER DEFAULT PRIVILEGES FOR ROLE automarket_owner IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO automarket_app;
ALTER DEFAULT PRIVILEGES FOR ROLE automarket_owner IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO automarket_app;

-- Explicitly deny the app role the ability to create objects in public.
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
REVOKE CREATE ON SCHEMA public FROM automarket_app;

-- ── Verification ────────────────────────────────────────────────────────────
SELECT
    r.rolname,
    r.rolsuper   AS is_superuser,
    r.rolbypassrls AS bypasses_rls
FROM pg_roles r
WHERE r.rolname IN ('automarket_owner', 'automarket_app');

-- Both roles must show false for bypasses_rls. If either is true, tenant
-- isolation is not being enforced for connections made as that role.
