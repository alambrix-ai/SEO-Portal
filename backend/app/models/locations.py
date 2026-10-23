"""Multi-location business entities for local SEO at scale."""
from __future__ import annotations

from sqlalchemy import Float, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import FK, PK, Base, TenantScopedMixin, TimestampMixin, new_id


class Location(Base, TimestampMixin, TenantScopedMixin):
    """One physical (or virtual) location in a multi-site workspace."""

    __tablename__ = "locations"
    __table_args__ = (
        UniqueConstraint("tenant_id", "slug", name="uq_location_slug"),
        Index("ix_location_tenant_status", "tenant_id", "status"),
        Index("ix_location_tenant_region", "tenant_id", "region"),
    )

    id: Mapped[str] = mapped_column(PK, primary_key=True, default=new_id)
    slug: Mapped[str] = mapped_column(String(80), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    #: Prepared for enterprise brand/region RBAC scoping.
    brand: Mapped[str] = mapped_column(String(80), default="")
    region: Mapped[str] = mapped_column(String(80), default="")
    address_line: Mapped[str] = mapped_column(String(300), default="")
    city: Mapped[str] = mapped_column(String(120), default="")
    region_code: Mapped[str] = mapped_column(String(32), default="")
    postal_code: Mapped[str] = mapped_column(String(24), default="")
    country_code: Mapped[str] = mapped_column(String(2), default="")
    place_id: Mapped[str] = mapped_column(String(255), default="")
    gbp_location_id: Mapped[str] = mapped_column(String(255), default="")
    lat: Mapped[float | None] = mapped_column(Float, nullable=True)
    lng: Mapped[float | None] = mapped_column(Float, nullable=True)
    #: Encrypted operator notes (no PII in logs).
    notes_encrypted: Mapped[str] = mapped_column(Text, default="")
    #: active | archived
    status: Mapped[str] = mapped_column(String(16), default="active", nullable=False)
