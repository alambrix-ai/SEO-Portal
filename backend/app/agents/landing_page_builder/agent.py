"""Draft landing pages and propose A/B starts — never split traffic silently."""
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
    ConnectorUse,
    Impact,
    UseMode,
)
from app.connectors.base.connector import Capability
from app.db.base import utcnow
from app.models.approval import ApprovalItem, ApprovalType
from app.models.landing import AbExperiment, LandingPage
from app.models.workspace import Organization
from app.services import landing_pages as lp_svc
from app.services.approvals import register_applier, register_rejecter
from app.services.encryption import Ctx, OrgCipher

TARGET_PAGE = "landing_page"
TARGET_EXPERIMENT = "ab_experiment"
SLUG = "landing_page_builder"
MAX_ACTIONS = 4


class LandingPageBuilderAgent(BaseAgent):
    spec = AgentSpec(
        slug=SLUG,
        name="Landing Page Builder",
        category=AgentCategory.SEO_AEO,
        description=(
            "Drafts landing pages to the CMS and proposes A/B experiments. "
            "Traffic splitting never starts without HIGH approval."
        ),
        default_interval=timedelta(days=1),
        default_schedule="Daily",
        required_capabilities=(Capability.CREATE_PAGE,),
        max_impact=Impact.HIGH,
        default_max_actions_per_day=10,
        scope_placeholder="Optional campaign title for a new control/variant pair",
        requires_llm=False,
        connector_use=(
            ConnectorUse(
                Capability.CREATE_PAGE,
                UseMode.ONE,
                question="Where should approved landing pages be drafted?",
            ),
        ),
    )

    def run(self, ctx: AgentContext) -> AgentResult:
        cms = ctx.with_capability(Capability.CREATE_PAGE)
        if cms is None:
            return AgentResult.skip("Waiting on a CMS or repository that can create pages")

        actions: list[AgentAction] = []

        # 1) Optional: build a fresh control/variant pair from scope.
        if ctx.scope and len(actions) < MAX_ACTIONS:
            actions.extend(self._propose_pair_from_scope(ctx, cms))

        # 2) Propose publish for draft pages not yet proposed.
        for page in self._draft_pages(ctx):
            if len(actions) >= min(MAX_ACTIONS, max(ctx.remaining_actions, 1)):
                break
            if page.status != "draft":
                continue
            page.status = "proposed"
            page.proposed_at = utcnow()
            body = ctx.cipher.decrypt(page.body_encrypted, context=Ctx.LANDING_BODY) or ""
            actions.append(
                AgentAction(
                    kind="publish_landing_page",
                    title=f'Publish landing page draft: "{page.title}"',
                    impact=Impact.HIGH,
                    approval_type=ApprovalType.LANDING_PAGE.value,
                    target_kind=TARGET_PAGE,
                    target_id=page.id,
                    payload={
                        "page_id": page.id,
                        "title": page.title,
                        "path": page.path,
                        "body": body,
                        "cms_slug": getattr(cms, "slug", ""),
                    },
                    apply=_make_page_publisher(page.id),
                    audit=f"published landing page {page.path}",
                    metrics={"landing_pages": 1},
                    writes_via=(getattr(cms, "slug", ""),) if getattr(cms, "slug", "") else (),
                )
            )

        # 3) Propose starting experiments that are still proposed (never auto-start).
        for experiment in self._proposed_experiments(ctx):
            if len(actions) >= min(MAX_ACTIONS, max(ctx.remaining_actions, 1)):
                break
            actions.append(
                AgentAction(
                    kind="start_ab_experiment",
                    title=f'Start A/B test: "{experiment.name}" ({experiment.variant_weight}% variant)',
                    impact=Impact.HIGH,
                    approval_type=ApprovalType.AB_EXPERIMENT.value,
                    target_kind=TARGET_EXPERIMENT,
                    target_id=experiment.id,
                    payload={
                        "experiment_id": experiment.id,
                        "slug": experiment.slug,
                        "variant_weight": experiment.variant_weight,
                        "metric_key": experiment.metric_key,
                        "control_page_id": experiment.control_page_id,
                        "variant_page_id": experiment.variant_page_id,
                    },
                    apply=_make_experiment_starter(experiment.id),
                    audit=f"started A/B experiment {experiment.slug}",
                    metrics={"ab_experiments_started": 1},
                )
            )

        ctx.db.flush()
        if not actions:
            return AgentResult.skip("No landing page drafts or A/B starts to propose")
        return AgentResult(
            summary=f"Proposed {len(actions)} landing / experiment action{'s' if len(actions) != 1 else ''}",
            actions=actions,
            metric_label=f"{len(actions)} action proposed",
            metrics={"landing_actions_proposed": len(actions)},
        )

    def _propose_pair_from_scope(self, ctx: AgentContext, cms) -> list[AgentAction]:  # noqa: ANN001
        title = (ctx.scope or "").strip().split(",")[0].strip()[:120]
        if not title:
            return []
        try:
            control = lp_svc.build_landing_body(
                title=f"{title} (control)",
                headline=title,
                body=(
                    f"Primary landing experience for {title}. Clear value proposition, "
                    "social proof placeholders, and a single conversion path for visitors."
                ),
                cta="Get started",
            )
            variant = lp_svc.build_landing_body(
                title=f"{title} (variant)",
                headline=f"{title} — faster path",
                body=(
                    f"Alternate landing experience for {title}. Emphasises a shorter form, "
                    "stronger call to action, and a focused benefit statement above the fold."
                ),
                cta="Start free",
            )
            experiment_meta = lp_svc.validate_experiment(
                name=f"{title} CTR test",
                hypothesis=f"A stronger CTA improves conversions for {title}",
                variant_weight=50,
                metric_key="conversions",
            )
        except ValueError:
            return []

        existing = {
            p.path
            for p in ctx.db.execute(
                select(LandingPage.path).where(LandingPage.tenant_id == ctx.tenant_id)
            ).scalars()
        }
        if control.path in existing or variant.path in existing:
            return []

        control_page = LandingPage(
            tenant_id=ctx.tenant_id,
            title=control.title,
            path=control.path,
            body_encrypted=ctx.cipher.encrypt(control.body_html, context=Ctx.LANDING_BODY),
            status="draft",
        )
        variant_page = LandingPage(
            tenant_id=ctx.tenant_id,
            title=variant.title,
            path=variant.path,
            body_encrypted=ctx.cipher.encrypt(variant.body_html, context=Ctx.LANDING_BODY),
            status="draft",
        )
        ctx.db.add_all([control_page, variant_page])
        ctx.db.flush()
        experiment = AbExperiment(
            tenant_id=ctx.tenant_id,
            slug=experiment_meta.slug,
            name=experiment_meta.name,
            hypothesis=experiment_meta.hypothesis,
            control_page_id=control_page.id,
            variant_page_id=variant_page.id,
            variant_weight=experiment_meta.variant_weight,
            metric_key=experiment_meta.metric_key,
            status="proposed",
            detail={"cms_slug": getattr(cms, "slug", "")},
        )
        ctx.db.add(experiment)
        ctx.db.flush()
        return []  # publish + start proposed on subsequent loops / same run below

    def _draft_pages(self, ctx: AgentContext) -> list[LandingPage]:
        return list(
            ctx.db.execute(
                select(LandingPage).where(
                    LandingPage.tenant_id == ctx.tenant_id,
                    LandingPage.status == "draft",
                ).limit(10)
            ).scalars()
        )

    def _proposed_experiments(self, ctx: AgentContext) -> list[AbExperiment]:
        return list(
            ctx.db.execute(
                select(AbExperiment).where(
                    AbExperiment.tenant_id == ctx.tenant_id,
                    AbExperiment.status == "proposed",
                ).limit(10)
            ).scalars()
        )


