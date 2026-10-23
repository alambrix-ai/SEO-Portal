"""AI visibility tracker — target questions, competitors, observations."""
from __future__ import annotations

from collections import Counter

from fastapi import APIRouter, Query
from sqlalchemy import desc, func, select

from app.api.deps import CurrentUserDep, DbSession
from app.core.exceptions import ConflictError, NotFoundError
from app.core.rbac import Module
from app.models.visibility import VisibilityObservation, VisibilityQuery
from app.schemas.common import ActionResult, Toast
from app.schemas.visibility import (
    VisibilityCompetitorsIn,
    VisibilityCompetitorsOut,
    VisibilityCompetitorShare,
    VisibilityEngineShare,
    VisibilityObservationOut,
    VisibilityQueryIn,
    VisibilityQueryOut,
    VisibilitySummaryOut,
    VisibilityWorkspaceOut,
)
from app.services import audit
from app.services import visibility as visibility_service
from app.services.encryption import Ctx, OrgCipher

router = APIRouter(prefix="/visibility", tags=["visibility"])

MAX_QUERIES = 50


def _query_out(row: VisibilityQuery, cipher: OrgCipher) -> VisibilityQueryOut:
    return VisibilityQueryOut(
        id=row.id,
        question=cipher.decrypt(row.question_encrypted, context=Ctx.VISIBILITY_QUESTION),
        label=row.label or "",
        active=bool(row.active),
        tags=list(row.tags or []),
        created_at=row.created_at,
    )


def _obs_out(row: VisibilityObservation, cipher: OrgCipher) -> VisibilityObservationOut:
    excerpt = ""
    if row.answer_excerpt_encrypted:
        excerpt = cipher.decrypt(
            row.answer_excerpt_encrypted, context=Ctx.VISIBILITY_ANSWER
        )
    return VisibilityObservationOut(
        id=row.id,
        query_id=row.query_id,
        question=cipher.decrypt(row.question_encrypted, context=Ctx.VISIBILITY_QUESTION),
        engine=row.engine,
        brand_cited=bool(row.brand_cited),
        brand_position=row.brand_position,
        cited_domains=list(row.cited_domains or []),
        competitors_cited=list(row.competitors_cited or []),
        answer_excerpt=excerpt,
        observed_at=row.observed_at,
    )


def _summary(
    rows: list[VisibilityObservation], *, read_only: bool
) -> VisibilitySummaryOut:
    probes = len(rows)
    brand = sum(1 for r in rows if r.brand_cited)
    by_engine: dict[str, list[int]] = {}
    rival_counter: Counter[str] = Counter()
    for row in rows:
        bucket = by_engine.setdefault(row.engine, [0, 0])
        bucket[0] += 1
        if row.brand_cited:
            bucket[1] += 1
        for host in row.competitors_cited or []:
            rival_counter[host] += 1

    engines = [
        VisibilityEngineShare(
            engine=engine,
            probes=counts[0],
            brand_citations=counts[1],
            share=(counts[1] / counts[0]) if counts[0] else 0.0,
        )
        for engine, counts in sorted(by_engine.items())
    ]
    top = [
        VisibilityCompetitorShare(domain=domain, sightings=count)
        for domain, count in rival_counter.most_common(15)
    ]
    return VisibilitySummaryOut(
        probes=probes,
        brand_citations=brand,
        brand_share=(brand / probes) if probes else 0.0,
        by_engine=engines,
        top_competitors=top,
        read_only=read_only,
    )


@router.get("", response_model=VisibilityWorkspaceOut)
def workspace(current: CurrentUserDep, db: DbSession) -> VisibilityWorkspaceOut:
    current.require_view(Module.SEO)
    can_write = current.can_write(Module.SEO)
    cipher = current.cipher

    queries = list(
        db.execute(
            select(VisibilityQuery)
            .where(VisibilityQuery.tenant_id == current.tenant_id)
            .order_by(VisibilityQuery.created_at.asc())
            .limit(MAX_QUERIES)
        ).scalars()
    )
    settings = visibility_service.get_or_create_settings(db, current.tenant_id)
    recent = list(
        db.execute(
            select(VisibilityObservation)
            .where(VisibilityObservation.tenant_id == current.tenant_id)
            .order_by(desc(VisibilityObservation.observed_at))
            .limit(40)
        ).scalars()
    )
    return VisibilityWorkspaceOut(
        queries=[_query_out(q, cipher) for q in queries],
        competitors=list(settings.competitor_domains or []),
        recent=[_obs_out(r, cipher) for r in recent],
        summary=_summary(recent, read_only=not can_write),
        read_only=not can_write,
    )


