"""Content library, brand profile and AI images.

* ``/brand-profile`` — read, generate from a URL, and edit the profile every
  content agent writes from.
* ``/assets`` — every article, search campaign, social post and email the
  content agents produced: list, read, edit, publish, reject and download.
* ``/public/images/...`` — signed, expiring, unauthenticated image links for
  platforms that only accept an image by URL (Instagram, Pinterest).
"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Query
from fastapi.responses import Response
from pydantic import Field
from sqlalchemy import select

from app.api.deps import CurrentUserDep, DbSession
from app.core.exceptions import ConflictError, InvalidInputError, NotFoundError
from app.core.rbac import Module
from app.models.approval import ApprovalItem, ApprovalStatus
from app.models.content import AssetKind, AssetStatus, GeneratedImage, MarketingAsset
from app.schemas.common import ActionResult, ApiModel, Toast
from app.services import assets as asset_service
from app.services import audit, brand_profile, images
from app.services.encryption import OrgCipher
from app.services.social_schedule import safe_link

router = APIRouter(tags=["content"])

STATUS_LABELS = {
    AssetStatus.DRAFT: "Waiting for approval",
    AssetStatus.APPROVED: "Approved — ready to download",
    AssetStatus.SCHEDULED: "Scheduled",
    AssetStatus.PUBLISHING: "Publishing now",
    AssetStatus.PUBLISHED: "Published",
    AssetStatus.EXPORTED: "Downloaded",
    AssetStatus.REJECTED: "Rejected",
    AssetStatus.FAILED: "Failed to publish",
}

#: What "published" means differs by kind: articles and emails are created as
#: drafts on the platform, and campaigns are created paused. Saying
#: "Published" for those would claim something went live that did not.
PUBLISHED_LABELS = {
    "article": "Saved as a draft in your CMS",
    "email": "Draft created in your email platform",
    "search_campaign": "Created paused in Google Ads",
    "social_post": "Posted",
}


def status_label(kind: str, status: str) -> str:
    if status == AssetStatus.PUBLISHED and kind in PUBLISHED_LABELS:
        return PUBLISHED_LABELS[kind]
    return STATUS_LABELS.get(status, status.title())


# ── Schemas ────────────────────────────────────────────────────────────────
class BrandProfileOut(ApiModel):
    profile: dict | None = None
    #: An AI model is connected, so "Generate" can work.
    can_generate: bool = False
    suggested_url: str = ""
    #: Workspace time zone used to schedule posts; "" means UTC.
    timezone: str = ""


class BrandGenerateRequest(ApiModel):
    url: str = Field(min_length=3, max_length=500)


class AssetOut(ApiModel):
    id: str
    kind: str
    channel: str
    title: str
    status: str
    status_label: str
    agent_slug: str
    agent_name: str = ""
    scheduled_for: datetime | None = None
    published_at: datetime | None = None
    created_at: datetime | None = None
    remote_url: str = ""
    error: str = ""
    image_ids: list[str] = Field(default_factory=list)
    #: Pending approval waiting on this asset, if any.
    approval_id: str | None = None
    can_publish: bool = False
    formats: dict[str, str] = Field(default_factory=dict)
    detail: dict = Field(default_factory=dict)


class AssetDetailOut(AssetOut):
    content: dict = Field(default_factory=dict)


class AssetUpdateRequest(ApiModel):
    title: str | None = Field(default=None, max_length=300)
    content: dict | None = None
    scheduled_for: datetime | None = None


# ── Brand profile ──────────────────────────────────────────────────────────
@router.get("/brand-profile", response_model=BrandProfileOut)
def get_brand_profile(current: CurrentUserDep, db: DbSession) -> BrandProfileOut:
    org = current.organization
    try:
        brand_profile.workspace_llm(db, org)
        can_generate = True
    except ConflictError:
        can_generate = False
    suggested = f"https://{org.primary_domain}" if org.primary_domain else ""
    return BrandProfileOut(
        profile=brand_profile.get(org),
        can_generate=can_generate,
        suggested_url=suggested,
        timezone=brand_profile.timezone_of(org),
    )


@router.post("/brand-profile/generate", response_model=BrandProfileOut)
def generate_brand_profile(payload: BrandGenerateRequest, current: CurrentUserDep, db: DbSession) -> BrandProfileOut:
    current.require_write(Module.CONTENT)
    before = dict(brand_profile.get(current.organization) or {})
    profile = brand_profile.generate(db, current.organization, payload.url)
    _keep_version(db, current, before, profile, source="generated", label=f"Created from {profile.get('source_url', 'your website')}")
    audit.record_user_action(
        db, user=current.user, action=f"generated the brand profile from {profile.get('source_url')}", module="content"
    )
    db.commit()
    return BrandProfileOut(profile=profile, can_generate=True, suggested_url=profile.get("source_url", ""))


@router.put("/brand-profile", response_model=BrandProfileOut)
def update_brand_profile(payload: dict, current: CurrentUserDep, db: DbSession) -> BrandProfileOut:
    current.require_write(Module.CONTENT)
    before = dict(brand_profile.get(current.organization) or {})
    profile = brand_profile.update(db, current.organization, payload)
    _keep_version(db, current, before, profile, source="edited", label="Edited")
    audit.record_user_action(db, user=current.user, action="edited the brand profile", module="content")
    db.commit()
    return BrandProfileOut(
        profile=profile,
        can_generate=True,
        suggested_url=profile.get("source_url", ""),
        timezone=brand_profile.timezone_of(current.organization),
    )


def _keep_version(
    db: DbSession, current: CurrentUserDep, before: dict, profile: dict, *, source: str, label: str
) -> None:
    """Keep this profile in the version history (see app.services.history.brand).

    In a savepoint and never fatal: the save is what the person asked for,
    and it must not fail because its history entry could not be written.
    """
    from app.services.history import brand, store

    org = current.organization

    def keep() -> None:
        # The profile as it was before this first recorded change, so the
        # first version in the list is not already the edited one.
        if before and not store.count(db, org.id, brand.KIND):
            brand.record_version(db, org, before, source="baseline", label=store.BASELINE_LABEL)
        brand.record_version(db, org, profile, source=source, label=label, user=current.user)

    store.guarded(db, "brand profile version", keep)


# ── Assets ─────────────────────────────────────────────────────────────────
def _agent_names(db: DbSession, tenant_id: str) -> dict[str, str]:
    from app.models.agent import AgentRecord

    return {
        r.slug: r.name
        for r in db.execute(select(AgentRecord).where(AgentRecord.tenant_id == tenant_id)).scalars()
    }


def _pending(db: DbSession, tenant_id: str, ids: list[str]) -> dict[str, str]:
    if not ids:
        return {}
    rows = db.execute(
        select(ApprovalItem.id, ApprovalItem.target_id).where(
            ApprovalItem.tenant_id == tenant_id,
            ApprovalItem.target_kind == asset_service.TARGET_KIND,
            ApprovalItem.target_id.in_(ids),
            ApprovalItem.status == ApprovalStatus.PENDING.value,
        )
    ).all()
    return {str(target): str(item) for item, target in rows}


def _out(asset: MarketingAsset, names: dict[str, str], pending: dict[str, str]) -> dict:
    return {
        "id": asset.id,
        "kind": asset.kind,
        "channel": asset.channel,
        "title": asset.title,
        "status": asset.status,
        "status_label": (
            "Cancelled"
            if asset.status == AssetStatus.REJECTED and (asset.detail or {}).get("cancelled")
            else status_label(asset.kind, asset.status)
        ),
        "agent_slug": asset.agent_slug,
        "agent_name": names.get(asset.agent_slug, asset.agent_slug),
        "scheduled_for": asset.scheduled_for,
        "published_at": asset.published_at,
        "created_at": asset.created_at,
        # Only a plain https link reaches the browser (it is opened as a link).
        "remote_url": safe_link(asset.remote_url or ""),
        "error": asset.error,
        "image_ids": list(asset.image_ids or []),
        "approval_id": pending.get(asset.id),
        "can_publish": asset_service.can_publish(asset.kind) and not (asset.detail or {}).get("export_only"),
        "formats": asset_service.formats_for(asset.kind),
        "detail": dict(asset.detail or {}),
    }


@router.get("/assets", response_model=list[AssetOut])
def list_assets(
    current: CurrentUserDep,
    db: DbSession,
    kind: str = Query(default="", max_length=24),
    status: str = Query(default="", max_length=16),
    agent: str = Query(default="", max_length=64),
    limit: int = Query(default=100, ge=1, le=500),
) -> list[AssetOut]:
    current.require_view(Module.CONTENT)
    query = select(MarketingAsset).where(MarketingAsset.tenant_id == current.tenant_id)
    if kind:
        query = query.where(MarketingAsset.kind == kind)
    if status:
        query = query.where(MarketingAsset.status == status)
    if agent:
        query = query.where(MarketingAsset.agent_slug == agent)
    rows = list(db.execute(query.order_by(MarketingAsset.created_at.desc()).limit(limit)).scalars())
    names = _agent_names(db, current.tenant_id)
    pending = _pending(db, current.tenant_id, [r.id for r in rows])
    return [AssetOut(**_out(r, names, pending)) for r in rows]


@router.get("/assets/{asset_id}", response_model=AssetDetailOut)
def get_asset(asset_id: str, current: CurrentUserDep, db: DbSession) -> AssetDetailOut:
    current.require_view(Module.CONTENT)
    asset = asset_service.get(db, tenant_id=current.tenant_id, asset_id=asset_id)
    cipher = OrgCipher.for_org(current.organization)
    data = _out(asset, _agent_names(db, current.tenant_id), _pending(db, current.tenant_id, [asset.id]))
    return AssetDetailOut(**data, content=asset_service.content_of(asset, cipher))


@router.put("/assets/{asset_id}", response_model=AssetDetailOut)
def update_asset(asset_id: str, payload: AssetUpdateRequest, current: CurrentUserDep, db: DbSession) -> AssetDetailOut:
    current.require_write(Module.CONTENT)
    asset = asset_service.get(db, tenant_id=current.tenant_id, asset_id=asset_id)
    if asset.kind == AssetKind.CREATIVE:
        # A design's text is re-checked and redrawn through its approval card
        # ("Edit text" / "Ask for changes"); raw content would skip both.
        raise ConflictError(
            "Designs can't be edited here. Use \"Edit text\" or \"Ask for changes\" on its approval card."
        )
    if asset.status in (AssetStatus.PUBLISHED, AssetStatus.PUBLISHING):
        raise ConflictError("This item is already published. Edit it on the platform instead.")
    cipher = OrgCipher.for_org(current.organization)
    if payload.title is not None:
        asset.title = payload.title.strip()[:300]
    if payload.content is not None:
        asset_service.set_content(asset, cipher, payload.content)
    if payload.scheduled_for is not None:
        asset.scheduled_for = payload.scheduled_for
    if asset.status == AssetStatus.FAILED:
        asset.status = AssetStatus.DRAFT
        asset.error = ""
    audit.record_user_action(db, user=current.user, action=f"edited {asset.kind.replace('_', ' ')}: {asset.title}", module="content")
    db.commit()
    return get_asset(asset_id, current, db)


@router.post("/assets/{asset_id}/publish", response_model=ActionResult)
def publish_asset(asset_id: str, current: CurrentUserDep, db: DbSession) -> ActionResult:
    """Approve and release one item: through its pending approval when there is one."""
    current.require_write(Module.CONTENT)
    asset = asset_service.get(db, tenant_id=current.tenant_id, asset_id=asset_id)
    pending = _pending(db, current.tenant_id, [asset.id]).get(asset.id)
    try:
        if pending:
            current.require_write(Module.APPROVALS)
            from app.services import approvals

            approvals.approve(db, org=current.organization, actor=current.user, item_id=pending, note="")
        else:
            from app.services import agent_safety

            # The same stop approvals obey: nothing is sent while it is on.
            halted = agent_safety.halt_reason(db, current.organization)
            if halted:
                raise ConflictError(halted)
            asset_service.release(db, current.organization, asset, OrgCipher.for_org(current.organization))
            audit.record_user_action(db, user=current.user, action=f"released {asset.title}", module="content")
    except ConflictError:
        db.commit()  # keep the recorded failure on the asset
        raise
    db.commit()
    message = {
        AssetStatus.PUBLISHED: status_label(asset.kind, AssetStatus.PUBLISHED),
        AssetStatus.SCHEDULED: "Scheduled",
        AssetStatus.APPROVED: "Approved — download it to use it",
    }.get(asset.status, "Done")
    return ActionResult(toast=Toast(message=message, kind="success"))


@router.post("/assets/{asset_id}/reject", response_model=ActionResult)
def reject_asset(asset_id: str, current: CurrentUserDep, db: DbSession) -> ActionResult:
    current.require_write(Module.CONTENT)
    asset = asset_service.get(db, tenant_id=current.tenant_id, asset_id=asset_id)
    pending = _pending(db, current.tenant_id, [asset.id]).get(asset.id)
    if pending:
        from app.services import approvals

        approvals.reject(db, org=current.organization, actor=current.user, item_id=pending, note="")
    elif asset.status not in (AssetStatus.PUBLISHED, AssetStatus.EXPORTED):
        asset.status = AssetStatus.REJECTED
    db.commit()
    return ActionResult(toast=Toast(message="Rejected", kind="info"))


@router.get("/assets/{asset_id}/export")
def export_asset(
    asset_id: str, current: CurrentUserDep, db: DbSession, format: str = Query(default="json", max_length=32)  # noqa: A002
) -> Response:
    current.require_view(Module.CONTENT)
    asset = asset_service.get(db, tenant_id=current.tenant_id, asset_id=asset_id)
    file = asset_service.export(asset, OrgCipher.for_org(current.organization), format)
    if asset.status == AssetStatus.APPROVED and current.can_write(Module.CONTENT):
        asset.status = AssetStatus.EXPORTED
        db.commit()
    return Response(
        content=file.data,
        media_type=file.mime,
        headers={"Content-Disposition": f'attachment; filename="{file.filename}"'},
    )


# ── Images ─────────────────────────────────────────────────────────────────
@router.get("/assets/images/{image_id}")
def get_image(image_id: str, current: CurrentUserDep, db: DbSession) -> Response:
    current.require_view(Module.CONTENT)
    image = db.get(GeneratedImage, image_id)
    if image is None or image.tenant_id != current.tenant_id:
        raise NotFoundError("Image not found")
    return Response(content=image.data, media_type=image.mime, headers={"Cache-Control": "private, max-age=86400"})


@router.get("/public/images/{tenant_id}/{image_id}/{expires}/{sig}")
def public_image(tenant_id: str, image_id: str, expires: int, sig: str) -> Response:
    """Unauthenticated: valid only with an unexpired signature for this exact image.

    A ``sig`` that is not 40 lower-case hex digits is refused before any
    comparison, so odd input (non-ASCII, too long) is a 404, never a 500.
    """
    if not images.SIGNATURE_SHAPE.fullmatch(sig) or not images.verify(tenant_id, image_id, expires, sig):
        raise NotFoundError("Image not found")
    from app.db.session import session_scope

    with session_scope(tenant_id) as db:
        image = db.get(GeneratedImage, image_id)
        if image is None or image.tenant_id != tenant_id:
            raise NotFoundError("Image not found")
        data, mime = image.data, image.mime
    return Response(content=data, media_type=mime, headers={"Cache-Control": "public, max-age=3600"})


__all__ = ["router", "InvalidInputError"]
