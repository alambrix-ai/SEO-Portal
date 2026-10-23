"""Enterprise SSO, SCIM, custom roles, and IP allowlists."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import FK, JSONDoc, PK, Base, TenantScopedMixin, TimestampMixin, new_id


class SsoConnection(Base, TimestampMixin, TenantScopedMixin):
    """SAML or OIDC connection for a workspace."""

    __tablename__ = "sso_connections"
    __table_args__ = (
        UniqueConstraint("tenant_id", "slug", name="uq_sso_connection_slug"),
        Index("ix_sso_connection_tenant_protocol", "tenant_id", "protocol"),
    )

    id: Mapped[str] = mapped_column(PK, primary_key=True, default=new_id)
    slug: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    #: saml | oidc
    protocol: Mapped[str] = mapped_column(String(16), nullable=False)
    issuer: Mapped[str] = mapped_column(String(512), default="")
    metadata_url: Mapped[str] = mapped_column(String(1000), default="")
    #: Encrypted IdP config (client_secret, certificates, ACS extras).
    config_encrypted: Mapped[str] = mapped_column(Text, default="")
    jit_provision: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    default_role: Mapped[str] = mapped_column(String(32), default="client", nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    #: Attribute claim used as subject (e.g. sub, NameID).
    subject_claim: Mapped[str] = mapped_column(String(80), default="sub", nullable=False)


class ScimToken(Base, TimestampMixin, TenantScopedMixin):
    """SCIM bearer token — store hash only; raw shown once at mint."""

    __tablename__ = "scim_tokens"
    __table_args__ = (Index("ix_scim_token_tenant_status", "tenant_id", "revoked_at"),)

    id: Mapped[str] = mapped_column(PK, primary_key=True, default=new_id)
    label: Mapped[str] = mapped_column(String(120), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    token_prefix: Mapped[str] = mapped_column(String(12), default="")
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by_id: Mapped[str | None] = mapped_column(FK, nullable=True)


class CustomRole(Base, TimestampMixin, TenantScopedMixin):
    """Tenant-defined role extending the six built-ins via a permission map."""

    __tablename__ = "custom_roles"
    __table_args__ = (
        UniqueConstraint("tenant_id", "slug", name="uq_custom_role_slug"),
    )

    id: Mapped[str] = mapped_column(PK, primary_key=True, default=new_id)
    slug: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    #: {module: "full"|"view"|"none"}
    permissions: Mapped[dict] = mapped_column(JSONDoc, default=dict)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class IpAllowlistEntry(Base, TimestampMixin, TenantScopedMixin):
    """CIDR or exact IP allowed for console/API access when allowlist is on."""

    __tablename__ = "ip_allowlist_entries"
    __table_args__ = (
        UniqueConstraint("tenant_id", "cidr", name="uq_ip_allowlist_cidr"),
    )

    id: Mapped[str] = mapped_column(PK, primary_key=True, default=new_id)
    cidr: Mapped[str] = mapped_column(String(64), nullable=False)
    label: Mapped[str] = mapped_column(String(120), default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
