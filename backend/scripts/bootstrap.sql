-- One-time database bootstrap. Run as a superuser, before the first migration.
--
--   psql -h <host> -U postgres \
--        -v owner_password="$AUTOMARKET_OWNER_PASSWORD" \
--        -v app_password="$AUTOMARKET_APP_PASSWORD" \
--        -f scripts/bootstrap.sql
--
-- Both passwords are required (at least 16 characters each; generate them,
-- e.g. `openssl rand -base64 32`) and there are no defaults: the script stops
-- before changing anything if either is missing or short. They only set the
-- password of a role this script creates; an existing role keeps its own.
-- Put the values in the environment variables above rather than typing them
-- on the command line, so they stay out of shell history.
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
-- The passwords come from the psql variables in the header. psql does not
-- substitute variables inside a DO block, so each CREATE ROLE is built with
-- format(%L) (quoted as a literal) and run with \gexec.
\set ON_ERROR_STOP on

-- Keep the statements that carry a password out of the server log, whatever
-- the logging settings are (this session only). These need a superuser,
-- which managed PostgreSQL (RDS, Cloud SQL, Neon, Supabase) does not hand
-- out, so a refusal here is tolerated and reported instead of stopping the
-- script. If the warning appears, check that the server does not log
-- statements (log_statement must not be 'all' or 'ddl') before going on, or
-- set the role passwords afterwards with psql's \password.
\set ON_ERROR_STOP off
\set log_settings_refused false
SET log_statement = 'none';
\if :ERROR
    \set log_settings_refused true
\endif
SET log_min_duration_statement = -1;
\if :ERROR
    \set log_settings_refused true
\endif
SET log_min_error_statement = panic;
\if :ERROR
    \set log_settings_refused true
\endif
\if :log_settings_refused
    \echo 'WARNING: could not turn statement logging off for this session (not a superuser).'
    \echo 'The CREATE ROLE statements below carry passwords; make sure the server does not log statements.'
\endif
\set ON_ERROR_STOP on

\if :{?owner_password}
\else
    \echo 'Missing -v owner_password=... (see the header of this file).'
    DO $$ BEGIN RAISE EXCEPTION 'owner_password is not set'; END $$;
\endif
\if :{?app_password}
\else
    \echo 'Missing -v app_password=... (see the header of this file).'
    DO $$ BEGIN RAISE EXCEPTION 'app_password is not set'; END $$;
\endif
SELECT length(:'owner_password') < 16 OR length(:'app_password') < 16 AS weak_password,
       :'owner_password' = :'app_password' AS same_password
\gset
\if :weak_password
    DO $$ BEGIN RAISE EXCEPTION 'owner_password and app_password must be at least 16 characters'; END $$;
\endif
\if :same_password
    DO $$ BEGIN RAISE EXCEPTION 'owner_password and app_password must differ'; END $$;
\endif

SELECT format('CREATE ROLE automarket_owner LOGIN PASSWORD %L', :'owner_password')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'automarket_owner')
\gexec
SELECT format('CREATE ROLE automarket_app LOGIN PASSWORD %L', :'app_password')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'automarket_app')
\gexec

-- Harden attributes even when the roles already existed (idempotent re-run).
-- CREATE ROLE defaults to NOSUPERUSER NOBYPASSRLS; ALTER covers older roles.
ALTER ROLE automarket_owner NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE;
ALTER ROLE automarket_app NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE;

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
--
-- Default privileges cannot leave one table out, so audit_log is granted
-- UPDATE and DELETE like every other table when a migration creates it. The
-- append-only migration (0020) revokes them straight after, and the REVOKE
-- below repeats that whenever this script runs again: the GRANT ... ON ALL
-- TABLES above would otherwise hand them back.
ALTER DEFAULT PRIVILEGES FOR ROLE automarket_owner IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO automarket_app;
ALTER DEFAULT PRIVILEGES FOR ROLE automarket_owner IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO automarket_app;

-- The audit log is append-only: the app role may add and read entries, never
-- change or remove them. On a first run, before any migration, the table does
-- not exist yet and 0020 applies the same REVOKE when it does.
DO $$
BEGIN
    IF to_regclass('public.audit_log') IS NOT NULL THEN
        REVOKE UPDATE, DELETE, TRUNCATE ON audit_log FROM automarket_app;
    END IF;
END
$$;

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
