"""Platform Portal Admin — workspaces, users, and catalogue feature flags.

Gated by PORTAL_ADMIN_EMAILS. Workspace Super Admin (/admin) is a different
surface and cannot reach these routes.
"""
from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter
from fastapi import Path as PathParam
from pydantic import Field
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError

from app.api.deps import ClientIp, DbSession, PortalAdminDep
from app.core.exceptions import InvalidInputError, NotFoundError
from app.core.logging import get_logger
from app.core.rbac import ROLE_LABELS, Role
from app.db.base import utcnow
from app.models.portal import FeatureKind
from app.models.workspace import Organization, User
from app.schemas.common import ActionResult, ApiModel, Toast
from app.services import audit, portal_features
from app.services.creative.quotas import CreativeLimitsUpdate

log = get_logger(__name__)

router = APIRouter(prefix="/portal", tags=["portal"])


class PortalOverviewOut(ApiModel):
    organizations: int
    #: Seats: one row per person per workspace.
    users: int
    #: Unique people (same email across workspaces counts once).
    accounts: int
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


class PortalAccountSeatOut(ApiModel):
    """One person's membership in one customer workspace."""

    organization_id: str
    organization_name: str
    organization_slug: str
    plan_name: str
    plan_tier: str
    organization_active: bool
    user_id: str
    role: str
    role_label: str
    is_owner: bool
    is_active: bool
    last_login_at: str | None = None
    sso_provider: str = ""
    locked: bool = False


class PortalAccountOut(ApiModel):
    """One person on the platform, with every workspace they belong to.

    Grouped by the email blind index so the same address is one tree node.
    The index itself is never sent to the browser.
    """

    email: str
    name: str
    workspace_count: int
    last_login_at: str | None = None
    sso_provider: str = ""
    is_locked: bool = False
    seats: list[PortalAccountSeatOut] = Field(default_factory=list)


class PortalFeatureOut(ApiModel):
    kind: str
    slug: str
    name: str
    enabled: bool


class PortalFeatureUpdate(ApiModel):
    enabled: bool = Field(...)


def _role_label(role: str) -> str:
    try:
        return ROLE_LABELS.get(Role(role), role)
    except ValueError:
        return role


def _seat_locked(member: User) -> bool:
    until = member.locked_until
    return bool(until and until > utcnow())


def collect_accounts(db, restore_tenant_id: str) -> list[PortalAccountOut]:  # noqa: ANN001
    """Walk every workspace and group members by email.

    Users are tenant-scoped, so each organisation is pinned in turn. Fields
    that decrypt under the org key are copied into plain models before the
    identity map is cleared. The email blind index is the grouping key and
    never leaves this function.
    """
    from app.db.session import bind_tenant

    org_rows = [
        {
            "id": org.id,
            "name": org.name,
            "slug": org.slug,
            "plan_name": org.plan_name,
            "plan_tier": org.plan_tier,
            "is_active": org.is_active,
        }
        for org in db.execute(select(Organization).order_by(Organization.name)).scalars()
    ]

    grouped: dict[str, dict[str, Any]] = {}
    for org in org_rows:
        bind_tenant(db, org["id"])
        members = list(
            db.execute(select(User).where(User.tenant_id == org["id"])).scalars()
        )
        for member in members:
            seat = PortalAccountSeatOut(
                organization_id=org["id"],
                organization_name=org["name"],
                organization_slug=org["slug"],
                plan_name=org["plan_name"],
                plan_tier=org["plan_tier"],
                organization_active=org["is_active"],
                user_id=member.id,
                role=member.role,
                role_label=_role_label(member.role),
                is_owner=member.is_owner,
                is_active=member.is_active,
                last_login_at=member.last_login_at.isoformat() if member.last_login_at else None,
                sso_provider=member.sso_provider or "",
                locked=_seat_locked(member),
            )
            bucket = grouped.setdefault(
                member.email_index,
                {"email": member.email, "name": member.name, "seats": []},
            )
            bucket["email"] = member.email
            if member.name:
                bucket["name"] = member.name
            bucket["seats"].append(seat)
        db.expunge_all()

    bind_tenant(db, restore_tenant_id)

    accounts: list[PortalAccountOut] = []
    for bucket in grouped.values():
        seats = sorted(
            bucket["seats"],
            key=lambda seat: seat.organization_name.lower(),
        )
        logins = [seat.last_login_at for seat in seats if seat.last_login_at]
        sso = next((seat.sso_provider for seat in seats if seat.sso_provider), "")
        accounts.append(
            PortalAccountOut(
                email=bucket["email"],
                name=bucket["name"] or bucket["email"],
                workspace_count=len(seats),
                last_login_at=max(logins) if logins else None,
                sso_provider=sso,
                is_locked=any(seat.locked for seat in seats),
                seats=seats,
            )
        )
    accounts.sort(key=lambda account: (account.name.lower(), account.email.lower()))
    return accounts


