"""Connectors marketplace — the catalogue, and connecting to it."""
from __future__ import annotations

from fastapi import APIRouter, Query

from app.api.deps import ClientIp, CurrentUserDep, DbSession
from app.connectors.base import registry
from app.core.rbac import Module
from app.core.rbac import Role
from app.schemas.common import ActionResult
from app.schemas.workspace import (
    ConnectorOut,
    ConnectRequest,
    ModelListOut,
    ModelListRequest,
    ModelOptionOut,
    OAuthChooseRequest,
    OAuthChoiceOut,
    OAuthStartOut,
    OAuthStartRequest,
)
from app.services import connectors as connector_service, oauth_flow, views

router = APIRouter(prefix="/connectors", tags=["connectors"])


def _connect_context(current, db, records=None) -> views.ConnectContext:  # noqa: ANN001
    """What the connect screen needs beyond one record: whether to show
    admin-only setup notes, and this workspace's existing sign-ins."""
    from app.connectors.base import oauth_providers

    records = records if records is not None else connector_service.list_records(
        db, tenant_id=current.tenant_id
    )
    not_connected: dict[str, list[tuple[str, str]]] = {}
    for record in records:
        spec = registry.get_spec(record.slug)
        if (
            spec is not None
            and spec.oauth is not None
            and not record.connected
            and oauth_providers.sign_in_available(spec)
        ):
            not_connected.setdefault(spec.oauth.provider, []).append((record.slug, spec.name))
    return views.ConnectContext(
        show_admin_notes=current.role is Role.ADMIN or current.is_portal_admin,
        is_portal_admin=current.is_portal_admin,
        accounts=oauth_flow.sibling_accounts(db, org=current.organization),
        not_connected=not_connected,
    )


def _require_write(current) -> None:  # noqa: ANN001
    if not current.can_write(Module.CONNECTORS):
        from app.core.exceptions import ForbiddenError

        # The exact wording the marketplace shows for a view-only role.
        raise ForbiddenError("View-only access — ask an admin to manage connectors")


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
        from app.connectors.base import registry as connector_registry

        def category_of(record) -> str:  # noqa: ANN001
            spec = connector_registry.get_spec(record.slug)
            return spec.category if spec else record.category

        all_records, records = records, [r for r in records if category_of(r) == category]
    else:
        all_records = records
    ctx = _connect_context(current, db, all_records)
    return [views.connector_out(record, can_write=can_write, ctx=ctx) for record in records]


@router.get("/categories", response_model=list[str])
def list_categories(current: CurrentUserDep) -> list[str]:
    """Filter chips for the marketplace, in display order."""
    current.require_view(Module.CONNECTORS)
    return ["All", *registry.categories()]


@router.get("/{slug}", response_model=ConnectorOut)
def get_connector(slug: str, current: CurrentUserDep, db: DbSession) -> ConnectorOut:
    current.require_view(Module.CONNECTORS)
    record = connector_service.get_record(db, tenant_id=current.tenant_id, slug=slug)
    return views.connector_out(
        record,
        can_write=current.can_write(Module.CONNECTORS),
        ctx=_connect_context(current, db),
    )


def _model_list_out(listing) -> ModelListOut:  # noqa: ANN001
    return ModelListOut(
        listable=listing.listable,
        models=[
            ModelOptionOut(id=m.id, label=m.label, created=m.created, kinds=list(m.kinds))
            for m in listing.models
        ],
        suggested=listing.suggested,
        source=listing.source,
        error=listing.error,
        stale=listing.stale,
        models_url=listing.models_url,
    )


@router.get("/{slug}/models", response_model=ModelListOut)
def list_models(slug: str, current: CurrentUserDep, db: DbSession) -> ModelListOut:
    """The models a connected AI integration's key can use, from the vendor."""
    current.require_view(Module.CONNECTORS)
    return _model_list_out(
        connector_service.model_listing(db, org=current.organization, slug=slug)
    )


@router.post("/{slug}/models", response_model=ModelListOut)
def list_models_before_saving(
    slug: str, payload: ModelListRequest, current: CurrentUserDep, db: DbSession
) -> ModelListOut:
    """The same list with the key just typed in the dialog, before saving.

    The key is used for this one call to the vendor; it is not stored,
    returned or logged.
    """
    _require_write(current)
    return _model_list_out(
        connector_service.model_listing(
            db, org=current.organization, slug=slug, values=payload.values
        )
    )


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
    return views.connector_out(record, can_write=True, ctx=_connect_context(current, db))


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


