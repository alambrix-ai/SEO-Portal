"""Platform Portal Admin — workspaces, users, and catalogue feature flags.

Gated by PORTAL_ADMIN_EMAILS. Workspace Super Admin (/admin) is a different
surface and cannot reach these routes.
"""
from __future__ import annotations

from fastapi import APIRouter
from pydantic import Field
from sqlalchemy import func, select

from app.api.deps import ClientIp, DbSession, PortalAdminDep
from app.core.exceptions import InvalidInputError, NotFoundError
from app.core.rbac import ROLE_LABELS, Role
from app.models.portal import FeatureKind
from app.models.workspace import Organization, User
from app.schemas.common import ActionResult, ApiModel, Toast
from app.services import audit, portal_features

router = APIRouter(prefix="/portal", tags=["portal"])


class PortalOverviewOut(ApiModel):
    organizations: int
    users: int
    features_total: int
    features_disabled: int


class PortalOrganizationOut(ApiModel):
    id: str
    name: str
    slug: str
    plan_name: str
    plan_tier: str
    seats_total: int
    member_count: int
    is_active: bool
    created_at: str


class PortalUserOut(ApiModel):
    id: str
    name: str
    email: str
    role: str
    role_label: str
    is_owner: bool
    is_active: bool
    last_login_at: str | None = None


class PortalFeatureOut(ApiModel):
    kind: str
    slug: str
    name: str
    enabled: bool


class PortalFeatureUpdate(ApiModel):
    enabled: bool = Field(...)


@router.get("/overview", response_model=PortalOverviewOut)
def overview(current: PortalAdminDep, db: DbSession) -> PortalOverviewOut:
    org_count = db.execute(select(func.count()).select_from(Organization)).scalar_one()
    # Users are RLS-scoped; count via per-org walk.
    from app.db.session import run_per_organization

    totals = {"users": 0}

    def count_users(session, tenant_id: str) -> int:  # noqa: ANN001
        return session.execute(
            select(func.count())
            .select_from(User)
            .where(User.tenant_id == tenant_id)
        ).scalar_one()

    for n in run_per_organization(db, count_users, label="Portal user count"):
        totals["users"] += n

    flags = portal_features.list_flags(db)
    disabled = sum(1 for f in flags if not f["enabled"])
    return PortalOverviewOut(
        organizations=int(org_count),
        users=totals["users"],
        features_total=len(flags),
        features_disabled=disabled,
    )


@router.get("/organizations", response_model=list[PortalOrganizationOut])
def list_organizations(current: PortalAdminDep, db: DbSession) -> list[PortalOrganizationOut]:
    from app.db.session import bind_tenant

    orgs = list(
        db.execute(select(Organization).order_by(Organization.created_at.desc())).scalars()
    )
    out: list[PortalOrganizationOut] = []
    for org in orgs:
        bind_tenant(db, org.id)
        member_count = db.execute(
            select(func.count())
            .select_from(User)
            .where(User.tenant_id == org.id, User.is_active.is_(True))
        ).scalar_one()
        out.append(
            PortalOrganizationOut(
                id=org.id,
                name=org.name,
                slug=org.slug,
                plan_name=org.plan_name,
                plan_tier=org.plan_tier,
                seats_total=org.seats_total,
                member_count=int(member_count),
                is_active=org.is_active,
                created_at=org.created_at.isoformat() if org.created_at else "",
            )
        )
    # Restore the caller's tenant pin for the rest of the request.
    bind_tenant(db, current.tenant_id)
    return out


@router.get("/organizations/{org_id}/users", response_model=list[PortalUserOut])
def list_organization_users(
    org_id: str, current: PortalAdminDep, db: DbSession
) -> list[PortalUserOut]:
    from app.db.session import bind_tenant

    org = db.get(Organization, org_id)
    if org is None:
        raise NotFoundError("That workspace was not found")

    bind_tenant(db, org_id)
    members = list(
        db.execute(
            select(User)
            .where(User.tenant_id == org_id)
            .order_by(User.is_owner.desc(), User.created_at)
        ).scalars()
    )
    bind_tenant(db, current.tenant_id)

    return [
        PortalUserOut(
            id=m.id,
            name=m.name,
            email=m.email,
            role=m.role,
            role_label=ROLE_LABELS.get(Role(m.role), m.role),
            is_owner=m.is_owner,
            is_active=m.is_active,
            last_login_at=m.last_login_at.isoformat() if m.last_login_at else None,
        )
        for m in members
    ]


@router.get("/features", response_model=list[PortalFeatureOut])
def list_features(current: PortalAdminDep, db: DbSession) -> list[PortalFeatureOut]:
    return [PortalFeatureOut(**row) for row in portal_features.list_flags(db)]


@router.put("/features/{kind}/{slug}", response_model=ActionResult)
def update_feature(
    kind: str,
    slug: str,
    payload: PortalFeatureUpdate,
    current: PortalAdminDep,
    db: DbSession,
    ip: ClientIp,
) -> ActionResult:
    if kind not in {FeatureKind.CONNECTOR.value, FeatureKind.AGENT.value, FeatureKind.MODULE.value}:
        raise InvalidInputError("Feature kind must be connector, agent, or module")

    try:
        portal_features.set_flag(
            db,
            kind=kind,
            slug=slug,
            enabled=payload.enabled,
            actor=current.user,
        )
    except ValueError as exc:
        raise InvalidInputError(str(exc)) from exc

    state = "enabled" if payload.enabled else "disabled"
    audit.record(
        db,
        tenant_id=current.tenant_id,
        actor=current.user.email or current.user.name,
        actor_id=current.user.id,
        action=f"portal {state} {kind}/{slug}",
        module="portal",
        ip_address=ip,
        context={"kind": kind, "slug": slug, "enabled": payload.enabled},
    )
    db.commit()
    return ActionResult(
        toast=Toast(
            message=f"{slug} {state}",
            kind="success" if payload.enabled else "warning",
        )
    )
