"""Dated snapshots of a workspace's audits, kept so the console can show history.

One generic table rather than one per audit, because every audit needs the
same three things from its history: when it was taken and by what, the
headline numbers (``metrics``), and enough per-item state (``items``) to say
what changed since the previous snapshot. What differs between audits is only
how those two documents are built, and that lives in ``app.services.history``.

Kinds (``AuditSnapshot.kind``):

* ``technical_seo``  one per Technical SEO Auditor run. ``items`` maps an
                     issue id to its state, for the open and ignored issues.
* ``seo_scores``     one per On-Page SEO Sync run. ``items`` maps a page url
                     to its semantic-gap score.
* ``brand_profile``  one per generate, save or restore of the brand profile.
                     ``items`` is the whole profile, so a version can be
                     compared and restored.
* ``site_analysis``, ``backlink_scan``, ``competitor_scan``: read-only
  analyses; ``brand_kit``, ``product``, ``agent_config``, ``workspace_policy``
  and ``creative_text``: restorable settings and content. Their builders live
  in ``app.services.history`` too.

Versions: every snapshot is version N of one *subject* (``subject_id``; ""
for a kind with a single subject per workspace, such as the brand profile).
Numbers run 1, 2, 3 … per (tenant, kind, subject) and are unique. The
database numbers a row inserted without one, and refuses any UPDATE: a
version is a record, so it is never edited, only added or (for retention)
deleted. Both rules are triggers, installed by migration
0021_metrics_usage_versions and, for a schema built from these models (the
tests), by the DDL hooks at the bottom of this module.

Local ranking checks are not stored here: ``local_rank_checks`` is already a
dated row per phrase per check, so a copy would only be a second place for the
same numbers to disagree.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    DDL,
    CheckConstraint,
    DateTime,
    FetchedValue,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    event,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import PK, Base, TenantScopedMixin, new_id, utcnow


class SnapshotKind:
    TECHNICAL_SEO = "technical_seo"
    SEO_SCORES = "seo_scores"
    BRAND_PROFILE = "brand_profile"
    SITE_ANALYSIS = "site_analysis"
    BACKLINK_SCAN = "backlink_scan"
    COMPETITOR_SCAN = "competitor_scan"
    BRAND_KIT = "brand_kit"
    PRODUCT = "product"
    AGENT_CONFIG = "agent_config"
    WORKSPACE_POLICY = "workspace_policy"
    CREATIVE_TEXT = "creative_text"


class SnapshotSource:
    #: Taken at the end of a successful agent run.
    AGENT_RUN = "agent_run"
    #: The state when history was first requested, before any recorded run.
    BASELINE = "baseline"
    #: Brand profile only.
    GENERATED = "generated"
    EDITED = "edited"
    RESTORED = "restored"


class SnapshotActor:
    """Who caused a version (``AuditSnapshot.actor_type``)."""

    USER = "user"
    AGENT = "agent"
    #: Scheduled or automatic work with no person or agent behind it.
    SYSTEM = "system"


class AuditSnapshot(Base, TenantScopedMixin):
    __tablename__ = "audit_snapshots"
    __table_args__ = (
        Index("ix_audit_snapshots_tenant_kind_created", "tenant_id", "kind", "created_at"),
        Index(
            "ix_audit_snapshots_tenant_kind_subject_created",
            "tenant_id",
            "kind",
            "subject_id",
            "created_at",
        ),
        UniqueConstraint(
            "tenant_id", "kind", "subject_id", "version", name="uq_audit_snapshots_subject_version"
        ),
        CheckConstraint("version >= 1", name="ck_audit_snapshots_version"),
    )

    id: Mapped[str] = mapped_column(PK, primary_key=True, default=new_id)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    #: Which thing this is a version of (a kit, product or agent id); "" when
    #: the kind has one subject per workspace.
    subject_id: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    #: 1, 2, 3 … per (tenant, kind, subject). Left unset, the database assigns
    #: the next number; ``app.services.history.store`` allocates it under a
    #: lock instead, so concurrent writers cannot collide.
    version: Mapped[int] = mapped_column(Integer, server_default=FetchedValue(), nullable=False)
    #: The agent run it was taken after, or "" (baseline, brand profile).
    run_id: Mapped[str] = mapped_column(String(36), default="", nullable=False)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    #: Short human description, e.g. "Scheduled run" or "Restored the 3 Oct version".
    label: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    created_by: Mapped[str] = mapped_column(String(36), default="", nullable=False)
    created_by_name: Mapped[str] = mapped_column(String(160), default="", nullable=False)
    #: ``SnapshotActor``: a person, an agent, or the system.
    actor_type: Mapped[str] = mapped_column(String(16), default="", nullable=False)
    #: The agent that produced it, when ``actor_type`` is agent.
    agent_slug: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    #: The AI model id that produced it, when one did.
    model: Mapped[str] = mapped_column(String(128), default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    #: Structural settings the version was made with (ids, options, counts).
    #: Never customer copy: that belongs in ``payload_encrypted``.
    inputs: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    metrics: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    #: Per-item state for diffs, capped (see ``app.services.history.store.MAX_ITEMS``).
    items: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    #: How many items there were before the cap; more than ``len(items)`` means truncated.
    item_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    #: A JSON document encrypted under the organisation's key, for versions
    #: whose content is the customer's own words (creative text, product
    #: copy, brand kit voice). "" when the kind keeps everything in ``items``.
    payload_encrypted: Mapped[str] = mapped_column(Text, default="", nullable=False)


# ── Database rules ──────────────────────────────────────────────────────────
# The same statements as migration 0021_metrics_usage_versions (which keeps a
# frozen copy; tests/test_metrics_versions_models.py checks they agree). No
# literal percent signs: DDL() treats them as format markers.
SNAPSHOT_TRIGGER_DDL: tuple[str, ...] = (
    """
CREATE OR REPLACE FUNCTION audit_snapshots_number_version() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, public AS $$
BEGIN
    IF NEW.version IS NULL THEN
        SELECT COALESCE(MAX(s.version), 0) + 1 INTO NEW.version
          FROM audit_snapshots AS s
         WHERE s.tenant_id = NEW.tenant_id
           AND s.kind = NEW.kind
           AND s.subject_id = NEW.subject_id;
    END IF;
    RETURN NEW;
END
$$
""".strip(),
    """
CREATE OR REPLACE FUNCTION audit_snapshots_block_update() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, public AS $$
BEGIN
    RAISE EXCEPTION 'audit_snapshots rows cannot be changed; record a new version instead'
        USING ERRCODE = 'restrict_violation';
END
$$
""".strip(),
    "DROP TRIGGER IF EXISTS trg_audit_snapshots_number_version ON audit_snapshots",
    (
        "CREATE TRIGGER trg_audit_snapshots_number_version BEFORE INSERT ON audit_snapshots "
        "FOR EACH ROW EXECUTE FUNCTION audit_snapshots_number_version()"
    ),
    "DROP TRIGGER IF EXISTS trg_audit_snapshots_immutable ON audit_snapshots",
    (
        "CREATE TRIGGER trg_audit_snapshots_immutable BEFORE UPDATE ON audit_snapshots "
        "FOR EACH ROW EXECUTE FUNCTION audit_snapshots_block_update()"
    ),
)

for _statement in SNAPSHOT_TRIGGER_DDL:
    event.listen(
        AuditSnapshot.__table__,
        "after_create",
        DDL(_statement).execute_if(dialect="postgresql"),
    )