@router.get("/overview", response_model=PortalOverviewOut)
def overview(current: PortalAdminDep, db: DbSession) -> PortalOverviewOut:
    org_count = db.execute(select(func.count()).select_from(Organization)).scalar_one()
    from app.db.session import bind_tenant

    indexes: set[str] = set()
    seats = 0
    org_ids = list(db.execute(select(Organization.id)).scalars())
    for org_id in org_ids:
        bind_tenant(db, org_id)
        rows = list(
            db.execute(select(User.email_index).where(User.tenant_id == org_id)).scalars()
        )
        seats += len(rows)
        indexes.update(rows)
        db.expunge_all()
    bind_tenant(db, current.tenant_id)

    flags = portal_features.list_flags(db)
    disabled = sum(1 for f in flags if not f["enabled"])
    return PortalOverviewOut(
        organizations=int(org_count),
        users=seats,
        accounts=len(indexes),
        features_total=len(flags),
        features_disabled=disabled,
    )


@router.get("/accounts", response_model=list[PortalAccountOut])
def list_accounts(current: PortalAdminDep, db: DbSession) -> list[PortalAccountOut]:
    return collect_accounts(db, current.tenant_id)


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


class CreativeLimitsOut(ApiModel):
    """Creative Studio's monthly limits for one workspace. 0 = no cap."""

    organization_id: str
    #: Platform defaults from server settings.
    defaults: dict[str, int]
    #: Values a portal admin set for this workspace only.
    overrides: dict[str, int]
    #: What applies now: each override, else the default.
    effective: dict[str, int]
    #: This calendar month (UTC): jobs, images, tokens.
    usage: dict[str, int]
    toast: Toast | None = None


OrgIdPath = Annotated[str, PathParam(min_length=1, max_length=64)]


def _creative_limits(db, org: Organization, current) -> CreativeLimitsOut:  # noqa: ANN001
    from app.db.session import bind_tenant
    from app.services.creative import quotas

    bind_tenant(db, org.id)
    try:
        used = quotas.usage(db, org.id)
    finally:
        bind_tenant(db, current.tenant_id)
    return CreativeLimitsOut(
        organization_id=org.id,
        defaults=quotas.defaults().as_dict(),
        overrides=quotas.overrides_in(org.settings),
        effective=quotas.limits_from(org.settings).as_dict(),
        usage=used.as_dict(),
    )


@router.get("/organizations/{org_id}/creative-limits", response_model=CreativeLimitsOut)
def read_creative_limits(
    org_id: OrgIdPath, current: PortalAdminDep, db: DbSession
) -> CreativeLimitsOut:
    org = db.get(Organization, org_id)
    if org is None:
        raise NotFoundError("That workspace was not found")
    return _creative_limits(db, org, current)


@router.put("/organizations/{org_id}/creative-limits", response_model=CreativeLimitsOut)
def update_creative_limits(
    org_id: OrgIdPath,
    payload: CreativeLimitsUpdate,
    current: PortalAdminDep,
    db: DbSession,
    ip: ClientIp,
) -> CreativeLimitsOut:
    """Override Creative Studio's monthly limits for one workspace (portal
    admin only, audited). ``null`` returns a limit to the platform default."""
    from app.db.session import bind_tenant
    from app.services.creative import quotas

    org, changed = quotas.set_overrides(db, org_id, payload)
    if changed:
        actor = current.user.email or current.user.name
        entry = {
            "actor": actor,
            "actor_id": current.user.id,
            "action": "portal changed Creative Studio monthly limits",
            "module": "portal",
            "ip_address": ip,
            # Field names and numbers only.
            "context": {"organization_id": org.id, "changed": changed},
        }
        audit.record(db, tenant_id=current.tenant_id, **entry)
        if org.id != current.tenant_id:
            # Also in the workspace's own log, so its admins can see why.
            bind_tenant(db, org.id)
            try:
                audit.record(db, tenant_id=org.id, **entry)
            finally:
                bind_tenant(db, current.tenant_id)
    db.commit()
    out = _creative_limits(db, org, current)
    out.toast = Toast(
        message="Creative Studio limits saved." if changed else "Nothing new to save.",
        kind="success" if changed else "info",
    )
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


