"""Plan topic clusters from Search Console + page keywords; propose briefs."""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import delete, select
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
from app.agents.topic_cluster_planner import prompts
from app.connectors.base.connector import Capability
from app.db.base import utcnow
from app.models.approval import ApprovalItem, ApprovalStatus, ApprovalType
from app.models.seo import SeoPage
from app.models.topic_cluster import ContentBrief, TopicCluster, TopicClusterPage
from app.models.workspace import Organization
from app.services import topic_clusters as cluster_svc
from app.services.approvals import register_applier, register_rejecter
from app.services.encryption import Ctx, OrgCipher

TARGET_KIND = "content_brief"
MAX_BRIEF_ACTIONS = 3


class TopicClusterPlannerAgent(BaseAgent):
    spec = AgentSpec(
        slug="topic_cluster_planner",
        name="Topic Cluster Planner",
        category=AgentCategory.SEO_AEO,
        description=(
            "Groups Search Console queries and page keywords into topic "
            "clusters, spots coverage gaps, and proposes content briefs "
            "through the approval gate for the article writer."
        ),
        default_interval=timedelta(days=1),
        default_schedule="Daily",
        required_capabilities=(Capability.READ_SEARCH_PERFORMANCE,),
        max_impact=Impact.HIGH,
        default_max_actions_per_day=10,
        scope_placeholder="Optional focus terms, comma-separated",
        requires_llm=False,
        connector_use=(
            ConnectorUse(
                Capability.READ_SEARCH_PERFORMANCE,
                UseMode.ONE,
                question="Which search performance source should seed topic clusters?",
            ),
        ),
    )

    def run(self, ctx: AgentContext) -> AgentResult:
        search = ctx.with_capability(Capability.READ_SEARCH_PERFORMANCE)
        queries: list[str] = []
        if search is not None:
            ctx.progress("Reading Search Console queries")
            try:
                data = search.read_search_performance(days=28)  # type: ignore[union-attr]
                for row in (data or {}).get("top_queries") or []:
                    q = str((row or {}).get("query") or (row if isinstance(row, str) else "")).strip()
                    if q:
                        queries.append(q)
            except Exception as exc:  # noqa: BLE001
                return AgentResult.skip(f"Could not read search performance: {exc}")

        pages = list(
            ctx.db.execute(
                select(SeoPage).where(
                    SeoPage.tenant_id == ctx.tenant_id,
                ).limit(500)
            ).scalars()
        )
        page_refs = [
            cluster_svc.PageRef(
                id=page.id,
                url=page.url,
                title=page.title,
                keywords=tuple(str(k) for k in (page.target_keywords or [])[:12]),
            )
            for page in pages
        ]
        drafts = cluster_svc.cluster_queries(queries, page_refs)
        if not drafts:
            return AgentResult.skip("No topic clusters could be formed from queries or keywords")

        self._replace_clusters(ctx, drafts)
        actions = self._propose_briefs(ctx, drafts, pages)
        ctx.db.flush()
        return AgentResult(
            summary=(
                f"Mapped {len(drafts)} topic cluster{'s' if len(drafts) != 1 else ''}; "
                f"proposed {len(actions)} brief{'s' if len(actions) != 1 else ''}"
            ),
            actions=actions,
            metric_label=f"{len(drafts)} clusters",
            metrics={
                "topic_clusters": len(drafts),
                "content_briefs_proposed": len(actions),
            },
            detail={"clusters": len(drafts), "briefs": len(actions)},
        )

    def _replace_clusters(self, ctx: AgentContext, drafts: list[cluster_svc.ClusterDraft]) -> None:
        # Rebuild open clusters each run so the map stays current; keep briefs.
        ctx.db.execute(
            delete(TopicClusterPage).where(TopicClusterPage.tenant_id == ctx.tenant_id)
        )
        ctx.db.execute(
            delete(TopicCluster).where(
                TopicCluster.tenant_id == ctx.tenant_id,
                TopicCluster.status == "open",
            )
        )
        ctx.db.flush()
        for draft in drafts:
            cluster = TopicCluster(
                tenant_id=ctx.tenant_id,
                name=draft.name,
                seed_terms=list(draft.seed_terms),
                gap_score=draft.gap_score,
                page_count=len(draft.page_ids),
                status="open",
                detected_at=utcnow(),
            )
            ctx.db.add(cluster)
            ctx.db.flush()
            for page_id in draft.page_ids:
                ctx.db.add(
                    TopicClusterPage(
                        tenant_id=ctx.tenant_id,
                        cluster_id=cluster.id,
                        page_id=page_id,
                        relevance=draft.page_relevance.get(page_id, 0.0),
                    )
                )
            draft.cluster_id = cluster.id
        ctx.db.flush()

    def _propose_briefs(
        self,
        ctx: AgentContext,
        drafts: list[cluster_svc.ClusterDraft],
        pages: list[SeoPage],
    ) -> list[AgentAction]:
        titles = [page.title for page in pages]
        actions: list[AgentAction] = []
        # Prefer highest gap clusters that still lack an open brief.
        gapped = sorted(drafts, key=lambda d: -d.gap_score)
        for draft in gapped:
            if len(actions) >= min(MAX_BRIEF_ACTIONS, max(ctx.remaining_actions, 1)):
                break
            if draft.gap_score < 40:
                continue
            cluster_id = draft.cluster_id
            if cluster_id is None:
                continue
            if self._pending_brief(ctx, cluster_id):
                continue

            outline = cluster_svc.brief_outline(
                cluster_name=draft.name,
                seed_terms=list(draft.seed_terms),
                existing_titles=titles,
            )
            # Optional polish when a model is wired; deterministic outline is enough.
            try:
                polished = ctx.ask_json(
                    prompts.polish_brief(
                        cluster_name=draft.name,
                        seed_terms=list(draft.seed_terms),
                        existing_titles=titles,
                        draft_body=outline["body"],
                    ),
                    system=prompts.SYSTEM,
                    schema_hint=prompts.SCHEMA_HINT,
                )
                if isinstance(polished, dict) and polished.get("body"):
                    outline["title"] = str(polished.get("title") or outline["title"])[:255]
                    outline["body"] = str(polished["body"])[:20_000]
                    kws = polished.get("target_keywords")
                    if isinstance(kws, list) and kws:
                        outline["target_keywords"] = [str(k)[:120] for k in kws[:24]]
            except Exception as exc:  # noqa: BLE001
                ctx.log_warning("Brief polish skipped: %s", exc)

            brief = ContentBrief(
                tenant_id=ctx.tenant_id,
                cluster_id=cluster_id,
                title=outline["title"],
                target_keywords=list(outline["target_keywords"]),
                body_encrypted=ctx.cipher.encrypt(outline["body"], context=Ctx.CONTENT_BRIEF),
                status="proposed",
            )
            ctx.db.add(brief)
            ctx.db.flush()

            actions.append(
                AgentAction(
                    kind="approve_content_brief",
                    title=f'Content brief: "{brief.title}"',
                    impact=Impact.HIGH,
                    approval_type=ApprovalType.CONTENT_BRIEF.value,
                    target_kind=TARGET_KIND,
                    target_id=brief.id,
                    payload={
                        "brief_id": brief.id,
                        "cluster_id": cluster_id,
                        "cluster_name": draft.name,
                        "title": brief.title,
                        "target_keywords": list(brief.target_keywords or []),
                        "body": outline["body"],
                    },
                    apply=_make_brief_applier(brief.id),
                    audit=f"approved content brief for cluster {draft.name}",
                    metrics={"content_briefs": 1},
                )
            )
        return actions

    def _pending_brief(self, ctx: AgentContext, cluster_id: str) -> bool:
        pending = ctx.db.execute(
            select(ApprovalItem.id)
            .join(ContentBrief, ContentBrief.id == ApprovalItem.target_id)
            .where(
                ApprovalItem.tenant_id == ctx.tenant_id,
                ApprovalItem.status == ApprovalStatus.PENDING.value,
                ApprovalItem.target_kind == TARGET_KIND,
                ContentBrief.cluster_id == cluster_id,
            )
            .limit(1)
        ).scalar_one_or_none()
        return pending is not None


