"""Turn approved articles into social, email, and video-script drafts."""
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
from app.models.repurpose import ContentRepurposeJob
from app.models.seo import SeoPage
from app.models.workspace import Organization
from app.services import assets, content_repurpose as repurpose
from app.services.approvals import register_applier, register_rejecter
from app.services.encryption import Ctx, OrgCipher

TARGET_KIND = "content_repurpose_job"
MAX_SOURCES = 3
SLUG = "content_repurposer"


class ContentRepurposerAgent(BaseAgent):
    spec = AgentSpec(
        slug=SLUG,
        name="Content Repurposer",
        category=AgentCategory.CONTENT,
        description=(
            "Repurposes approved articles into social posts, email drafts, and "
            "short video scripts as marketing assets through the approval gate."
        ),
        default_interval=timedelta(days=1),
        default_schedule="Daily",
        required_capabilities=(),
        max_impact=Impact.HIGH,
        default_max_actions_per_day=12,
        requires_llm=False,
    )

    def run(self, ctx: AgentContext) -> AgentResult:
        done_ids = {
            job.source_id
            for job in ctx.db.execute(
                select(ContentRepurposeJob).where(
                    ContentRepurposeJob.tenant_id == ctx.tenant_id,
                    ContentRepurposeJob.status.in_(("proposed", "approved")),
                )
            ).scalars()
        }
        sources = self._sources(ctx, exclude=done_ids)
        if not sources:
            return AgentResult.skip("No approved articles waiting to be repurposed")

        actions: list[AgentAction] = []
        for source_kind, source_id, title, body, url in sources[
            : min(MAX_SOURCES, max(ctx.remaining_actions, 1))
        ]:
            try:
                pack = repurpose.build_pack(title=title, body_html=body, site_url=url)
            except ValueError:
                continue
            # Fence the pack so any future LLM polish cannot treat it as instructions.
            _ = repurpose.as_untrusted_block(pack)
            pack_dict = repurpose.pack_to_dict(pack)

            social = assets.create(
                ctx.db,
                tenant_id=ctx.tenant_id,
                cipher=ctx.cipher,
                agent_slug=SLUG,
                kind=AssetKind.SOCIAL_POST,
                channel="export:linkedin",
                title=f"Social · {title}"[:300],
                content={
                    "text": pack.social_text,
                    "hashtags": pack.hashtags,
                    "network": "linkedin",
                },
                run_id=ctx.run_id or "",
                detail={"source_kind": source_kind, "source_id": source_id, "export_only": True},
            )
            email = assets.create(
                ctx.db,
                tenant_id=ctx.tenant_id,
                cipher=ctx.cipher,
                agent_slug=SLUG,
                kind=AssetKind.EMAIL,
                channel="export:email",
                title=pack.email_subject[:300],
                content={
                    "subject": pack.email_subject,
                    "body": pack.email_body,
                    "html": f"<p>{pack.email_body.replace(chr(10), '<br>')}</p>",
                },
                run_id=ctx.run_id or "",
                detail={"source_kind": source_kind, "source_id": source_id, "export_only": True},
            )
            # Video scripts reuse the creative asset kind as text-only drafts.
            script = assets.create(
                ctx.db,
                tenant_id=ctx.tenant_id,
                cipher=ctx.cipher,
                agent_slug=SLUG,
                kind=AssetKind.CREATIVE,
                channel="export:video_script",
                title=f"Script · {title}"[:300],
                content={"script": pack.video_script, "format": "short_video"},
                run_id=ctx.run_id or "",
                detail={
                    "source_kind": source_kind,
                    "source_id": source_id,
                    "export_only": True,
                    "asset_subtype": "video_script",
                },
            )
            job = ContentRepurposeJob(
                tenant_id=ctx.tenant_id,
                source_kind=source_kind,
                source_id=source_id,
                source_title=title[:300],
                pack_encrypted=ctx.cipher.encrypt_json(
                    pack_dict, context=Ctx.REPURPOSE_PACK
                ),
                status="proposed",
                asset_ids=[social.id, email.id, script.id],
                proposed_at=utcnow(),
            )
            ctx.db.add(job)
            ctx.db.flush()
            actions.append(
                AgentAction(
                    kind="approve_repurpose_pack",
                    title=f'Repurpose “{title}” into social, email, and script',
                    impact=Impact.HIGH,
                    approval_type=ApprovalType.CONTENT_REPURPOSE.value,
                    target_kind=TARGET_KIND,
                    target_id=job.id,
                    payload={
                        "job_id": job.id,
                        "asset_ids": [social.id, email.id, script.id],
                        "source_title": title[:200],
                        "channels": ["social", "email", "video_script"],
                    },
                    apply=_make_release(job.id),
                    audit=f"approved repurpose pack for {title[:80]}",
                    metrics={"repurpose_packs": 1},
                )
            )

        ctx.db.flush()
        if not actions:
            return AgentResult.skip("No articles produced a valid repurpose pack")
        return AgentResult(
            summary=f"Proposed {len(actions)} repurpose pack{'s' if len(actions) != 1 else ''}",
            actions=actions,
            metric_label=f"{len(actions)} pack proposed",
            metrics={"repurpose_packs_proposed": len(actions)},
        )

    def _sources(
        self, ctx: AgentContext, *, exclude: set[str]
    ) -> list[tuple[str, str, str, str, str]]:
        out: list[tuple[str, str, str, str, str]] = []
        articles = list(
            ctx.db.execute(
                select(MarketingAsset)
                .where(
                    MarketingAsset.tenant_id == ctx.tenant_id,
                    MarketingAsset.kind == AssetKind.ARTICLE,
                    MarketingAsset.status.in_(
                        (AssetStatus.APPROVED, AssetStatus.PUBLISHED, AssetStatus.EXPORTED)
                    ),
                )
                .order_by(MarketingAsset.updated_at.desc())
                .limit(20)
            ).scalars()
        )
        for asset in articles:
            if asset.id in exclude:
                continue
            content = ctx.cipher.decrypt_json(
                asset.content_encrypted, context=Ctx.ASSET_CONTENT, default={}
            )
            if not isinstance(content, dict):
                # assets.create stores json.dumps string via encrypt, not encrypt_json
                raw = ctx.cipher.decrypt(asset.content_encrypted, context=Ctx.ASSET_CONTENT)
                import json

                try:
                    content = json.loads(raw) if raw else {}
                except json.JSONDecodeError:
                    content = {}
            body = str(content.get("body_html") or content.get("body") or content.get("html") or "")
            if not body:
                continue
            out.append(("article", asset.id, asset.title or "Article", body, str(content.get("url") or "")))

        pages = list(
            ctx.db.execute(
                select(SeoPage)
                .where(
                    SeoPage.tenant_id == ctx.tenant_id,
                    SeoPage.status == "live",
                )
                .limit(10)
            ).scalars()
        )
        for page in pages:
            if page.id in exclude or len(out) >= MAX_SOURCES:
                break
            body = ctx.cipher.decrypt(page.body_encrypted, context=Ctx.PAGE_BODY) or ""
            if len(body) < 80:
                continue
            out.append(("seo_page", page.id, page.title or "Page", body, page.url or ""))
        return out