def _make_page_publisher(page_id: str):  # noqa: ANN202
    def apply(ctx: AgentContext) -> None:
        _publish_page(ctx.db, ctx.org, page_id, cipher=ctx.cipher, ctx=ctx)

    return apply


def _make_experiment_starter(experiment_id: str):  # noqa: ANN202
    def apply(ctx: AgentContext) -> None:
        _start_experiment(ctx.db, ctx.org, experiment_id)

    return apply


def _publish_page(
    db: Session,
    org: Organization,
    page_id: str,
    *,
    cipher: OrgCipher,
    ctx: AgentContext | None = None,
    payload: dict | None = None,
) -> None:
    from app.models.agent import AgentRecord
    from app.orchestration.runner import build_context

    page = db.get(LandingPage, page_id)
    if page is None or page.tenant_id != org.id:
        raise LookupError("Landing page no longer exists")
    body = (payload or {}).get("body") or cipher.decrypt(
        page.body_encrypted, context=Ctx.LANDING_BODY
    )
    title = str((payload or {}).get("title") or page.title)
    if not body:
        raise ValueError("Landing page has no body")

    if ctx is None:
        record = db.execute(
            select(AgentRecord).where(
                AgentRecord.tenant_id == org.id,
                AgentRecord.slug == SLUG,
            )
        ).scalar_one_or_none()
        if record is None:
            raise LookupError("Landing Page Builder is not installed")
        ctx = build_context(db, record, org, trigger="approval")

    cms = ctx.with_capability(Capability.CREATE_PAGE)
    if cms is None:
        from app.core.exceptions import ConnectorError

        raise ConnectorError("No connected system can create pages right now")
    create = getattr(cms, "create_page", None)
    if not callable(create):
        raise NotImplementedError(f"{cms.name} cannot create pages")
    slug = page.path.strip("/").replace("/", "-") or "landing"
    remote_id, link = create(
        title=title,
        body_html=body,
        slug=slug[:120],
        meta_description="",
        status="draft",
    )
    page.status = "live"
    page.cms_id = str(remote_id)
    page.cms_slug = getattr(cms, "slug", "") or ""
    page.cms_url = (link or "")[:1000]
    db.flush()


