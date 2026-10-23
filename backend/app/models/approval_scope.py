"""Per-user approval scopes (brand / region / channel)."""
from __future__ import annotations

from sqlalchemy import Index, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import FK, PK, Base, TenantScopedMixin, TimestampMixin, new_id


class UserApprovalScope(Base, TimestampMixin, TenantScopedMixin):
    """Which brand/region/channel values an approver may see and act on.

    Empty lists mean unrestricted for that dimension. An item matches when
    every non-empty scope field on the item is either unrestricted for the
    user or listed in their allow-set.
    """

    __tablename__ = "user_approval_scopes"
    __table_args__ = (
        UniqueConstraint("tenant_id", "user_id", name="uq_user_approval_scope"),
        Index("ix_user_approval_scopes_user_id", "user_id"),
    )

    id: Mapped[str] = mapped_column(PK, primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(FK, nullable=False)
    brands: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    regions: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    channels: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
