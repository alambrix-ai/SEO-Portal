-- Idempotent dual-role setup for an *existing* test database.
-- Unlike bootstrap.sql this does not CREATE DATABASE; run it against the
-- throwaway DB (e.g. automarket_test) as a superuser before the suite.
--
--   psql -h 127.0.0.1 -U postgres -d automarket_test \
--        -v owner_password="$AUTOMARKET_OWNER_PASSWORD" \
--        -v app_password="$AUTOMARKET_APP_PASSWORD" \
--        -f scripts/ensure_test_roles.sql
--
-- Then point TEST_MIGRATION_DATABASE_URL at automarket_owner and
-- TEST_DATABASE_URL at automarket_app.
\set ON_ERROR_STOP on

\if :{?owner_password}
\else
    \echo 'Missing -v owner_password=...'
    DO $$ BEGIN RAISE EXCEPTION 'owner_password is not set'; END $$;
\endif
\if :{?app_password}
\else
    \echo 'Missing -v app_password=...'
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

SELECT format(
    'CREATE ROLE automarket_owner LOGIN PASSWORD %L NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE',
    :'owner_password'
)
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'automarket_owner')
\gexec
SELECT format(
    'CREATE ROLE automarket_app LOGIN PASSWORD %L NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE',
    :'app_password'
)
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'automarket_app')
\gexec

ALTER ROLE automarket_owner NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE;
ALTER ROLE automarket_app NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE;

-- Current database (the test DB) is owned by the migrator role.
SELECT format('ALTER DATABASE %I OWNER TO automarket_owner', current_database())
\gexec

SELECT format('GRANT CONNECT ON DATABASE %I TO automarket_app', current_database())
\gexec

-- Fresh public schema owned by the migrator so leftover superuser-owned
-- functions from earlier runs cannot block create_all.
DROP SCHEMA IF EXISTS public CASCADE;
CREATE SCHEMA public AUTHORIZATION automarket_owner;
GRANT USAGE ON SCHEMA public TO automarket_app;
GRANT CREATE ON SCHEMA public TO automarket_owner;
REVOKE CREATE ON SCHEMA public FROM automarket_app;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;

ALTER DEFAULT PRIVILEGES FOR ROLE automarket_owner IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO automarket_app;
ALTER DEFAULT PRIVILEGES FOR ROLE automarket_owner IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO automarket_app;

CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS citext;

SELECT
    r.rolname,
    r.rolsuper AS is_superuser,
    r.rolbypassrls AS bypasses_rls
FROM pg_roles r
WHERE r.rolname IN ('automarket_owner', 'automarket_app');
