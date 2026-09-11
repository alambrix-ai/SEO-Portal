"""Multi-workspace: list, create, and switch organisations for one email."""
from __future__ import annotations

from fastapi import APIRouter, Request

from app.api.deps import ClientIp, CurrentUserDep, DbSession
from app.api.v1.auth import _auth_response
from app.db.session import bind_tenant
from app.schemas.auth import (
    AuthResponse,
    CreateWorkspaceRequest,
    WorkspaceCardOut,
)
from app.core.rbac import Role
from app.services import auth as auth_service

router = APIRouter(prefix="/workspaces", tags=["workspaces"])


@router.get("", response_model=list[WorkspaceCardOut])
def list_workspaces(db: DbSession, current: CurrentUserDep) -> list[WorkspaceCardOut]:
    cards = auth_service.list_workspace_cards(
        db,
        email_index_value=current.user.email_index,
        current_tenant_id=current.organization.id,
    )
    return [
        WorkspaceCardOut(
            id=card.id,
            name=card.name,
            slug=card.slug,
            primary_domain=card.primary_domain,
            role=Role(card.role),
            role_label=card.role_label,
            onboarding_complete=card.onboarding_complete,
            is_current=card.is_current,
            is_owner=card.is_owner,
        )
        for card in cards
    ]


@router.post("", response_model=AuthResponse)
def create_workspace(
    payload: CreateWorkspaceRequest,
    db: DbSession,
    current: CurrentUserDep,
    request: Request,
    ip: ClientIp,
) -> AuthResponse:
    """Create another organisation and switch the session onto it."""
    result = auth_service.create_workspace(
        db,
        actor=current.user,
        name=payload.name,
        primary_domain=payload.primary_domain,
        user_agent=request.headers.get("user-agent", ""),
        ip_address=ip,
    )
    db.commit()
    bind_tenant(db, result.organization.id)
    return _auth_response(db, result)


@router.post("/{organization_id}/switch", response_model=AuthResponse)
def switch_workspace(
    organization_id: str,
    db: DbSession,
    current: CurrentUserDep,
    request: Request,
    ip: ClientIp,
) -> AuthResponse:
    result = auth_service.switch_workspace(
        db,
        actor=current.user,
        organization_id=organization_id,
        user_agent=request.headers.get("user-agent", ""),
        ip_address=ip,
    )
    db.commit()
    bind_tenant(db, result.organization.id)
    return _auth_response(db, result)


@router.delete("/{organization_id}", response_model=AuthResponse)
def delete_workspace(
    organization_id: str,
    db: DbSession,
    current: CurrentUserDep,
    request: Request,
    ip: ClientIp,
) -> AuthResponse:
    """Soft-delete a workspace. Session moves to another seat if needed."""
    result = auth_service.delete_workspace(
        db,
        actor=current.user,
        organization_id=organization_id,
        user_agent=request.headers.get("user-agent", ""),
        ip_address=ip,
    )
    db.commit()
    bind_tenant(db, result.organization.id)
    return _auth_response(db, result)
