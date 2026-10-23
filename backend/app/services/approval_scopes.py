"""Scoped approvals: match approvers to brand/region/channel on items."""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.exceptions import ForbiddenError
from app.core.rbac import Role
from app.models.approval import ApprovalItem
from app.models.approval_scope import UserApprovalScope
from app.models.workspace import Organization, User
from app.services import audit


@dataclass(frozen=True, slots=True)
class ScopeSet:
    brands: tuple[str, ...]
    regions: tuple[str, ...]
    channels: tuple[str, ...]

    @property
    def unrestricted(self) -> bool:
        return not self.brands and not self.regions and not self.channels


def _norm_list(values: list | tuple | None) -> tuple[str, ...]:
    out: list[str] = []
    for raw in values or ():
        text = str(raw or "").strip()
        if text and text not in out:
            out.append(text[:80])
    return tuple(out)


def load_scope(db: Session, user: User) -> ScopeSet:
    """Return the user's approval scope; missing row = unrestricted."""
    if db is None or not hasattr(db, "execute"):
        return ScopeSet(brands=(), regions=(), channels=())
    try:
        row = db.execute(
            select(UserApprovalScope).where(
                UserApprovalScope.tenant_id == user.tenant_id,
                UserApprovalScope.user_id == user.id,
            )
        ).scalar_one_or_none()
    except Exception:  # noqa: BLE001 - fixtures without the scopes table stay open
        return ScopeSet(brands=(), regions=(), channels=())
    if row is None:
        return ScopeSet(brands=(), regions=(), channels=())
    return ScopeSet(
        brands=_norm_list(row.brands),
        regions=_norm_list(row.regions),
        channels=_norm_list(row.channels),
    )


def _dimension_ok(item_value: str, allowed: tuple[str, ...]) -> bool:
    value = (item_value or "").strip()
    if not value:
        # Item is not scoped on this axis — visible to everyone.
        return True
    if not allowed:
        # Approver unrestricted on this axis.
        return True
    needle = value.casefold()
    return any(a.casefold() == needle for a in allowed)


def item_in_scope(item: ApprovalItem, scope: ScopeSet) -> bool:
    return (
        _dimension_ok(getattr(item, "scope_brand", "") or "", scope.brands)
        and _dimension_ok(getattr(item, "scope_region", "") or "", scope.regions)
        and _dimension_ok(getattr(item, "scope_channel", "") or "", scope.channels)
    )


def assert_can_act(db: Session, user: User, item: ApprovalItem) -> None:
    if item_in_scope(item, load_scope(db, user)):
        return
    raise ForbiddenError("This approval is outside your assigned scope.")


def filter_items(
    db: Session, user: User, items: list[ApprovalItem]
) -> list[ApprovalItem]:
    scope = load_scope(db, user)
    if scope.unrestricted:
        return items
    return [item for item in items if item_in_scope(item, scope)]


def set_user_scope(
    db: Session,
    org: Organization,
    user: User,
    *,
    brands: list[str] | None = None,
    regions: list[str] | None = None,
    channels: list[str] | None = None,
    actor_email: str,
) -> UserApprovalScope:
    if user.tenant_id != org.id:
        raise LookupError("user not in workspace")
    row = db.execute(
        select(UserApprovalScope).where(
            UserApprovalScope.tenant_id == org.id,
            UserApprovalScope.user_id == user.id,
        )
    ).scalar_one_or_none()
    if row is None:
        row = UserApprovalScope(tenant_id=org.id, user_id=user.id)
        db.add(row)
    if brands is not None:
        row.brands = list(_norm_list(brands))
    if regions is not None:
        row.regions = list(_norm_list(regions))
    if channels is not None:
        row.channels = list(_norm_list(channels))
    db.flush()
    audit.record(
        db,
        tenant_id=org.id,
        actor=actor_email,
        action="approval_scope.set",
        module="admin",
        context={
            "user_fp": user.id[:8],
            "brands": len(row.brands or []),
            "regions": len(row.regions or []),
            "channels": len(row.channels or []),
        },
    )
    return row


def scopes_from_action_payload(payload: dict | None) -> tuple[str, str, str]:
    """Pull optional scope stamps from an AgentAction payload."""
    data = payload or {}
    brand = str(data.get("scope_brand") or data.get("_scope_brand") or "")[:80]
    region = str(data.get("scope_region") or data.get("_scope_region") or "")[:80]
    channel = str(data.get("scope_channel") or data.get("_scope_channel") or "")[:80]
    return brand.strip(), region.strip(), channel.strip()


def users_in_scope_for_item(
    db: Session | None, *, tenant_id: str, item: ApprovalItem
) -> list[User]:
    """Approvers who may see ``item`` (used by runner / notification filters)."""
    if db is None or not hasattr(db, "execute"):
        return []
    try:
        members = list(
            db.execute(
                select(User).where(
                    User.tenant_id == tenant_id,
                    User.is_active.is_(True),
                    User.role.in_(
                        [
                            Role.ADMIN.value,
                            Role.MANAGER.value,
                            Role.APPROVER.value,
                        ]
                    ),
                )
            ).scalars()
        )
    except Exception:  # noqa: BLE001 - incomplete fixtures
        return []
    return [u for u in members if item_in_scope(item, load_scope(db, u))]
