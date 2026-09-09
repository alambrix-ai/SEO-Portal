"""PostgreSQL row-level security for tenant isolation.

Application queries always filter by ``tenant_id``. This is the layer that
holds when one of them forgets: with RLS enabled, a query on a tenant-scoped
table returns nothing unless the transaction has pinned an organisation via
``app.current_tenant`` (see ``app.db.session.bind_tenant``), and it can never
return another organisation's rows.

The policies are created by the initial Alembic migration and can be re-applied
idempotently with :func:`apply_policies` — useful after adding a table.

Note the database role matters: a superuser or the table owner bypasses RLS
unless ``FORCE ROW LEVEL SECURITY`` is set, which is why every policy below is
installed with ``FORCE``. Run the application as a dedicated non-owner role
(``automarket_app``) for the strongest guarantee.
"""
from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.engine import Connection

from app.core.logging import get_logger

log = get_logger(__name__)

TENANT_SETTING = "app.current_tenant"

# Tables carrying a tenant_id column that are read inside an authenticated,
# tenant-pinned session.
#
# Four tables deliberately sit outside this list, because they are read
# *before* a session exists and therefore cannot be scoped by one:
#
#   auth_identities   resolves which organisation an email belongs to at
#                     sign-in; holds only an HMAC of the address (see
#                     app/models/identity.py)
#   login_codes       the one-time passcode that is this platform's credential;
#                     requested before there is a session, and for sign-up
#                     before there is an organisation. Holds only an HMAC of
#                     the code and a blind index of the address.
#   refresh_tokens    redeemed by presenting a 32-byte token
#   invitations       opened by presenting an invite token
#
# In every case authorisation is possession of a secret that only ever went to
# the account holder, and every one of them stores that secret as an HMAC
# rather than in the clear. The tenant-scoped ones are still filtered by
# tenant_id in the queries that list them for an authenticated user.
TENANT_SCOPED_TABLES: tuple[str, ...] = (
    "users",
    "onboarding_state",
    "audit_log",
    "agents",
    "agent_runs",
    "connectors",
    "seo_pages",
    "seo_issues",
    "aeo_qa_pairs",
    "schema_patches",
    "referral_spam_events",
    "backlink_targets",
    "outreach_pitches",
    "competitor_alerts",
    "budget_allocations",
    "ad_creatives",
    "audience_clusters",
    "fraud_events",
    "approval_items",
    "daily_metrics",
)

_POLICY = "tenant_isolation"


def _statements(table: str) -> list[str]:
    predicate = (
        f"tenant_id = NULLIF(current_setting('{TENANT_SETTING}', true), '')::uuid"
    )
    return [
        f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY",
        f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY",
        f"DROP POLICY IF EXISTS {_POLICY} ON {table}",
        (
            f"CREATE POLICY {_POLICY} ON {table} "
            f"USING ({predicate}) WITH CHECK ({predicate})"
        ),
    ]


def apply_policies(conn: Connection, tables: tuple[str, ...] = TENANT_SCOPED_TABLES) -> None:
    """Enable RLS and install the isolation policy on every scoped table."""
    for table in tables:
        exists = conn.execute(
            text("SELECT to_regclass(:t) IS NOT NULL"), {"t": f"public.{table}"}
        ).scalar_one()
        if not exists:
            log.warning("Skipping RLS for missing table %s", table)
            continue
        for stmt in _statements(table):
            conn.execute(text(stmt))
    log.info("Row-level security applied to %d tables", len(tables))


def drop_policies(conn: Connection, tables: tuple[str, ...] = TENANT_SCOPED_TABLES) -> None:
    for table in tables:
        conn.execute(text(f"DROP POLICY IF EXISTS {_POLICY} ON {table}"))
        conn.execute(text(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY"))
        conn.execute(text(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY"))
