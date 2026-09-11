"""Platform-wide feature flags (Portal Admin).

Not tenant-scoped: one row controls the catalogue for every workspace.
Missing row means enabled (fail-open for newly shipped catalogue entries).
"""
from __future__ import annotations

from enum import StrEnum

from sqlalchemy import Boolean, String, UniqueConstraint
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
