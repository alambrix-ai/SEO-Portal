"""Topic cluster map and content briefs."""
from __future__ import annotations

from fastapi import APIRouter
from sqlalchemy import select

from app.api.deps import CurrentUserDep, DbSession
from app.core.rbac import Module
from app.models.topic_cluster import ContentBrief, TopicCluster, TopicClusterPage
from app.schemas.common import ApiModel
from app.services.encryption import Ctx

router = APIRouter(prefix="/topics", tags=["topics"])


class TopicClusterOut(ApiModel):
    id: str
    name: str
    seed_terms: list[str]
    gap_score: float
    page_count: int
    status: str
    page_ids: list[str] = []


class ContentBriefOut(ApiModel):
    id: str
    cluster_id: str | None
    title: str
    target_keywords: list[str]
    status: str
    body: str = ""


class TopicWorkspaceOut(ApiModel):
    clusters: list[TopicClusterOut]
    briefs: list[ContentBriefOut]
    read_only: bool = False


@router.get("", response_model=TopicWorkspaceOut)
def workspace(current: CurrentUserDep, db: DbSession) -> TopicWorkspaceOut:
    """Topic map + briefs for the console."""
    current.require_view(Module.SEO)
    clusters = list(
        db.execute(
            select(TopicCluster)
            .where(TopicCluster.tenant_id == current.tenant_id)
            .order_by(TopicCluster.gap_score.desc())
            .limit(100)
        ).scalars()
    )
    membership: dict[str, list[str]] = {}
    if clusters:
        rows = db.execute(
            select(TopicClusterPage).where(
                TopicClusterPage.tenant_id == current.tenant_id,
                TopicClusterPage.cluster_id.in_([c.id for c in clusters]),
            )
        ).scalars()
        for row in rows:
            membership.setdefault(row.cluster_id, []).append(row.page_id)

    briefs = list(
        db.execute(
            select(ContentBrief)
            .where(ContentBrief.tenant_id == current.tenant_id)
            .order_by(ContentBrief.created_at.desc())
            .limit(50)
        ).scalars()
    )
    can_write = current.can_write(Module.SEO)
    brief_outs: list[ContentBriefOut] = []
    for brief in briefs:
        body = ""
        if can_write:
            body = current.cipher.decrypt(brief.body_encrypted, context=Ctx.CONTENT_BRIEF)
        brief_outs.append(
            ContentBriefOut(
                id=brief.id,
                cluster_id=brief.cluster_id,
                title=brief.title,
                target_keywords=list(brief.target_keywords or []),
                status=brief.status,
                body=body,
            )
        )
    return TopicWorkspaceOut(
        clusters=[
            TopicClusterOut(
                id=c.id,
                name=c.name,
                seed_terms=list(c.seed_terms or []),
                gap_score=c.gap_score,
                page_count=c.page_count,
                status=c.status,
                page_ids=membership.get(c.id, []),
            )
            for c in clusters
        ],
        briefs=brief_outs,
        read_only=not can_write,
    )
