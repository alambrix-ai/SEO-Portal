"""Platform-wide feature flags (Portal Admin).

Not tenant-scoped: one row controls the catalogue for every workspace.
Missing row means enabled (fail-open for newly shipped catalogue entries).
"""
from __future__ import annotations

from enum import StrEnum

from sqlalchemy import Boolean, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import PK, Base, TimestampMixin, new_id


class FeatureKind(StrEnum):
    CONNECTOR = "connector"
    AGENT = "agent"
    MODULE = "module"


class PortalFeatureFlag(Base, TimestampMixin):
    __tablename__ = "portal_feature_flags"
    __table_args__ = (
        UniqueConstraint("kind", "slug", name="uq_portal_feature_kind_slug"),
    )

    id: Mapped[str] = mapped_column(PK, primary_key=True, default=new_id)
    kind: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    slug: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    updated_by: Mapped[str] = mapped_column(String(255), default="")


class PlatformOAuthApp(Base, TimestampMixin):
    """The platform's own app with one sign-in provider (Google, Meta, ...).

    Set once by a portal admin under Portal Admin -> Sign-in providers, so
    nobody has to edit the server environment. Platform-level: not tenant
    scoped, outside row-level security, and read or written only through
    portal-admin routes and the sign-in code.

    The client id is not a secret (it travels in every sign-in URL). The
    client secret and the extra values (developer tokens, the GitHub App's
    private key) are encrypted under a key derived from the platform master
    key, bound to this provider (see app.services.platform_oauth_apps).
    """

    __tablename__ = "platform_oauth_apps"
    __table_args__ = (UniqueConstraint("provider", name="uq_platform_oauth_apps_provider"),)

    id: Mapped[str] = mapped_column(PK, primary_key=True, default=new_id)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    client_id: Mapped[str] = mapped_column(Text, default="", nullable=False)
    client_secret_encrypted: Mapped[str] = mapped_column(Text, default="", nullable=False)
    #: JSON object of the provider's extra values, encrypted as one document.
    extras_encrypted: Mapped[str] = mapped_column(Text, default="", nullable=False)
    updated_by: Mapped[str] = mapped_column(String(255), default="", nullable=False)
