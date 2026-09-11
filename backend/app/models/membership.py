"""Workspace membership — one email, many organisations.

Lives beside :class:`AuthIdentity` for the same reason: memberships are
resolved at login *before* a tenant is pinned, so the table cannot sit behind
row-level security. Each row is the join between a blind-indexed address and
one organisation's :class:`~app.models.workspace.User`.
"""
from __future__ import annotations

from sqlalchemy import Boolean, ForeignKey, Index, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import FK, PK, Base, TimestampMixin, new_id
from app.db.types import BlindIndex


class WorkspaceMembership(Base, TimestampMixin):
    """One person's seat in one workspace.

    An address may hold many of these; uniqueness is ``(email_index,
    tenant_id)``. Deactivating a member flips ``is_active`` on this row (and
    the tenant-scoped user) without wiping their other workspaces.
    """

    __tablename__ = "workspace_memberships"
    __table_args__ = (
        UniqueConstraint("email_index", "tenant_id", name="uq_membership_email_tenant"),
        Index("ix_membership_email", "email_index"),
        Index("ix_membership_tenant", "tenant_id"),
        Index("ix_membership_user", "user_id"),
    )

    id: Mapped[str] = mapped_column(PK, primary_key=True, default=new_id)
    email_index: Mapped[str] = mapped_column(BlindIndex, nullable=False)
    tenant_id: Mapped[str] = mapped_column(
        FK, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[str] = mapped_column(
        FK, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