@router.get("/summary", response_model=VisibilitySummaryOut)
def summary(
    current: CurrentUserDep,
    db: DbSession,
    limit: int = Query(default=200, ge=1, le=500),
) -> VisibilitySummaryOut:
    current.require_view(Module.SEO)
    rows = list(
        db.execute(
            select(VisibilityObservation)
            .where(VisibilityObservation.tenant_id == current.tenant_id)
            .order_by(desc(VisibilityObservation.observed_at))
            .limit(limit)
        ).scalars()
    )
    return _summary(rows, read_only=not current.can_write(Module.SEO))


@router.post("/queries", response_model=VisibilityQueryOut)
def create_query(
    body: VisibilityQueryIn, current: CurrentUserDep, db: DbSession
) -> VisibilityQueryOut:
    current.require_write(Module.SEO)
    cipher = current.cipher
    existing = db.execute(
        select(func.count())
        .select_from(VisibilityQuery)
        .where(VisibilityQuery.tenant_id == current.tenant_id)
    ).scalar_one()
    if int(existing) >= MAX_QUERIES:
        raise ConflictError("This workspace already has the maximum number of questions.")

    row = VisibilityQuery(
        tenant_id=current.tenant_id,
        question_encrypted=cipher.encrypt(
            body.question, context=Ctx.VISIBILITY_QUESTION
        ),
        label=(body.label or body.question[:80]).strip()[:120],
        active=body.active,
        tags=body.tags,
    )
    db.add(row)
    db.flush()
    audit.record_user_action(
        db,
        user=current.user,
        action="added an AI visibility question",
        module="seo",
        context={"query_id": row.id},
    )
    db.commit()
    db.refresh(row)
    return _query_out(row, cipher)


@router.patch("/queries/{query_id}", response_model=VisibilityQueryOut)
def update_query(
    query_id: str, body: VisibilityQueryIn, current: CurrentUserDep, db: DbSession
) -> VisibilityQueryOut:
    current.require_write(Module.SEO)
    cipher = current.cipher
    row = db.get(VisibilityQuery, query_id)
    if row is None or row.tenant_id != current.tenant_id:
        raise NotFoundError("That question was not found.")
    row.question_encrypted = cipher.encrypt(
        body.question, context=Ctx.VISIBILITY_QUESTION
    )
    row.label = (body.label or body.question[:80]).strip()[:120]
    row.active = body.active
    row.tags = body.tags
    db.flush()
    audit.record_user_action(
        db,
        user=current.user,
        action="updated an AI visibility question",
        module="seo",
        context={"query_id": row.id},
    )
    db.commit()
    db.refresh(row)
    return _query_out(row, cipher)


@router.delete("/queries/{query_id}", response_model=ActionResult)
def delete_query(query_id: str, current: CurrentUserDep, db: DbSession) -> ActionResult:
    current.require_write(Module.SEO)
    row = db.get(VisibilityQuery, query_id)
    if row is None or row.tenant_id != current.tenant_id:
        raise NotFoundError("That question was not found.")
    db.delete(row)
    audit.record_user_action(
        db,
        user=current.user,
        action="removed an AI visibility question",
        module="seo",
        context={"query_id": query_id},
    )
    db.commit()
    return ActionResult(toast=Toast(message="Question removed", kind="success"))


@router.put("/competitors", response_model=VisibilityCompetitorsOut)
def set_competitors(
    body: VisibilityCompetitorsIn, current: CurrentUserDep, db: DbSession
) -> VisibilityCompetitorsOut:
    current.require_write(Module.SEO)
    settings = visibility_service.get_or_create_settings(db, current.tenant_id)
    settings.competitor_domains = visibility_service.normalise_competitor_list(
        body.competitor_domains
    )
    db.flush()
    audit.record_user_action(
        db,
        user=current.user,
        action="updated AI visibility competitors",
        module="seo",
        context={"count": len(settings.competitor_domains)},
    )
    db.commit()
    return VisibilityCompetitorsOut(competitor_domains=list(settings.competitor_domains))