def _make_release(job_id: str):  # noqa: ANN202
    def apply(ctx: AgentContext) -> None:
        _release_job(ctx.db, ctx.org, job_id, cipher=ctx.cipher)

    return apply


def _release_job(
    db: Session, org: Organization, job_id: str, *, cipher: OrgCipher
) -> None:
    job = db.get(ContentRepurposeJob, job_id)
    if job is None or job.tenant_id != org.id:
        raise LookupError("Repurpose job no longer exists")
    for asset_id in job.asset_ids or []:
        asset = db.get(MarketingAsset, asset_id)
        if asset is None or asset.tenant_id != org.id:
            continue
        assets.release(db, org, asset, cipher)
    job.status = "approved"
    db.flush()


@register_applier(TARGET_KIND)
def apply_repurpose(
    db: Session, org: Organization, item: ApprovalItem, payload: dict
) -> None:
    job_id = str((payload or {}).get("job_id") or item.target_id or "")
    if not job_id:
        raise ValueError("Missing job id")
    cipher = OrgCipher.for_org(org)
    _release_job(db, org, job_id, cipher=cipher)
    job = db.get(ContentRepurposeJob, job_id)
    if job is not None:
        job.approval_item_id = item.id


@register_rejecter(TARGET_KIND)
def reject_repurpose(
    db: Session, org: Organization, item: ApprovalItem, payload: dict
) -> None:
    job_id = str((payload or {}).get("job_id") or item.target_id or "")
    job = db.get(ContentRepurposeJob, job_id) if job_id else None
    if job is None or job.tenant_id != org.id:
        return
    job.status = "rejected"
    job.approval_item_id = item.id
    for asset_id in job.asset_ids or []:
        asset = db.get(MarketingAsset, asset_id)
        if asset is not None and asset.tenant_id == org.id:
            asset.status = AssetStatus.REJECTED
    db.flush()


AGENT = ContentRepurposerAgent()
