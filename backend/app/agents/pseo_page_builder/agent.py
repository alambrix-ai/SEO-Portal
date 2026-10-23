"""Build programmatic pages from sandboxed templates + datasets via approvals."""
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
    ConnectorUse,
    UseMode,
)
from app.connectors.base.connector import Capability
from app.db.base import utcnow
from app.models.approval import ApprovalItem, ApprovalType
from app.models.pseo import PseoDataset, PseoPageJob, PseoTemplate
from app.models.seo import SeoPage
from app.models.workspace import Organization
from app.services.approvals import register_applier, register_rejecter
from app.services.encryption import Ctx, OrgCipher
from app.services.pseo import quality, render

TARGET_KIND = "pseo_page_job"
MAX_PROPOSE = 5


class PseoPageBuilderAgent(BaseAgent):
    spec = AgentSpec(
        slug="pseo_page_builder",
        name="Programmatic SEO Builder",
        category=AgentCategory.SEO_AEO,
        description=(
            "Renders sandboxed templates against dataset rows, rejects thin or "
            "duplicate pages, and proposes draft CMS publishes through the "
            "approval gate."
        ),
        default_interval=timedelta(days=1),
        default_schedule="Daily",
        required_capabilities=(Capability.CREATE_PAGE,),
        max_impact=Impact.HIGH,
        default_max_actions_per_day=20,
        scope_placeholder="Optional template slug to run",
        requires_llm=False,
        connector_use=(
            ConnectorUse(
                Capability.CREATE_PAGE,
                UseMode.ONE,
                question="Where should approved programmatic pages be drafted?",
            ),
        ),
    )

    def run(self, ctx: AgentContext) -> AgentResult:
        cms = ctx.with_capability(Capability.CREATE_PAGE)
        if cms is None:
            return AgentResult.skip("Waiting on a CMS or repository that can create pages")

        templates = list(
            ctx.db.execute(
                select(PseoTemplate).where(
                    PseoTemplate.tenant_id == ctx.tenant_id,
                    PseoTemplate.status == "active",
                )
            ).scalars()
        )
        if ctx.scope:
            slug = ctx.scope.strip().split(",")[0].strip()
            templates = [t for t in templates if t.slug == slug]
        if not templates:
            return AgentResult.skip("No active programmatic SEO templates")

        datasets = list(
            ctx.db.execute(
                select(PseoDataset).where(
                    PseoDataset.tenant_id == ctx.tenant_id,
                    PseoDataset.status == "ready",
                )
            ).scalars()
        )
        if not datasets:
            return AgentResult.skip("No ready programmatic SEO datasets")

        existing_pages = list(
            ctx.db.execute(
                select(SeoPage).where(SeoPage.tenant_id == ctx.tenant_id).limit(2000)
            ).scalars()
        )
        page_tokens = [
            quality.tokens(
                f"{p.title} {p.url} {' '.join(p.target_keywords or [])}"
            )
            for p in existing_pages
        ]
        existing_hashes = {
            job.content_hash
            for job in ctx.db.execute(
                select(PseoPageJob).where(
                    PseoPageJob.tenant_id == ctx.tenant_id,
                    PseoPageJob.content_hash != "",
                )
            ).scalars()
            if job.content_hash
        }
        existing_paths = {
            job.path
            for job in ctx.db.execute(
                select(PseoPageJob.path).where(PseoPageJob.tenant_id == ctx.tenant_id)
            ).scalars()
        } | {p.url for p in existing_pages}

        actions: list[AgentAction] = []
        skipped = 0
        for template in templates:
            source = ctx.cipher.decrypt(template.body_encrypted, context=Ctx.PSEO_TEMPLATE)
            if not source:
                continue
            for dataset in datasets:
                if len(actions) >= min(MAX_PROPOSE, max(ctx.remaining_actions, 1)):
                    break
                rows = ctx.cipher.decrypt_json(
                    dataset.rows_encrypted, context=Ctx.PSEO_DATASET, default=[]
                )
                try:
                    rows = quality.parse_dataset_rows(rows if isinstance(rows, list) else [])
                except ValueError:
                    continue
                for row in rows:
                    if len(actions) >= min(MAX_PROPOSE, max(ctx.remaining_actions, 1)):
                        break
                    try:
                        body = render.render_body(source, row)
                        path = render.render_path(template.path_pattern, row)
                        title = render.title_for(row, body)
                    except ValueError as exc:
                        skipped += 1
                        ctx.log_warning("pSEO render skipped: %s", exc)
                        continue
                    if path in existing_paths:
                        skipped += 1
                        continue
                    gate = quality.assess(
                        body,
                        title=title,
                        path=path,
                        existing_hashes=existing_hashes,
                        existing_page_tokens=page_tokens,
                    )
                    if not gate.ok:
                        skipped += 1
                        job = PseoPageJob(
                            tenant_id=ctx.tenant_id,
                            template_id=template.id,
                            dataset_id=dataset.id,
                            title=title[:255],
                            path=path,
                            body_encrypted="",
                            content_hash=gate.content_hash,
                            unique_tokens=gate.unique_tokens,
                            status="skipped",
                            skip_reason=gate.reason[:255],
                            proposed_at=utcnow(),
                            row_data={k: row[k] for k in list(row)[:20]},
                        )
                        ctx.db.add(job)
                        continue

                    job = PseoPageJob(
                        tenant_id=ctx.tenant_id,
                        template_id=template.id,
                        dataset_id=dataset.id,
                        title=title[:255],
                        path=path,
                        body_encrypted=ctx.cipher.encrypt(body, context=Ctx.PSEO_PAGE_BODY),
                        content_hash=gate.content_hash,
                        unique_tokens=gate.unique_tokens,
                        status="proposed",
                        proposed_at=utcnow(),
                        row_data={k: row[k] for k in list(row)[:20]},
                    )
                    ctx.db.add(job)
                    ctx.db.flush()
                    existing_hashes.add(gate.content_hash)
                    existing_paths.add(path)
                    actions.append(
                        AgentAction(
                            kind="publish_pseo_page",
                            title=f'Programmatic page: "{title}"',
                            impact=Impact.HIGH,
                            approval_type=ApprovalType.PSEO_PAGE.value,
                            target_kind=TARGET_KIND,
                            target_id=job.id,
                            payload={
                                "job_id": job.id,
                                "title": title,
                                "path": path,
                                "body": body,
                                "cms_slug": getattr(cms, "slug", ""),
                            },
                            apply=_make_publisher(job.id),
                            audit=f"published programmatic page {path}",
                            metrics={"pseo_pages": 1},
                            writes_via=(getattr(cms, "slug", ""),) if getattr(cms, "slug", "") else (),
                        )
                    )
        ctx.db.flush()
        if not actions and skipped:
            return AgentResult.skip(
                f"Rendered rows were rejected by quality gates ({skipped} skipped)"
            )
        if not actions:
            return AgentResult.skip("No new programmatic pages to propose")
        return AgentResult(
            summary=f"Proposed {len(actions)} programmatic page{'s' if len(actions) != 1 else ''}",
            actions=actions,
            metric_label=f"{len(actions)} page proposed",
            metrics={"pseo_pages_proposed": len(actions), "pseo_skipped": skipped},
        )