def _make_brief_applier(brief_id: str):  # noqa: ANN202
    def apply(ctx: AgentContext) -> None:
        _approve_brief(ctx.db, ctx.org, brief_id, cipher=ctx.cipher, payload=None)

    return apply


def _approve_brief(
    db: Session,
    org: Organization,
    brief_id: str,
    *,
    cipher: OrgCipher,
    payload: dict | None,
) -> None:
    brief = db.get(ContentBrief, brief_id)
    if brief is None or brief.tenant_id != org.id:
        raise LookupError("The content brief no longer exists")
    if payload:
        data = cluster_svc.validate_brief_payload({**payload, "brief_id": brief_id})
        brief.title = data["title"]
        brief.target_keywords = data["target_keywords"]
        brief.body_encrypted = cipher.encrypt(data["body"], context=Ctx.CONTENT_BRIEF)
    brief.status = "approved"
    if brief.cluster_id:
        cluster = db.get(TopicCluster, brief.cluster_id)
        if cluster is not None and cluster.tenant_id == org.id:
            cluster.status = "planned"
    db.flush()


@register_applier(TARGET_KIND)
def apply_content_brief(
    db: Session, org: Organization, item: ApprovalItem, payload: dict
) -> None:
    data = cluster_svc.validate_brief_payload(
        {**(payload or {}), "brief_id": (payload or {}).get("brief_id") or item.target_id}
    )
    cipher = OrgCipher.for_org(org)
    _approve_brief(db, org, data["brief_id"], cipher=cipher, payload=data)
    brief = db.get(ContentBrief, data["brief_id"])
    if brief is not None:
        brief.approval_item_id = item.id


@register_rejecter(TARGET_KIND)
def reject_content_brief(
    db: Session, org: Organization, item: ApprovalItem, payload: dict
) -> None:
    brief_id = str((payload or {}).get("brief_id") or item.target_id or "")
    brief = db.get(ContentBrief, brief_id) if brief_id else None
    if brief is None or brief.tenant_id != org.id:
        return
    brief.status = "rejected"
    brief.approval_item_id = item.id
    db.flush()


AGENT = TopicClusterPlannerAgent()
