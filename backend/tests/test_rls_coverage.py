"""Every table with a tenant_id column has made an explicit RLS decision.

A table is either under row-level security (``TENANT_SCOPED_TABLES``) or
exempt with a written reason (``RLS_EXEMPT_TABLES``) — never neither, never
both. A new tenant table that forgets to choose fails here rather than
shipping without the second line of tenant isolation.

Built from the SQLAlchemy metadata, so no database is needed.
"""
from __future__ import annotations

import app.models  # noqa: F401 - registers every mapper on Base.metadata
from app.db.base import Base
from app.db.rls import RLS_EXEMPT_TABLES, TENANT_SCOPED_TABLES


def _tenant_tables() -> set[str]:
    return {name for name, table in Base.metadata.tables.items() if "tenant_id" in table.c}


def test_every_tenant_table_is_in_exactly_one_list() -> None:
    scoped = set(TENANT_SCOPED_TABLES)
    exempt = set(RLS_EXEMPT_TABLES)
    undecided = sorted(_tenant_tables() - scoped - exempt)
    both = sorted(scoped & exempt)
    assert undecided == [], f"Tenant tables with no RLS decision: {undecided}"
    assert both == [], f"Tables both scoped and exempt: {both}"


def test_lists_name_only_real_tenant_tables() -> None:
    """A stale or misspelt entry would hide a table that really is missing."""
    tenant_tables = _tenant_tables()
    assert sorted(set(TENANT_SCOPED_TABLES) - tenant_tables) == []
    assert sorted(set(RLS_EXEMPT_TABLES) - tenant_tables) == []


def test_scoped_list_has_no_duplicates() -> None:
    assert len(TENANT_SCOPED_TABLES) == len(set(TENANT_SCOPED_TABLES))


def test_every_exemption_gives_a_reason() -> None:
    for table, reason in RLS_EXEMPT_TABLES.items():
        assert isinstance(reason, str) and len(reason.strip()) >= 20, table


def test_exemptions_are_the_known_pre_session_tables() -> None:
    """Growing this list weakens isolation, so it is pinned: adding an
    exemption has to change this test too, in the same review."""
    assert set(RLS_EXEMPT_TABLES) == {
        "auth_identities",
        "refresh_tokens",
        "invitations",
        "oauth_states",
        "workspace_memberships",
    }


def test_creative_studio_tables_are_under_rls() -> None:
    for table in ("stored_files", "brand_kits", "products", "creative_jobs"):
        assert table in TENANT_SCOPED_TABLES
        assert table not in RLS_EXEMPT_TABLES


def test_a_table_missing_from_both_lists_is_detected() -> None:
    """The check itself works: an undeclared tenant table is reported."""
    from sqlalchemy import Column, MetaData, String, Table

    metadata = MetaData()
    Table("rogue_tenant_rows", metadata, Column("tenant_id", String(36)))
    found = {n for n, t in metadata.tables.items() if "tenant_id" in t.c}
    undecided = found - set(TENANT_SCOPED_TABLES) - set(RLS_EXEMPT_TABLES)
    assert undecided == {"rogue_tenant_rows"}
