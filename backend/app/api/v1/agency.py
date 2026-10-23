"""Agency tier: parent accounts, client links, white-label settings."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import CurrentUser, get_current_user, get_db
from app.core.exceptions import ForbiddenError, InvalidInputError, NotFoundError
from app.core.rbac import Module
from app.models.agency import Agency, AgencyClientLink, WhiteLabelSettings
from app.models.workspace import Organization
from app.services import agency as agency_svc

router = APIRouter(prefix="/agency", tags=["agency"])


class CreateAgencyIn(BaseModel):
    name: str
    slug: str


class AddMemberIn(BaseModel):
    email: str
    role: str = "member"


class LinkClientIn(BaseModel):
    client_tenant_id: str
    access_level: str = "view"


class WhiteLabelIn(BaseModel):
    custom_domain: str = ""
    email_from_name: str = ""
    branding: dict = Field(default_factory=dict)
    brand_safety: dict = Field(default_factory=dict)
    enabled: bool = False


def _agency_or_404(db: Session, agency_id: str) -> Agency:
    row = db.get(Agency, agency_id)
    if row is None or not row.is_active:
        raise NotFoundError("Agency not found")
    return row


@router.post("")
def create_agency(
    body: CreateAgencyIn,
    current: CurrentUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    current.require_write(Module.ADMIN)
    try:
        row = agency_svc.create_agency(
            db,
            name=body.name,
            slug=body.slug,
            owner_email=current.user.email,
            audit_tenant_id=current.tenant_id,
        )
        db.commit()
    except ValueError as exc:
        raise InvalidInputError("That request could not be completed.") from exc
    return {"id": row.id, "slug": row.slug, "name": row.name}


@router.post("/{agency_id}/members")
def add_member(
    agency_id: str,
    body: AddMemberIn,
    current: CurrentUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    current.require_write(Module.ADMIN)
    agency = _agency_or_404(db, agency_id)
    try:
        row = agency_svc.add_member(
            db,
            agency,
            email=body.email,
            role=body.role,
            actor_email=current.user.email,
            audit_tenant_id=current.tenant_id,
        )
        db.commit()
    except ValueError as exc:
        raise InvalidInputError("That request could not be completed.") from exc
    return {"id": row.id, "role": row.role, "is_active": row.is_active}


@router.post("/{agency_id}/clients")
def link_client(
    agency_id: str,
    body: LinkClientIn,
    current: CurrentUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    current.require_write(Module.ADMIN)
    agency = _agency_or_404(db, agency_id)
    client = db.get(Organization, body.client_tenant_id)
    if client is None:
        raise NotFoundError("Client workspace not found")
    try:
        row = agency_svc.link_client(
            db,
            agency,
            client,
            access_level=body.access_level,
            actor_email=current.user.email,
            audit_tenant_id=current.tenant_id,
        )
        db.commit()
    except ValueError as exc:
        raise InvalidInputError("That request could not be completed.") from exc
    return {
        "id": row.id,
        "client_tenant_id": row.client_tenant_id,
        "access_level": row.access_level,
    }


@router.get("/{agency_id}/clients")
def list_clients(
    agency_id: str,
    current: CurrentUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    current.require_view(Module.ADMIN)
    agency = _agency_or_404(db, agency_id)
    try:
        ids = agency_svc.list_client_tenant_ids(db, agency, email=current.user.email)
    except ValueError as exc:
        raise ForbiddenError("You do not have access to this agency.") from exc
    if not ids:
        return {"clients": []}
    links = list(
        db.execute(
            select(AgencyClientLink).where(
                AgencyClientLink.agency_id == agency.id,
                AgencyClientLink.client_tenant_id.in_(ids),
            )
        ).scalars()
    )
    return {
        "clients": [
            {
                "id": link.id,
                "client_tenant_id": link.client_tenant_id,
                "access_level": link.access_level,
            }
            for link in links
        ]
    }


@router.put("/{agency_id}/white-label")
def upsert_white_label(
    agency_id: str,
    body: WhiteLabelIn,
    current: CurrentUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    current.require_write(Module.ADMIN)
    agency = _agency_or_404(db, agency_id)
    try:
        row = agency_svc.upsert_white_label(
            db,
            agency,
            current.cipher,
            custom_domain=body.custom_domain,
            email_from_name=body.email_from_name,
            branding=body.branding,
            brand_safety=body.brand_safety,
            enabled=body.enabled,
            actor_email=current.user.email,
            audit_tenant_id=current.tenant_id,
        )
        db.commit()
    except ValueError as exc:
        raise InvalidInputError("That request could not be completed.") from exc
    return {
        "id": row.id,
        "custom_domain": row.custom_domain,
        "email_from_name": row.email_from_name,
        "enabled": row.enabled,
    }


@router.get("/{agency_id}/white-label")
def get_white_label(
    agency_id: str,
    current: CurrentUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    current.require_view(Module.ADMIN)
    agency = _agency_or_404(db, agency_id)
    try:
        agency_svc.list_client_tenant_ids(db, agency, email=current.user.email)
    except ValueError as exc:
        raise ForbiddenError("You do not have access to this agency.") from exc
    row = db.execute(
        select(WhiteLabelSettings).where(WhiteLabelSettings.agency_id == agency.id)
    ).scalar_one_or_none()
    if row is None:
        return {"enabled": False, "custom_domain": "", "email_from_name": "", "branding": {}}
    branding = agency_svc.decrypt_branding(current.cipher, row)
    return {
        "id": row.id,
        "custom_domain": row.custom_domain,
        "email_from_name": row.email_from_name,
        "enabled": row.enabled,
        "brand_safety": row.brand_safety,
        "branding": branding,
    }
