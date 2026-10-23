"""Enterprise identity admin: SSO, SCIM tokens, custom roles, IP allowlists."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from sqlalchemy import select

from app.api.deps import CurrentUser, client_ip as resolve_client_ip, get_current_user, get_db
from app.core.exceptions import InvalidInputError, NotFoundError
from app.core.rbac import Module
from app.models.enterprise_identity import CustomRole, IpAllowlistEntry, SsoConnection
from app.services import custom_roles, enterprise_sso, ip_allowlist, mfa as mfa_service, scim


def _step_up(current: CurrentUser) -> None:
    mfa_service.require_step_up(current.claims, current.organization, current.user)

router = APIRouter(prefix="/enterprise", tags=["enterprise"])


class SsoUpsertIn(BaseModel):
    slug: str
    name: str
    protocol: str
    issuer: str = ""
    metadata_url: str = ""
    default_role: str = "client"
    subject_claim: str = "sub"
    enabled: bool = False
    jit_provision: bool = True
    client_secret: str | None = None
    idp_cert_pem: str | None = None


class ScimMintIn(BaseModel):
    label: str = "SCIM token"


class CustomRoleIn(BaseModel):
    slug: str
    name: str
    description: str = ""
    permissions: dict[str, str] = Field(default_factory=dict)


class IpEntryIn(BaseModel):
    cidr: str
    label: str = ""


class IpToggleIn(BaseModel):
    enabled: bool


@router.get("/sso")
def list_sso(
    current: CurrentUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    current.require_view(Module.ADMIN)
    rows = list(
        db.execute(
            select(SsoConnection).where(SsoConnection.tenant_id == current.tenant_id)
        ).scalars()
    )
    return {
        "connections": [
            {
                "id": r.id,
                "slug": r.slug,
                "name": r.name,
                "protocol": r.protocol,
                "issuer": r.issuer,
                "enabled": r.enabled,
                "jit_provision": r.jit_provision,
                "default_role": r.default_role,
            }
            for r in rows
        ]
    }


@router.post("/sso")
def upsert_sso(
    body: SsoUpsertIn,
    request: Request,
    current: CurrentUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    current.require_write(Module.ADMIN)
    _step_up(current)
    secrets = {}
    if body.client_secret:
        secrets["client_secret"] = body.client_secret
    if body.idp_cert_pem:
        secrets["idp_cert_pem"] = body.idp_cert_pem
    try:
        row = enterprise_sso.upsert_connection(
            db,
            current.organization,
            current.cipher,
            slug=body.slug,
            name=body.name,
            protocol=body.protocol,
            issuer=body.issuer,
            metadata_url=body.metadata_url,
            default_role=body.default_role,
            subject_claim=body.subject_claim,
            secrets=secrets or None,
            actor_email=current.user.email,
            enabled=body.enabled,
            jit_provision=body.jit_provision,
        )
    except ValueError as exc:
        raise InvalidInputError("That request could not be completed.") from exc
    db.commit()
    return {"id": row.id, "slug": row.slug, "enabled": row.enabled}


@router.post("/scim/tokens")
def mint_scim_token(
    body: ScimMintIn,
    current: CurrentUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    current.require_write(Module.ADMIN)
    _step_up(current)
    minted = scim.mint_token(
        db,
        current.organization,
        label=body.label,
        created_by_id=current.user.id,
        actor_email=current.user.email,
    )
    db.commit()
    # Raw token returned once — never logged.
    return {
        "id": minted.row.id,
        "label": minted.row.label,
        "token": minted.raw_token,
        "prefix": minted.row.token_prefix,
    }


@router.delete("/scim/tokens/{token_id}")
def revoke_scim_token(
    token_id: str,
    current: CurrentUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    current.require_write(Module.ADMIN)
    _step_up(current)
    try:
        scim.revoke_token(
            db, current.organization, token_id, actor_email=current.user.email
        )
    except LookupError as exc:
        raise NotFoundError("Not found.") from exc
    db.commit()
    return {"revoked": True}


@router.post("/roles")
def create_custom_role(
    body: CustomRoleIn,
    current: CurrentUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    current.require_write(Module.ADMIN)
    _step_up(current)
    try:
        row = custom_roles.create_role(
            db,
            current.organization,
            slug=body.slug,
            name=body.name,
            permissions=body.permissions,
            description=body.description,
            actor_email=current.user.email,
            actor_access=current.effective_access,
        )
    except ValueError as exc:
        raise InvalidInputError("That request could not be completed.") from exc
    db.commit()
    return {"id": row.id, "slug": row.slug, "permissions": row.permissions}


@router.get("/roles")
def list_custom_roles(
    current: CurrentUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    current.require_view(Module.ADMIN)
    rows = list(
        db.execute(
            select(CustomRole).where(CustomRole.tenant_id == current.tenant_id)
        ).scalars()
    )
    return {
        "roles": [
            {
                "id": r.id,
                "slug": r.slug,
                "name": r.name,
                "permissions": r.permissions,
                "is_active": r.is_active,
            }
            for r in rows
        ]
    }


@router.post("/ip-allowlist")
def add_ip(
    body: IpEntryIn,
    current: CurrentUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    current.require_write(Module.ADMIN)
    _step_up(current)
    try:
        row = ip_allowlist.add_entry(
            db,
            current.organization,
            cidr=body.cidr,
            label=body.label,
            actor_email=current.user.email,
        )
    except ValueError as exc:
        raise InvalidInputError("That request could not be completed.") from exc
    db.commit()
    return {"id": row.id, "cidr": row.cidr}


@router.post("/ip-allowlist/toggle")
def toggle_ip(
    body: IpToggleIn,
    request: Request,
    current: CurrentUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    current.require_write(Module.ADMIN)
    _step_up(current)
    actor_ip = resolve_client_ip(request) or None
    try:
        org = ip_allowlist.set_enabled(
            db,
            current.organization,
            enabled=body.enabled,
            actor_email=current.user.email,
            actor_ip=actor_ip,
        )
    except InvalidInputError:
        raise
    db.commit()
    return {"ip_allowlist_enabled": org.ip_allowlist_enabled}


@router.get("/ip-allowlist")
def list_ip(
    current: CurrentUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    current.require_view(Module.ADMIN)
    rows = list(
        db.execute(
            select(IpAllowlistEntry).where(
                IpAllowlistEntry.tenant_id == current.tenant_id
            )
        ).scalars()
    )
    return {
        "enabled": current.organization.ip_allowlist_enabled,
        "entries": [{"id": r.id, "cidr": r.cidr, "label": r.label, "enabled": r.enabled} for r in rows],
    }
