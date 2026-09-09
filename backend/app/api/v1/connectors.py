"""Connectors marketplace — the catalogue, and connecting to it."""
from __future__ import annotations

from fastapi import APIRouter, Query

from app.api.deps import ClientIp, CurrentUserDep, DbSession
from app.connectors.base import registry
from app.core.rbac import Module
from app.schemas.common import ActionResult, Toast
from app.schemas.workspace import ConnectorOut, ConnectRequest
from app.services import connectors as connector_service, views

router = APIRouter(prefix="/connectors", tags=["connectors"])


@router.get("", response_model=list[ConnectorOut])
def list_connectors(
    current: CurrentUserDep,
    db: DbSession,
    category: str = Query(default="", max_length=64),
) -> list[ConnectorOut]:
    current.require_view(Module.CONNECTORS)
    can_write = current.can_write(Module.CONNECTORS)

    records = connector_service.list_records(db, tenant_id=current.tenant_id)
    if category and category.lower() != "all":
        records = [r for r in records if r.category == category]
    return [views.connector_out(record, can_write=can_write) for record in records]


@router.get("/categories", response_model=list[str])
def list_categories(current: CurrentUserDep) -> list[str]:
    """Filter chips for the marketplace, in display order."""
    current.require_view(Module.CONNECTORS)
    return ["All", *registry.categories()]


@router.get("/{slug}", response_model=ConnectorOut)
def get_connector(slug: str, current: CurrentUserDep, db: DbSession) -> ConnectorOut:
    current.require_view(Module.CONNECTORS)
    record = connector_service.get_record(db, tenant_id=current.tenant_id, slug=slug)
    return views.connector_out(record, can_write=current.can_write(Module.CONNECTORS))


@router.post("/{slug}/connect", response_model=ConnectorOut)
def connect(
    slug: str,
    payload: ConnectRequest,
    current: CurrentUserDep,
    db: DbSession,
    ip: ClientIp,
) -> ConnectorOut:
    """Store credentials and mark the integration live.

    Secrets are encrypted under this organisation's own data key before they
    reach the database, and are never returned by any endpoint.
    """
    if not current.can_write(Module.CONNECTORS):
        from app.core.exceptions import ForbiddenError

        # The exact wording the marketplace shows for a view-only role.
        raise ForbiddenError("View-only access — ask an admin to manage connectors")

    record = connector_service.connect(
        db,
        org=current.organization,
        actor=current.user,
        slug=slug,
        values=payload.values,
        oauth_completed=payload.oauth_completed,
        ip_address=ip,
    )
    db.commit()
    return views.connector_out(record, can_write=True)


@router.post("/{slug}/disconnect", response_model=ConnectorOut)
def disconnect(
    slug: str, current: CurrentUserDep, db: DbSession, ip: ClientIp
) -> ConnectorOut:
    if not current.can_write(Module.CONNECTORS):
        from app.core.exceptions import ForbiddenError

        raise ForbiddenError("View-only access — ask an admin to manage connectors")

    record = connector_service.disconnect(
        db, org=current.organization, actor=current.user, slug=slug, ip_address=ip
    )
    db.commit()
    return views.connector_out(record, can_write=True)


# There is no test endpoint. Credentials are verified when they are submitted
# — see app.services.connectors.verify_credentials — and re-probed by the
# scheduler's health sweep, so a connected integration's health is current
# without anybody asking for it. A manual probe was the thing that let a
# wrong token be saved and marked connected in the first place.