class SignInConnectorOut(ApiModel):
    slug: str
    name: str
    available: bool
    missing: list[str] = Field(default_factory=list)
    #: ``missing``, as the setup form labels the values.
    needs: list[str] = Field(default_factory=list)
    scopes: list[str] = Field(default_factory=list)


class SignInFieldOut(ApiModel):
    """One value the vendor's console gives you. Never carries a secret:
    ``preview`` is the value itself only for non-secrets (the client ID),
    otherwise "Set · ends in 1234"."""

    key: str
    label: str
    secret: bool = False
    multiline: bool = False
    needed_for: str = ""
    help: str = ""
    placeholder: str = ""
    is_set: bool = False
    #: portal (saved here) | server (environment) | "" (not set)
    source: str = ""
    preview: str = ""


class SignInProviderOut(ApiModel):
    name: str
    label: str
    configured: bool
    #: Register exactly this with the vendor.
    redirect_uri: str
    settings_keys: list[str] = Field(default_factory=list)
    missing: list[str] = Field(default_factory=list)
    connectors: list[SignInConnectorOut] = Field(default_factory=list)
    console_url: str = ""
    note: str = ""
    #: ready | partial | not_set_up | built_in
    status: str = "not_set_up"
    status_detail: str = ""
    console_label: str = ""
    steps: list[str] = Field(default_factory=list)
    permissions: list[str] = Field(default_factory=list)
    permissions_where: str = ""
    review_note: str = ""
    fields: list[SignInFieldOut] = Field(default_factory=list)
    saved_here: bool = False
    updated_by: str = ""
    updated_at: str = ""
    checkable: bool = False


class SignInProviderSave(ApiModel):
    #: ``{field key: value}``. A blank value keeps what is saved.
    values: dict[str, str] = Field(default_factory=dict)


class SignInCheckOut(ApiModel):
    #: ok | failed | unchecked
    status: str
    message: str


class SignInProviderSaved(ApiModel):
    provider: SignInProviderOut
    check: SignInCheckOut
    toast: Toast | None = None


def _provider_out(status) -> SignInProviderOut:  # noqa: ANN001
    data = {k: getattr(status, k) for k in status.__slots__}
    data["connectors"] = [SignInConnectorOut(**c) for c in status.connectors]
    data["fields"] = [SignInFieldOut(**f) for f in status.fields]
    return SignInProviderOut(**data)


def _read_snapshot(db):  # noqa: ANN001, ANN202
    """The saved apps, read on this request's session. Before migration
    0017 has run there is no table: server settings only."""
    from app.services import platform_oauth_apps

    try:
        with db.begin_nested():
            return platform_oauth_apps.read_snapshot(db)
    except SQLAlchemyError:
        return platform_oauth_apps.Snapshot()


def _statuses(db) -> list:  # noqa: ANN001
    """Fresh from this request's session (and shared with this worker's
    cache), so the page shows a save the moment it is made."""
    from app.services import oauth_flow, platform_oauth_apps

    platform_oauth_apps.prime_cache(_read_snapshot(db))
    return oauth_flow.provider_statuses()


def _one(db, name: str) -> SignInProviderOut:  # noqa: ANN001
    for status in _statuses(db):
        if status.name == name:
            return _provider_out(status)
    raise NotFoundError("That sign-in provider was not found")


def _settable(name: str) -> str:
    from app.services import provider_setup

    name = (name or "").strip().lower()
    if provider_setup.setup_for(name) is None:
        raise NotFoundError("That sign-in provider has nothing to set up")
    return name


@router.get("/sign-in-providers", response_model=list[SignInProviderOut])
def sign_in_providers(_: PortalAdminDep, db: DbSession) -> list[SignInProviderOut]:
    """Every sign-in provider: status, setup steps, the redirect URI to
    register, and the values to enter. Secrets are never returned."""
    return [_provider_out(status) for status in _statuses(db)]


