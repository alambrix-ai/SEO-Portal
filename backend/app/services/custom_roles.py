"""Custom role CRUD and permission resolution."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.rbac import (
    Access,
    Module,
    Role,
    merge_access,
    validate_permission_map,
)
from app.models.enterprise_identity import CustomRole
from app.models.workspace import Organization, User
from app.services import audit

_ACCESS_RANK = {Access.NONE: 0, Access.VIEW: 1, Access.FULL: 2}


def assert_no_privilege_escalation(
    actor_access: dict[Module, Access],
    permissions: dict[str, str],
) -> None:
    """Refuse grants that exceed what the actor themselves holds."""
    for key, value in permissions.items():
        module = Module(str(key))
        wanted = Access(str(value).lower())
        held = actor_access.get(module, Access.NONE)
        if _ACCESS_RANK[wanted] > _ACCESS_RANK[held]:
            raise ValueError(
                f"cannot grant {module.value}={wanted.value}; "
                f"actor only holds {held.value}"
            )


def create_role(
    db: Session,
    org: Organization,
    *,
    slug: str,
    name: str,
    permissions: dict,
    description: str = "",
    actor_email: str,
    actor_access: dict[Module, Access] | None = None,
) -> CustomRole:
    slug = (slug or "").strip().lower().replace(" ", "-")[:64]
    if not slug:
        raise ValueError("slug required")
    name = (name or "").strip()[:120]
    if len(name) < 2:
        raise ValueError("name required")
    perms = validate_permission_map(permissions)
    if actor_access is not None:
        assert_no_privilege_escalation(actor_access, perms)
    existing = db.execute(
        select(CustomRole).where(
            CustomRole.tenant_id == org.id, CustomRole.slug == slug
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise ValueError("role slug already exists")
    row = CustomRole(
        tenant_id=org.id,
        slug=slug,
        name=name,
        description=(description or "")[:2000],
        permissions=perms,
        is_active=True,
    )
    db.add(row)
    db.flush()
    audit.record(
        db,
        tenant_id=org.id,
        actor=actor_email,
        action="custom_role.create",
        module="admin",
        context={"slug": slug},
    )
    return row


def resolve_user_access(user: User, db: Session) -> dict[Module, Access]:
    overlay = None
    if user.custom_role_id:
        role = db.get(CustomRole, user.custom_role_id)
        if role is not None and role.tenant_id == user.tenant_id and role.is_active:
            overlay = dict(role.permissions or {})
    return merge_access(user.role, overlay)


def assign_custom_role(
    db: Session, org: Organization, user: User, role_id: str | None, *, actor_email: str
) -> User:
    if user.tenant_id != org.id:
        raise LookupError("user not in workspace")
    if role_id:
        role = db.get(CustomRole, role_id)
        if role is None or role.tenant_id != org.id or not role.is_active:
            raise LookupError("custom role not found")
        user.custom_role_id = role.id
    else:
        user.custom_role_id = None
    db.flush()
    audit.record(
        db,
        tenant_id=org.id,
        actor=actor_email,
        action="custom_role.assign",
        module="admin",
        context={"user_id": user.id, "custom_role_id": user.custom_role_id},
    )
    return user
