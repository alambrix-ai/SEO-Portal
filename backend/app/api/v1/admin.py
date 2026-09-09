"""Admin — team and role-based access, invitations, billing, audit log."""
from __future__ import annotations

from fastapi import APIRouter, Query
from sqlalchemy import func, select

from app.api.deps import ClientIp, CurrentUserDep, DbSession
from app.core.rbac import ROLE_LABELS, Module, Role
from app.models.workspace import Invitation, User
from app.schemas.auth import InviteRequest, InvitationOut
from app.schemas.common import ActionResult, Toast
from app.schemas.workspace import (
    AdminOut,
    AuditEntryOut,
    BillingOut,
    RoleChangeRequest,
    TeamMemberOut,
)
from app.services import audit as audit_service
from app.services import auth as auth_service
from app.services import views

router = APIRouter(prefix="/admin", tags=["admin"])


def _team(db: DbSession, tenant_id: str) -> list[User]:
    return list(
        db.execute(
            select(User)
            .where(User.tenant_id == tenant_id)
            # Owner first, then by role, so the list reads as a hierarchy.
            .order_by(User.is_owner.desc(), User.role, User.created_at)
        ).scalars()
    )


def _invitations(db: DbSession, tenant_id: str) -> list[Invitation]:
    return list(
        db.execute(
            select(Invitation)
            .where(
                Invitation.tenant_id == tenant_id,
                *auth_service.outstanding_invitation_clauses(),
            )
            .order_by(Invitation.created_at.desc())
        ).scalars()
    )


@router.get("", response_model=AdminOut)
def admin_overview(
    current: CurrentUserDep,
    db: DbSession,
    search: str = Query(default="", max_length=200),
) -> AdminOut:
    current.require_view(Module.ADMIN)
    can_write = current.can_write(Module.ADMIN)

    members = _team(db, current.tenant_id)
    active = sum(1 for m in members if m.is_active)
    org = current.organization
    percent = round((active / org.seats_total) * 100) if org.seats_total else 0

    return AdminOut(
        team=[
            views.team_member_out(m, actor=current.user, can_write=can_write)
            for m in members
        ],
        invitations=[
            InvitationOut(
                id=inv.id,
                email=inv.email,
                role=Role(inv.role),
                role_label=ROLE_LABELS[Role(inv.role)],
                expires_at=inv.expires_at,
                accepted_at=inv.accepted_at,
            ).model_dump()
            for inv in _invitations(db, current.tenant_id)
        ],
        audit=[
            views.audit_out(entry)
            for entry in audit_service.list_entries(
                db, tenant_id=current.tenant_id, search=search, limit=200
            )
        ],
        billing=BillingOut(
            plan_name=org.plan_name,
            seats_total=org.seats_total,
            seats_used=active,
            seats_percent=min(percent, 100),
            renews_on=org.renews_on,
        ),
        role_options=views.role_options(),
        read_only=not can_write,
    )


@router.get("/audit", response_model=list[AuditEntryOut])
def audit_log(
    current: CurrentUserDep,
    db: DbSession,
    search: str = Query(default="", max_length=200),
    limit: int = Query(default=200, ge=1, le=1000),
) -> list[AuditEntryOut]:
    """Filterable audit log, for the panel's own search box."""
    current.require_view(Module.ADMIN)
    return [
        views.audit_out(entry)
        for entry in audit_service.list_entries(
            db, tenant_id=current.tenant_id, search=search, limit=limit
        )
    ]


# ── Team management ────────────────────────────────────────────────────────
@router.put("/team/{member_id}/role", response_model=TeamMemberOut)
def change_role(
    member_id: str,
    payload: RoleChangeRequest,
    current: CurrentUserDep,
    db: DbSession,
) -> TeamMemberOut:
    """Change a teammate's role.

    Their live sessions are revoked, so the new permissions take effect on
    their next request rather than whenever their token happens to expire.
    """
    current.require_write(Module.ADMIN)
    member = auth_service.change_member_role(
        db,
        org=current.organization,
        actor=current.user,
        member_id=member_id,
        role=payload.role,
    )
    db.commit()
    return views.team_member_out(member, actor=current.user, can_write=True)


@router.post("/team/{member_id}/deactivate", response_model=ActionResult)
def deactivate(
    member_id: str, current: CurrentUserDep, db: DbSession
) -> ActionResult:
    current.require_write(Module.ADMIN)
    member = auth_service.deactivate_member(
        db, org=current.organization, actor=current.user, member_id=member_id
    )
    db.commit()
    return ActionResult(toast=Toast(message=f"{member.name} deactivated"))


# ── Invitations ────────────────────────────────────────────────────────────
@router.post("/invitations", response_model=InvitationOut)
def invite(
    payload: InviteRequest, current: CurrentUserDep, db: DbSession
) -> InvitationOut:
    """Invite a teammate. The link proves their address; they never set a password."""
    current.require_write(Module.ADMIN)
    invitation = auth_service.invite_member(
        db,
        org=current.organization,
        inviter=current.user,
        email=str(payload.email),
        role=payload.role,
    )
    db.commit()
    role = Role(invitation.role)
    return InvitationOut(
        id=invitation.id,
        email=invitation.email,
        role=role,
        role_label=ROLE_LABELS[role],
        expires_at=invitation.expires_at,
        accepted_at=None,
    )


@router.delete("/invitations/{invitation_id}", response_model=ActionResult)
def revoke_invitation(
    invitation_id: str, current: CurrentUserDep, db: DbSession
) -> ActionResult:
    current.require_write(Module.ADMIN)
    auth_service.revoke_invitation(
        db,
        org=current.organization,
        invitation_id=invitation_id,
        actor=current.user,
    )
    db.commit()
    return ActionResult(toast=Toast(message="Invitation revoked"))
