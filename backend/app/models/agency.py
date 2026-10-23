"""Agency parent accounts, client links, and white-label settings."""
from __future__ import annotations

from sqlalchemy import Boolean, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import FK, JSONDoc, PK, Base, TimestampMixin, new_id


class Agency(Base, TimestampMixin):
    """Parent partner account — not a client tenant; access is via memberships."""

    __tablename__ = "agencies"
    __table_args__ = (UniqueConstraint("slug", name="uq_agency_slug"),)

    id: Mapped[str] = mapped_column(PK, primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    slug: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class AgencyMembership(Base, TimestampMixin):
    """A person (by email blind index) belonging to an agency."""

    __tablename__ = "agency_memberships"
    __table_args__ = (
        UniqueConstraint("agency_id", "email_index", name="uq_agency_member"),
        Index("ix_agency_membership_email", "email_index"),
    )

    id: Mapped[str] = mapped_column(PK, primary_key=True, default=new_id)
    agency_id: Mapped[str] = mapped_column(
        FK, ForeignKey("agencies.id", ondelete="CASCADE"), nullable=False, index=True
    )
    email_index: Mapped[str] = mapped_column(String(64), nullable=False)
    #: owner | admin | member
    role: Mapped[str] = mapped_column(String(32), default="member", nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class AgencyClientLink(Base, TimestampMixin):
    """Explicit link from an agency to a client workspace (tenant)."""

    __tablename__ = "agency_client_links"
    __table_args__ = (
        UniqueConstraint("agency_id", "client_tenant_id", name="uq_agency_client"),
        Index("ix_agency_client_tenant", "client_tenant_id"),
    )

    id: Mapped[str] = mapped_column(PK, primary_key=True, default=new_id)
    agency_id: Mapped[str] = mapped_column(
        FK, ForeignKey("agencies.id", ondelete="CASCADE"), nullable=False, index=True
    )
    client_tenant_id: Mapped[str] = mapped_column(
        FK, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    #: view | manage
    access_level: Mapped[str] = mapped_column(String(16), default="view", nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class WhiteLabelSettings(Base, TimestampMixin):
    """Branding for an agency — never weakens auth origin checks."""

    __tablename__ = "white_label_settings"
    __table_args__ = (UniqueConstraint("agency_id", name="uq_white_label_agency"),)

    id: Mapped[str] = mapped_column(PK, primary_key=True, default=new_id)
    agency_id: Mapped[str] = mapped_column(
        FK, ForeignKey("agencies.id", ondelete="CASCADE"), nullable=False
    )
    custom_domain: Mapped[str] = mapped_column(String(255), default="")
    #: Encrypted logo/colours/from-name bundle.
    branding_encrypted: Mapped[str] = mapped_column(Text, default="")
    email_from_name: Mapped[str] = mapped_column(String(120), default="")
    #: Brand-safety rules (blocked terms) — structural only.
    brand_safety: Mapped[dict] = mapped_column(JSONDoc, default=dict)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