def _make_publisher(job_id: str):  # noqa: ANN202
    def apply(ctx: AgentContext) -> None:
        _publish_job(ctx.db, ctx.org, job_id, cipher=ctx.cipher, ctx=ctx)

    return apply


def _publish_job(
    db: Session,
    org: Organization,
    job_id: str,
    *,
    cipher: OrgCipher,
    ctx: AgentContext | None = None,
    payload: dict | None = None,
) -> None:
    from app.models.agent import AgentRecord
    from app.orchestration.runner import build_context

    job = db.get(PseoPageJob, job_id)
    if job is None or job.tenant_id != org.id:
        raise LookupError("The programmatic page job no longer exists")
    body = (payload or {}).get("body") or cipher.decrypt(
        job.body_encrypted, context=Ctx.PSEO_PAGE_BODY
    )
    title = str((payload or {}).get("title") or job.title)
    path = str((payload or {}).get("path") or job.path)
    if not body:
        raise ValueError("Programmatic page has no body to publish")

    if ctx is None:
        record = db.execute(
            select(AgentRecord).where(
                AgentRecord.tenant_id == org.id,
                AgentRecord.slug == "pseo_page_builder",
            )
        ).scalar_one_or_none()
        if record is None:
            raise LookupError("Programmatic SEO Builder is not installed")
        ctx = build_context(db, record, org, trigger="approval")

    cms = ctx.with_capability(Capability.CREATE_PAGE)
    if cms is None:
        from app.core.exceptions import ConnectorError

        raise ConnectorError("No connected system can create pages right now")

    slug = path.strip("/").replace("/", "-") or "page"
    create = getattr(cms, "create_page", None)
    if not callable(create):
        raise NotImplementedError(f"{cms.name} cannot create pages")
    remote_id, link = create(
        title=title,
        body_html=body,
        slug=slug[:120],
        meta_description="",
        status="draft",
    )
    job.status = "published"
    job.skip_reason = f"draft:{remote_id}:{link}"[:255]
    # Keep a synced SeoPage pointer so cannibalisation checks see it next run.
    page = SeoPage(
        tenant_id=org.id,
        url=link or path,
        title=title[:255],
        cms_id=str(remote_id),
        cms_slug=getattr(cms, "slug", "") or "",
        body_encrypted=cipher.encrypt(body, context=Ctx.PAGE_BODY),
        status="live",
        schema_types=[],
        missing_topics=[],
        target_keywords=[],
    )
    db.add(page)
    db.flush()


@register_applier(TARGET_KIND)
def apply_pseo_page(
    db: Session, org: Organization, item: ApprovalItem, payload: dict
) -> None:
    job_id = str((payload or {}).get("job_id") or item.target_id or "")
    if not job_id:
        raise ValueError("Missing job id")
    cipher = OrgCipher.for_org(org)
    _publish_job(db, org, job_id, cipher=cipher, payload=payload or {})
    job = db.get(PseoPageJob, job_id)
    if job is not None:
        job.approval_item_id = item.id


@register_rejecter(TARGET_KIND)
def reject_pseo_page(
    db: Session, org: Organization, item: ApprovalItem, payload: dict
) -> None:
    job_id = str((payload or {}).get("job_id") or item.target_id or "")
    job = db.get(PseoPageJob, job_id) if job_id else None
    if job is None or job.tenant_id != org.id:
        return
    job.status = "rejected"
    job.approval_item_id = item.id
    db.flush()


AGENT = PseoPageBuilderAgent()