@router.post("/{slug}/request-sign-in", response_model=ActionResult)
def request_sign_in(
    slug: str, current: CurrentUserDep, db: DbSession, ip: ClientIp
) -> ActionResult:
    """"Notify my admin": a sign-in this person wants is not switched on.

    Recorded in the workspace's activity, which is what the header's
    notification bell shows, so the workspace's admins see who asked and
    for what. Once a day per person and integration is enough.
    """
    from datetime import timedelta

    from sqlalchemy import select

    from app.connectors.base import oauth_providers
    from app.db.base import utcnow
    from app.models.workspace import AuditLogEntry
    from app.schemas.common import Toast
    from app.services import audit

    current.require_view(Module.CONNECTORS)
    record = connector_service.get_record(db, tenant_id=current.tenant_id, slug=slug)
    spec = registry.get_spec(slug)
    provider = oauth_providers.provider_for(spec)
    if provider is None or oauth_providers.sign_in_available(spec):
        from app.core.exceptions import InvalidInputError

        raise InvalidInputError("This integration can already be connected here.")
    action = f"asked for Sign in with {provider.label} to be switched on, to connect {record.name}"
    asked_recently = db.execute(
        select(AuditLogEntry.id).where(
            AuditLogEntry.tenant_id == current.tenant_id,
            AuditLogEntry.actor_id == current.user.id,
            AuditLogEntry.action == action,
            AuditLogEntry.at > utcnow() - timedelta(days=1),
        ).limit(1)
    ).first()
    if asked_recently is None:
        audit.record_user_action(
            db,
            user=current.user,
            action=action,
            module="connectors",
            ip_address=ip,
            context={"connector": slug, "provider": provider.name, "request": "sign_in_setup"},
        )
        db.commit()
    return ActionResult(
        toast=Toast(
            message="Request sent. Your workspace admins will see it in their notifications.",
            kind="success",
        )
    )


# ── Sign-in ("Sign in with Google", WordPress approval) ────────────────────
@router.post("/{slug}/oauth/start", response_model=OAuthStartOut)
def oauth_start(
    slug: str, payload: OAuthStartRequest, current: CurrentUserDep, db: DbSession
) -> OAuthStartOut:
    """Where to send the browser to sign in. The state it carries is signed,
    expires in minutes and can be redeemed once."""
    _require_write(current)
    result = oauth_flow.start(
        db,
        org=current.organization,
        user=current.user,
        slug=slug,
        return_path=payload.return_path,
        site_url=payload.site_url,
        reuse=payload.reuse,
        inputs=payload.inputs,
    )
    db.commit()
    return OAuthStartOut(authorize_url=result.authorize_url, status=result.status)


@router.get("/{slug}/oauth/choices", response_model=list[OAuthChoiceOut])
def oauth_choices(slug: str, current: CurrentUserDep, db: DbSession) -> list[OAuthChoiceOut]:
    """What the signed-in account can reach: properties, sites, accounts."""
    _require_write(current)
    choices = oauth_flow.list_choices(db, org=current.organization, slug=slug)
    return [OAuthChoiceOut(id=c.id, label=c.label, detail=c.detail) for c in choices]


@router.post("/{slug}/oauth/choose", response_model=ConnectorOut)
def oauth_choose(
    slug: str,
    payload: OAuthChooseRequest,
    current: CurrentUserDep,
    db: DbSession,
    ip: ClientIp,
) -> ConnectorOut:
    """Save the pick and check the connection works."""
    _require_write(current)
    record = oauth_flow.choose(
        db,
        org=current.organization,
        actor=current.user,
        slug=slug,
        choice_id=payload.choice_id,
        ip_address=ip,
    )
    db.commit()
    return views.connector_out(record, can_write=True, ctx=_connect_context(current, db))


# There is no test endpoint. Credentials are verified when they are submitted
# — see app.services.connectors.verify_credentials — and re-probed by the
# scheduler's health sweep, so a connected integration's health is current
# without anybody asking for it. A manual probe was the thing that let a
# wrong token be saved and marked connected in the first place.