def _start_experiment(db: Session, org: Organization, experiment_id: str) -> None:
    experiment = db.get(AbExperiment, experiment_id)
    if experiment is None or experiment.tenant_id != org.id:
        raise LookupError("Experiment no longer exists")
    if experiment.status == "running":
        return
    if experiment.status not in {"proposed"}:
        raise ValueError("Only proposed experiments can be started")
    # Require both pages to exist; traffic split is metadata until CMS supports it.
    control = db.get(LandingPage, experiment.control_page_id)
    variant = db.get(LandingPage, experiment.variant_page_id)
    if control is None or variant is None:
        raise LookupError("Experiment pages are missing")
    experiment.status = "running"
    experiment.started_at = utcnow()
    experiment.detail = {
        **(experiment.detail or {}),
        "traffic_split_approved": True,
        "variant_weight": experiment.variant_weight,
    }
    db.flush()


@register_applier(TARGET_PAGE)
def apply_landing_page(
    db: Session, org: Organization, item: ApprovalItem, payload: dict
) -> None:
    page_id = str((payload or {}).get("page_id") or item.target_id or "")
    if not page_id:
        raise ValueError("Missing page id")
    cipher = OrgCipher.for_org(org)
    _publish_page(db, org, page_id, cipher=cipher, payload=payload or {})
    page = db.get(LandingPage, page_id)
    if page is not None:
        page.approval_item_id = item.id


@register_rejecter(TARGET_PAGE)
def reject_landing_page(
    db: Session, org: Organization, item: ApprovalItem, payload: dict
) -> None:
    page_id = str((payload or {}).get("page_id") or item.target_id or "")
    page = db.get(LandingPage, page_id) if page_id else None
    if page is None or page.tenant_id != org.id:
        return
    page.status = "archived"
    page.approval_item_id = item.id
    db.flush()


@register_applier(TARGET_EXPERIMENT)
def apply_ab_experiment(
    db: Session, org: Organization, item: ApprovalItem, payload: dict
) -> None:
    experiment_id = str((payload or {}).get("experiment_id") or item.target_id or "")
    if not experiment_id:
        raise ValueError("Missing experiment id")
    _start_experiment(db, org, experiment_id)
    experiment = db.get(AbExperiment, experiment_id)
    if experiment is not None:
        experiment.approval_item_id = item.id


@register_rejecter(TARGET_EXPERIMENT)
def reject_ab_experiment(
    db: Session, org: Organization, item: ApprovalItem, payload: dict
) -> None:
    experiment_id = str((payload or {}).get("experiment_id") or item.target_id or "")
    experiment = db.get(AbExperiment, experiment_id) if experiment_id else None
    if experiment is None or experiment.tenant_id != org.id:
        return
    experiment.status = "rejected"
    experiment.approval_item_id = item.id
    experiment.ended_at = utcnow()
    db.flush()


AGENT = LandingPageBuilderAgent()