@router.put("/sign-in-providers/{name}", response_model=SignInProviderSaved)
def save_sign_in_provider(
    name: str,
    payload: SignInProviderSave,
    current: PortalAdminDep,
    db: DbSession,
    ip: ClientIp,
) -> SignInProviderSaved:
    """Save the platform's app details for one provider (encrypted), then
    check them with the vendor where it offers a reliable quick check."""
    from app.connectors.base import oauth_providers
    from app.services import platform_oauth_apps

    name = _settable(name)
    label = oauth_providers.PROVIDERS[name].label
    actor = current.user.email or current.user.name
    try:
        changed = platform_oauth_apps.save(db, provider=name, values=payload.values, actor=actor)
    except SQLAlchemyError as exc:
        db.rollback()
        raise InvalidInputError(
            "Saving needs the latest database update (migration 0017). Ask whoever runs "
            "the server to apply it, then try again."
        ) from exc
    if changed:
        audit.record(
            db,
            tenant_id=current.tenant_id,
            actor=actor,
            actor_id=current.user.id,
            action=f"portal saved Sign in with {label} ({', '.join(changed)})",
            module="portal",
            ip_address=ip,
            # Which values changed, never the values.
            context={"provider": name, "changed": changed},
        )
    db.commit()
    platform_oauth_apps.invalidate_cache()
    provider = _one(db, name)
    result = platform_oauth_apps.check(name)
    message = result.message if changed else f"Nothing new to save. {result.message}"
    return SignInProviderSaved(
        provider=provider,
        check=SignInCheckOut(status=result.status, message=message),
        toast=Toast(
            message=message,
            kind={"ok": "success", "failed": "warning"}.get(result.status, "info"),
        ),
    )


@router.post("/sign-in-providers/{name}/check", response_model=SignInCheckOut)
def check_sign_in_provider(name: str, _: PortalAdminDep, db: DbSession) -> SignInCheckOut:
    """Check the values in force (saved here, else server settings)."""
    from app.services import platform_oauth_apps

    name = _settable(name)
    platform_oauth_apps.prime_cache(_read_snapshot(db))
    result = platform_oauth_apps.check(name)
    return SignInCheckOut(status=result.status, message=result.message)


@router.delete("/sign-in-providers/{name}", response_model=SignInProviderSaved)
def clear_sign_in_provider(
    name: str, current: PortalAdminDep, db: DbSession, ip: ClientIp
) -> SignInProviderSaved:
    """Remove what was saved here for one provider. Values set in the
    server environment, if any, apply again."""
    from app.connectors.base import oauth_providers
    from app.services import platform_oauth_apps

    name = _settable(name)
    label = oauth_providers.PROVIDERS[name].label
    try:
        removed = platform_oauth_apps.clear(db, provider=name)
    except SQLAlchemyError:
        db.rollback()
        removed = False
    if removed:
        audit.record(
            db,
            tenant_id=current.tenant_id,
            actor=current.user.email or current.user.name,
            actor_id=current.user.id,
            action=f"portal removed Sign in with {label}",
            module="portal",
            ip_address=ip,
            context={"provider": name},
        )
    db.commit()
    platform_oauth_apps.invalidate_cache()
    provider = _one(db, name)
    if not removed:
        message = "Nothing was saved here for this provider."
    elif provider.configured:
        message = f"Removed. Sign in with {label} still works from the server settings."
    else:
        message = f"Removed. Sign in with {label} is now switched off."
    return SignInProviderSaved(
        provider=provider,
        check=SignInCheckOut(status="unchecked", message=message),
        toast=Toast(message=message, kind="info"),
    )


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
        # Authored copy only; the exception text is not echoed back.
        log.info("Portal feature update rejected (%s)", type(exc).__name__)
        raise InvalidInputError("That feature is not recognised.") from exc

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


