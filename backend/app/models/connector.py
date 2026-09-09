"""Connector installation state and encrypted credentials."""
from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from sqlalchemy import Boolean, DateTime, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import PK, Base, TenantScopedMixin, TimestampMixin, new_id


class ConnectorCategory(StrEnum):
    CMS = "CMS"
    AD_PLATFORMS = "Ad Platforms"
    ANALYTICS_SEARCH = "Analytics & Search"
    CRM = "CRM"
    AEO_LLM = "AEO/LLM Monitoring"
    COLLABORATION = "Collaboration"


class ConnectorHealth(StrEnum):
    OK = "ok"
    DEGRADED = "degraded"
    FAILING = "failing"
    UNKNOWN = "unknown"


class ConnectorRecord(Base, TimestampMixin, TenantScopedMixin):
    """One organisation's installation of one registered connector.

    ``slug`` matches the package name under ``app/connectors/``.

    Credentials never exist as plaintext columns. They are a single JSON blob
    encrypted under the *organisation's own* data key (tier 2), which is itself
    stored wrapped under the master key — so reading a customer's API tokens
    out of a database dump needs the master key as well as the row.
    ``credential_hints`` holds only the non-secret parts (account ids,
    hostnames) so the console can show what is wired up without decrypting.
    """

    __tablename__ = "connectors"
    __table_args__ = (UniqueConstraint("tenant_id", "slug", name="uq_connector_tenant_slug"),)

    id: Mapped[str] = mapped_column(PK, primary_key=True, default=new_id)
    slug: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    category: Mapped[str] = mapped_column(String(40), nullable=False)

    connected: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    oauth_completed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    credentials_encrypted: Mapped[str] = mapped_column(Text, default="")
    credential_hints: Mapped[dict] = mapped_column(JSONB, default=dict)
    # Bumped whenever credentials are rewritten, so a stale in-flight client
    # can tell its cached secrets are no longer current.
    credentials_version: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # OAuth token lifecycle. The tokens themselves live inside the encrypted
    # blob; only the expiry is in the clear so refresh can be scheduled.
    token_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    scopes: Mapped[list] = mapped_column(JSONB, default=list)

    connected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    connected_by: Mapped[str] = mapped_column(String(160), default="")
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_health_check_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    health: Mapped[str] = mapped_column(
        String(16), default=ConnectorHealth.UNKNOWN.value, nullable=False
    )
    last_error: Mapped[str] = mapped_column(Text, default="")
