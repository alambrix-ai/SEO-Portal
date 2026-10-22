"""Share approved designs on social networks, now or later.

* ``POST /social/schedule``             schedule (or post now) one design on several networks
* ``GET  /social/posts?creative=``      their status: scheduled, posted (with a link) or failed (with the reason)
* ``POST /social/posts/{post_id}/cancel``  cancel a post that is still scheduled

Scheduling and cancelling need write access to Content; reading needs view
access. Every query is filtered by the caller's workspace; another
workspace's design or post is "not found". No model is called. The rules
live in :mod:`app.services.social_schedule`.
"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, BackgroundTasks, Path, Query

from app.agents.social_media_manager import platforms
from app.api.deps import ClientIp, CurrentUserDep, DbSession
from app.core.rbac import Module
from app.schemas.common import ApiModel, Toast
from app.services import audit, social_schedule
from app.services.social_schedule import ScheduleRequest

router = APIRouter(prefix="/social", tags=["social"])

_ID_PATTERN = r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"


class SocialPostOut(ApiModel):
    id: str
    creative_asset_id: str = ""
    network: str
    network_label: str
    subreddit: str = ""
    status: str
    status_label: str
    scheduled_for: datetime | None = None
    #: ``YYYY-MM-DDTHH:MM`` in the workspace time zone.
    scheduled_local: str = ""
    timezone: str = "UTC"
    published_at: datetime | None = None
    #: The post on the platform, once posted.
    link: str = ""
    #: Why it failed, in plain words.
    reason: str = ""
    #: Anything else worth knowing (moved later, posted without its picture).
    note: str = ""
    can_cancel: bool = False


class ScheduleOut(ApiModel):
    posts: list[SocialPostOut]
    toast: Toast


class CancelOut(ApiModel):
    post: SocialPostOut
    toast: Toast


@router.post("/schedule", response_model=ScheduleOut)
def schedule_posts(
    payload: ScheduleRequest,
    background: BackgroundTasks,
    current: CurrentUserDep,
    db: DbSession,
    ip: ClientIp,
) -> ScheduleOut:
    current.require_write(Module.CONTENT)
    org = current.organization
    result = social_schedule.schedule(db, org, user_id=current.user.id, request=payload)
    networks = sorted({str((p.detail or {}).get("network") or "") for p in result.posts})
    audit.record_user_action(
        db,
        user=current.user,
        action=f"scheduled {len(result.posts)} social post(s) of a design",
        module="content",
        ip_address=ip,
        context={
            "creative_asset_id": payload.creative_asset_id.lower(),
            "post_ids": [p.id for p in result.posts],
            "networks": networks,
            "post_now": len(result.post_now_ids),
        },
    )
    # Committed before the task is queued: the background session is a
    # different transaction and must see the new posts.
    db.commit()
    if result.post_now_ids:
        background.add_task(social_schedule.publish_now, org.id, list(result.post_now_ids))
    zone, zone_name = platforms.zone_for(getattr(org, "settings", None))
    posts = [SocialPostOut(**social_schedule.post_view(p, zone, zone_name)) for p in result.posts]
    if result.post_now_ids and len(result.post_now_ids) == len(posts):
        message = "Posting now. The status updates here when it's done."
    elif result.post_now_ids:
        message = "Posting now where you chose it; the rest are scheduled."
    else:
        message = "Scheduled."
    return ScheduleOut(posts=posts, toast=Toast(message=message, kind="success"))


@router.get("/posts", response_model=list[SocialPostOut])
def list_posts(
    current: CurrentUserDep,
    db: DbSession,
    creative: str | None = Query(default=None, pattern=_ID_PATTERN),
    limit: int = Query(default=social_schedule.MAX_LIST, ge=1, le=social_schedule.MAX_LIST),
) -> list[SocialPostOut]:
    current.require_view(Module.CONTENT)
    if creative:
        # Another workspace's design is "not found", not an empty list.
        social_schedule.load_creative(db, current.tenant_id, creative)
    rows = social_schedule.list_posts(db, current.organization, creative_id=creative, limit=limit)
    return [SocialPostOut(**row) for row in rows]


@router.post("/posts/{post_id}/cancel", response_model=CancelOut)
def cancel_post(
    current: CurrentUserDep,
    db: DbSession,
    ip: ClientIp,
    post_id: str = Path(pattern=_ID_PATTERN),
) -> CancelOut:
    current.require_write(Module.CONTENT)
    org = current.organization
    asset = social_schedule.cancel(db, org, post_id=post_id, user_id=current.user.id)
    audit.record_user_action(
        db,
        user=current.user,
        action="cancelled a scheduled social post",
        module="content",
        ip_address=ip,
        context={"post_id": asset.id, "network": str((asset.detail or {}).get("network") or "")},
    )
    db.commit()
    zone, zone_name = platforms.zone_for(getattr(org, "settings", None))
    return CancelOut(
        post=SocialPostOut(**social_schedule.post_view(asset, zone, zone_name)),
        toast=Toast(message="Cancelled. It won't be posted.", kind="success"),
    )


__all__ = ["router"]
