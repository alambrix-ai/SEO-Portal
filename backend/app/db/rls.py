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

import re

from sqlalchemy import text
from sqlalchemy.engine import Connection

from app.core.logging import get_logger

log = get_logger(__name__)

TENANT_SETTING = "app.current_tenant"
#: Table names passed into DDL must be plain SQL identifiers (our constants).
_TABLE_IDENT = re.compile(r"^[a-z][a-z0-9_]*$")

# Tables carrying a tenant_id column that are read inside an authenticated,
# tenant-pinned session.
#
# Some tables deliberately sit outside this list, because they are read
# *before* a session exists and therefore cannot be scoped by one (the ones
# with a tenant_id column are listed, with reasons, in RLS_EXEMPT_TABLES):
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
#   oauth_states      redeemed by the public OAuth callback, by presenting a
#                     state signed with SECRET_KEY; keyed by a SHA-256 of
#                     the state's nonce, and deleted as it is redeemed
#   workspace_memberships
#                     lists an address's workspaces at sign-in, before a
#                     tenant is pinned; keyed by a blind index of the address
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
    "seo_issue_fix_proposals",
    "internal_link_suggestions",
    "topic_clusters",
    "topic_cluster_pages",
    "content_briefs",
    "willy_grounding_traces",
    "scheduled_reports",
    "report_artifacts",
    "alert_rules",
    "alert_events",
    "pseo_templates",
    "pseo_datasets",
    "pseo_page_jobs",
    "product_feed_findings",
    "content_repurpose_jobs",
    "video_script_jobs",
    "cross_channel_budget_proposals",
    "landing_pages",
    "ab_experiments",
    "competitor_ads",
    "locations",
    "social_mentions",
    "playbook_installations",
    "sso_connections",
    "scim_tokens",
    "custom_roles",
    "ip_allowlist_entries",
    "retention_policies",
    "subject_requests",
    "audit_export_jobs",
    "ai_governance_policies",
    "approval_steps",
    "api_tokens",
    "outbound_webhooks",
    "webhook_delivery_logs",
    "roi_attribution_snapshots",
    "billing_accounts",
    "usage_meters",
    "plan_entitlements",
    "mfa_recovery_codes",
    "webauthn_credentials",
    "mfa_pending_challenges",
    "user_approval_scopes",
    "warehouse_export_jobs",
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
    "local_rank_checks",
    "review_replies",
    "marketing_assets",
    "generated_images",
    "site_analyses",
    "audit_snapshots",
    "approval_items",
    "daily_metrics",
    # Creative Studio (0019_creative_studio)
    "stored_files",
    "brand_kits",
    "products",
    "creative_jobs",
    # Metrics and versions (0021_metrics_usage_versions)
    "metric_points",
    "social_post_metrics",
    # AI visibility tracker (0024_ai_visibility)
    "visibility_queries",
    "visibility_settings",
    "visibility_observations",
    # Content refresh (0024_content_refresh)
    "content_refresh_candidates",
)

# Tables that carry a tenant_id column but are deliberately NOT under RLS,
# each with the reason. tests/test_rls_coverage.py fails when a table with a
# tenant_id column is in neither list (or in both), so a new table cannot
# silently skip the decision.
RLS_EXEMPT_TABLES: dict[str, str] = {
    "auth_identities": (
        "Resolves which organisation an email belongs to at sign-in, before a "
        "session exists; keyed by an HMAC of the address."
    ),
    "refresh_tokens": "Redeemed by presenting a 32-byte token, before a session exists.",
    "invitations": "Opened by presenting an invite token, before a session exists.",
    "oauth_states": (
        "Redeemed by the public OAuth callback with a state signed by SECRET_KEY; "
        "keyed by a SHA-256 of the nonce and deleted as it is redeemed."
    ),
    "workspace_memberships": (
        "Read at sign-in to list an address's workspaces before a tenant is "
        "pinned (services/auth.py list_memberships); keyed by a blind index."
    ),
}

_POLICY = "tenant_isolation"


def _require_table_ident(table: str) -> str:
    """Reject anything that is not a known-shape SQL identifier."""
    if not _TABLE_IDENT.fullmatch(table):
        raise ValueError(f"Refusing non-identifier table name for RLS DDL: {table!r}")
    return table


def _statements(table: str) -> list[str]:
    table = _require_table_ident(table)
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
        table = _require_table_ident(table)
        exists = conn.execute(
            text("SELECT to_regclass(:t) IS NOT NULL"), {"t": f"public.{table}"}
        ).scalar_one()
        if not exists:
            log.warning("Skipping RLS for missing table %s", table)
            continue
        for stmt in _statements(table):
            # Table name is validated as a SQL identifier above; not user input.
            # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text
            conn.execute(text(stmt))
    log.info("Row-level security applied to %d tables", len(tables))


def drop_policies(conn: Connection, tables: tuple[str, ...] = TENANT_SCOPED_TABLES) -> None:
    for table in tables:
        table = _require_table_ident(table)
        # Table name is validated as a SQL identifier above; not user input.
        # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text
        conn.execute(text(f"DROP POLICY IF EXISTS {_POLICY} ON {table}"))
        # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text
        conn.execute(text(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY"))
        # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text
        conn.execute(text(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY"))