# ── Website analyses (public demo) and workspace analyses ─────────────────
@router.get("/analyses")
def list_analyses(
    current: PortalAdminDep,
    db: DbSession,
    source: str = "all",
    status: str = "",
    q: str = "",
    limit: int = 200,
) -> dict:
    """Every analysis run on the platform, newest first.

    ``website`` rows come from the marketing site's free analysis (no
    workspace; visitors are identified only by keyed hashes). ``workspace``
    rows are analyses run inside a workspace, read workspace by workspace so
    row-level security still applies.
    """
    from app.db.session import bind_tenant
    from app.models.demo import DemoAnalysis, SiteAnalysis

    limit = max(1, min(limit, 500))
    needle = q.strip().lower()
    items: list[dict] = []

    def keep(row) -> bool:  # noqa: ANN001
        if status and row.status != status:
            return False
        return not needle or needle in row.website_url.lower() or needle in (row.business_url or "").lower()

    def common(row) -> dict:  # noqa: ANN001
        report = row.report or {}
        overall = report.get("overall") or {}
        business = ((report.get("ai") or {}).get("strategy") or {}).get("business")
        took = (
            round((row.finished_at - row.started_at).total_seconds())
            if row.finished_at and row.started_at else None
        )
        return {
            "id": row.id,
            "website": row.website_url,
            "business_url": row.business_url,
            "status": row.status,
            "error": row.error or None,
            "score": overall.get("score"),
            "grade": overall.get("grade"),
            "business_name": str(business.get("name") or "") if isinstance(business, dict) else "",
            "model": row.model,
            "seconds": took,
            "created_at": row.created_at.isoformat() if row.created_at else None,
        }

    if source in ("all", "website"):
        for row in db.scalars(select(DemoAnalysis).order_by(DemoAnalysis.created_at.desc()).limit(limit)):
            if keep(row):
                items.append({**common(row), "source": "website", "workspace": None, "workspace_id": None})

    if source in ("all", "workspace"):
        orgs = list(db.execute(select(Organization)).scalars())
        for org in orgs:
            bind_tenant(db, org.id)
            for row in db.scalars(
                select(SiteAnalysis).where(SiteAnalysis.tenant_id == org.id)
                .order_by(SiteAnalysis.created_at.desc()).limit(limit)
            ):
                if keep(row):
                    items.append({
                        **common(row), "source": "workspace", "workspace": org.name, "workspace_id": org.id,
                        "requested_by": row.requested_by_name,
                        "connectors": len((row.context or {}).get("connectors") or []),
                    })
        bind_tenant(db, current.tenant_id)

    items.sort(key=lambda i: i["created_at"] or "", reverse=True)
    day_ago = utcnow_iso_day_ago()
    return {
        "items": items[:limit],
        "totals": {
            "website": sum(1 for i in items if i["source"] == "website"),
            "workspace": sum(1 for i in items if i["source"] == "workspace"),
            "last_24h": sum(1 for i in items if (i["created_at"] or "") >= day_ago),
            "failed": sum(1 for i in items if i["status"] == "failed"),
        },
    }


def utcnow_iso_day_ago() -> str:
    from datetime import timedelta

    from app.db.base import utcnow

    return (utcnow() - timedelta(days=1)).isoformat()


@router.get("/analyses/website/{analysis_id}")
def read_website_analysis(analysis_id: str, current: PortalAdminDep, db: DbSession) -> dict:
    from app.models.demo import DemoAnalysis
    from app.services.demo_analysis import service

    row = db.get(DemoAnalysis, analysis_id)
    if row is None:
        raise NotFoundError("Analysis not found")
    return {**service.public_view(row), "source": "website", "model": row.model}


@router.get("/analyses/website/{analysis_id}/download")
def download_website_analysis(analysis_id: str, current: PortalAdminDep, db: DbSession, format: str = "pdf"):  # noqa: A002, ANN201
    from app.api.v1.site_analysis import report_file
    from app.models.demo import DemoAnalysis

    row = db.get(DemoAnalysis, analysis_id)
    if row is None:
        raise NotFoundError("Analysis not found")
    return report_file(row.report, format)


@router.get("/analyses/workspace/{org_id}/{analysis_id}/download")
def download_workspace_analysis(org_id: str, analysis_id: str, current: PortalAdminDep, db: DbSession, format: str = "pdf"):  # noqa: A002, ANN201
    from app.api.v1.site_analysis import report_file
    from app.db.session import bind_tenant
    from app.services.demo_analysis import workspace

    org = db.get(Organization, org_id)
    if org is None:
        raise NotFoundError("That workspace was not found")
    bind_tenant(db, org_id)
    try:
        row = workspace.get(db, org_id, analysis_id)
        detail = workspace.detail(db, row)
        return report_file(
            detail["report"], format,
            extra={"comparison": detail["comparison"], "context": row.context, "workspace": org.name},
        )
    finally:
        bind_tenant(db, current.tenant_id)


@router.get("/analyses/workspace/{org_id}/{analysis_id}")
def read_workspace_analysis(org_id: str, analysis_id: str, current: PortalAdminDep, db: DbSession) -> dict:
    from app.db.session import bind_tenant
    from app.services.demo_analysis import workspace

    org = db.get(Organization, org_id)
    if org is None:
        raise NotFoundError("That workspace was not found")
    bind_tenant(db, org_id)
    try:
        out = workspace.detail(db, workspace.get(db, org_id, analysis_id))
    finally:
        bind_tenant(db, current.tenant_id)
    return {**out, "source": "workspace", "workspace": org.name, "workspace_id": org.id}
