"""Propose short video scripts + captions; thumbnails use Creative Studio formats."""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.base.context import AgentContext
from app.agents.base.contracts import (
    AgentAction,
    AgentCategory,
    AgentResult,
    AgentSpec,
    BaseAgent,
    Impact,
)
from app.db.base import utcnow
from app.models.approval import ApprovalItem, ApprovalType
from app.models.content import AssetKind, AssetStatus, MarketingAsset
from app.models.creative import Product
from app.models.video_script import VideoScriptJob
from app.models.workspace import Organization
from app.services import assets
from app.services.approvals import register_applier, register_rejecter
from app.services.creative import video_scripts as scripts
from app.services.encryption import Ctx, OrgCipher

TARGET_KIND = "video_script_job"
SLUG = "creative_video_scripts"
MAX_ACTIONS = 3


class CreativeVideoScriptsAgent(BaseAgent):
    spec = AgentSpec(
        slug=SLUG,
        name="Creative Video Scripts",
        category=AgentCategory.CONTENT,
        description=(
            "Drafts short-form video scripts and captions from product facts, "
            "and targets YouTube/TikTok thumbnail formats for Creative Studio."
        ),
        default_interval=timedelta(days=1),
        default_schedule="Daily",
        required_capabilities=(),
        max_impact=Impact.HIGH,
        default_max_actions_per_day=10,
        requires_llm=False,
    )

    def run(self, ctx: AgentContext) -> AgentResult:
        products = list(
            ctx.db.execute(
                select(Product).where(
                    Product.tenant_id == ctx.tenant_id,
                    Product.is_archived.is_(False),
                ).limit(50)
            ).scalars()
        )
        if not products:
            return AgentResult.skip("No products available for video scripts")

        proposed_products = {
            j.product_id
            for j in ctx.db.execute(
                select(VideoScriptJob).where(
                    VideoScriptJob.tenant_id == ctx.tenant_id,
                    VideoScriptJob.status.in_(("proposed", "approved")),
                )
            ).scalars()
            if j.product_id
        }

        actions: list[AgentAction] = []
        for product in products:
            if product.id in proposed_products:
                continue
            if len(actions) >= min(MAX_ACTIONS, max(ctx.remaining_actions, 1)):
                break
            draft = scripts.build_draft(
                name=product.name,
                description=product.description or "",
                claims=list(product.approved_claims or []),
            )
            _ = scripts.as_untrusted(draft)
            asset = assets.create(
                ctx.db,
                tenant_id=ctx.tenant_id,
                cipher=ctx.cipher,
                agent_slug=SLUG,
                kind=AssetKind.VIDEO_SCRIPT,
                channel="export:video_script",
                title=draft.title,
                content=scripts.to_dict(draft),
                run_id=ctx.run_id or "",
                detail={
                    "export_only": True,
                    "thumbnail_formats": draft.thumbnail_formats,
                    "product_id": product.id,
                },
            )
            job = VideoScriptJob(
                tenant_id=ctx.tenant_id,
                title=draft.title,
                body_encrypted=ctx.cipher.encrypt_json(
                    scripts.to_dict(draft), context=Ctx.VIDEO_SCRIPT
                ),
                thumbnail_formats=draft.thumbnail_formats,
                asset_id=asset.id,
                status="proposed",
                product_id=product.id,
                proposed_at=utcnow(),
            )
            ctx.db.add(job)
            ctx.db.flush()
            actions.append(
                AgentAction(
                    kind="approve_video_script",
                    title=f'Video script: "{draft.title}"',
                    impact=Impact.HIGH,
                    approval_type=ApprovalType.VIDEO_SCRIPT.value,
                    target_kind=TARGET_KIND,
                    target_id=job.id,
                    payload={
                        "job_id": job.id,
                        "asset_id": asset.id,
                        "title": draft.title,
                        "thumbnail_formats": draft.thumbnail_formats,
                        "captions": draft.captions[:6],
                    },
                    apply=_make_release(job.id),
                    audit=f"approved video script {draft.title[:80]}",
                    metrics={"video_scripts": 1},
                )
            )

        ctx.db.flush()
        if not actions:
            return AgentResult.skip("No new video scripts to propose")
        return AgentResult(
            summary=f"Proposed {len(actions)} video script{'s' if len(actions) != 1 else ''}",
            actions=actions,
            metric_label=f"{len(actions)} script proposed",
            metrics={"video_scripts_proposed": len(actions)},
        )


def _make_release(job_id: str):  # noqa: ANN202
    def apply(ctx: AgentContext) -> None:
        _release(ctx.db, ctx.org, job_id, cipher=ctx.cipher)

    return apply


def _release(db: Session, org: Organization, job_id: str, *, cipher: OrgCipher) -> None:
    job = db.get(VideoScriptJob, job_id)
    if job is None or job.tenant_id != org.id:
        raise LookupError("Video script job no longer exists")
    if job.asset_id:
        asset = db.get(MarketingAsset, job.asset_id)
        if asset is not None and asset.tenant_id == org.id:
            assets.release(db, org, asset, cipher)
    job.status = "approved"
    db.flush()


@register_applier(TARGET_KIND)
def apply_video_script(
    db: Session, org: Organization, item: ApprovalItem, payload: dict
) -> None:
    job_id = str((payload or {}).get("job_id") or item.target_id or "")
    if not job_id:
        raise ValueError("Missing job id")
    cipher = OrgCipher.for_org(org)
    _release(db, org, job_id, cipher=cipher)
    job = db.get(VideoScriptJob, job_id)
    if job is not None:
        job.approval_item_id = item.id


@register_rejecter(TARGET_KIND)
def reject_video_script(
    db: Session, org: Organization, item: ApprovalItem, payload: dict
) -> None:
    job_id = str((payload or {}).get("job_id") or item.target_id or "")
    job = db.get(VideoScriptJob, job_id) if job_id else None
    if job is None or job.tenant_id != org.id:
        return
    job.status = "rejected"
    job.approval_item_id = item.id
    if job.asset_id:
        asset = db.get(MarketingAsset, job.asset_id)
        if asset is not None and asset.tenant_id == org.id:
            asset.status = AssetStatus.REJECTED
    db.flush()


AGENT = CreativeVideoScriptsAgent()
