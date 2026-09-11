"""Knowledge Graph & Schema — builds and patches JSON-LD as content shifts.

Structured data goes stale silently: a page gains a price, loses a FAQ, or has
its trim levels rewritten, and the graph still describes last month's page.
This agent decides which schema types a page *should* carry from what is
actually on it, generates the JSON-LD, and patches rather than duplicates —
a new ``Product`` graph supersedes the previous one via ``replaces_patch_id``.

Schema injection is high impact: a malformed graph can lose rich results
outright, so under the hybrid guardrail patches queue for review.
"""
from __future__ import annotations

import json
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.agents.base.context import AgentContext
from app.agents.base.style import humanise_json
from app.agents.base import scope as scope_rules
from app.agents.base.contracts import (
    AgentAction,
    AgentCategory,
    AgentResult,
    AgentSpec,
    BaseAgent,
    Impact,
)
from app.agents.knowledge_graph_schema import prompts
from app.connectors.base.connector import Capability
from app.connectors.base.interfaces import CmsConnector
from app.core.logging import get_logger
from app.db.base import utcnow
from app.models.approval import ApprovalItem, ApprovalType
from app.models.seo import SchemaPatch, SeoPage
from app.models.workspace import Organization
from app.services.approvals import register_applier
from app.services.encryption import Ctx

log = get_logger(__name__)

PAGES_PER_RUN = 5

# Which schema types a page's shape implies. Order matters: the first match
# supplies the page's primary type, the rest are additive.
_SIGNALS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Product", ("price", "msrp", "trim", "model", "buy", "inventory", "stock")),
    ("LocalBusiness", ("location", "hours", "address", "directions", "visit us", "showroom")),
    ("ItemList", ("lineup", "range", "compare", "options", "collection", "models")),
    ("Service", ("service", "repair", "maintenance", "booking", "appointment")),
    ("FAQPage", ("faq", "question", "frequently asked", "what is", "how do")),
)


class KnowledgeGraphSchemaAgent(BaseAgent):
    spec = AgentSpec(
        slug="knowledge_graph_schema",
        name="Knowledge Graph & Schema",
        category=AgentCategory.SEO_AEO,
        description="Injects and patches JSON-LD (FAQ, Product, LocalBusiness).",
        default_interval=timedelta(hours=12),
        default_schedule="Daily",
        required_capabilities=(Capability.WRITE_PAGE,),
        max_impact=Impact.HIGH,
        default_max_actions_per_day=15,
        scope_placeholder="Product, FAQPage — or blank to infer per page",
        requires_llm=True,
    )

    #: Common types, offered as examples. **Not** an allow-list: schema.org
    #: has hundreds and a client could legitimately want any of them.
    COMMON_TYPES = (
        "Product", "FAQPage", "LocalBusiness", "Organization", "Article",
        "BreadcrumbList", "Service", "Offer", "Review", "Event", "Course",
        "Recipe", "JobPosting", "Vehicle", "RealEstateListing", "MedicalClinic",
        "SoftwareApplication", "Book", "Movie", "Restaurant", "HowTo",
    )

    def validate_scope(self, scope: str) -> str | None:
        """Schema types to emit, e.g. ``Product, FAQPage``.

        The **shape** is checked, not membership of a list. An earlier version
        allowed ten types, which quietly decided this platform was for car
        dealers: a recipe site, a university, a clinic or a job board would
        have been refused a type it legitimately needed. schema.org has
        hundreds and clients are in every vertical.

        So anything that looks like a schema.org type is accepted, and only
        input that cannot be one — a phrase, punctuation, a lowercase word —
        is refused. That still catches the typo worth catching.
        """
        return scope_rules.type_names(scope, examples=self.COMMON_TYPES)

    def run(self, ctx: AgentContext) -> AgentResult:
        actions = self._build_patches(ctx)

        queued = ctx.db.execute(
            select(func.count())
            .select_from(SchemaPatch)
            .where(SchemaPatch.tenant_id == ctx.tenant_id, SchemaPatch.status == "queued")
        ).scalar_one()

        return AgentResult(
            summary=f"{len(actions)} schema patches prepared",
            actions=actions,
            metric_label=f"{queued} schema patches queued",
            detail={"prepared": len(actions), "queued_total": queued},
        )

    # ── Building ───────────────────────────────────────────────────────────
    def _build_patches(self, ctx: AgentContext) -> list[AgentAction]:
        pages = self._pages_to_review(ctx)
        actions: list[AgentAction] = []

        for page in pages:
            body = ctx.cipher.decrypt(page.body_encrypted, context=Ctx.PAGE_BODY)
            if not body:
                continue

            wanted = self._wanted_types(ctx, body, page)
            if not wanted:
                continue

            try:
                data = ctx.ask_json(
                    prompts.schema_graph(
                        title=page.title,
                        url=self._absolute(ctx, page.url),
                        body=body,
                        schema_types=wanted,
                        organization=ctx.org.name,
                    ),
                    system=prompts.SYSTEM,
                    schema_hint=prompts.SCHEMA_HINT,
                )
            except Exception as exc:  # noqa: BLE001
                ctx.log_warning("Schema generation failed for %s: %s", page.url, exc)
                continue

            existing = {
                patch.schema_type: patch
                for patch in ctx.db.execute(
                    select(SchemaPatch).where(
                        SchemaPatch.tenant_id == ctx.tenant_id,
                        SchemaPatch.page_id == page.id,
                        SchemaPatch.status == "injected",
                    )
                ).scalars()
            }

            for entry in data.get("patches") or []:
                schema_type = (entry.get("schema_type") or "").strip()
                json_ld = entry.get("json_ld")
                if not schema_type or not isinstance(json_ld, dict):
                    continue
                # The prose inside the graph publishes to the page too, so
                # the values are cleaned, not just the wrapper.
                json_ld = humanise_json(json_ld)

                problem = prompts.validate_json_ld(json_ld, schema_type)
                if problem:
                    # A graph that would not validate is dropped rather than
                    # published — a broken graph is worse than none.
                    ctx.log_warning("Rejected %s graph for %s: %s", schema_type, page.url, problem)
                    continue

                superseded = existing.get(schema_type)
                if superseded is not None and self._unchanged(ctx, superseded, json_ld):
                    continue

                patch = SchemaPatch(
                    tenant_id=ctx.tenant_id,
                    page_id=page.id,
                    schema_type=schema_type,
                    json_ld_encrypted=ctx.cipher.encrypt_json(
                        json_ld, context=Ctx.SCHEMA_JSONLD
                    ),
                    status="queued",
                    replaces_patch_id=superseded.id if superseded else None,
                )
                ctx.db.add(patch)
                ctx.db.flush()

                verb = "Patch" if superseded else "Inject"
                actions.append(
                    AgentAction(
                        kind="inject_schema",
                        title=f'{verb} {schema_type} schema on "{page.title}"',
                        impact=Impact.HIGH,
                        approval_type=ApprovalType.SCHEMA_PATCH.value,
                        target_kind="schema_patch",
                        target_id=patch.id,
                        payload={
                            "patch_id": patch.id,
                            "page_id": page.id,
                            "url": page.url,
                            "schema_type": schema_type,
                            "json_ld": json_ld,
                        },
                        apply=_make_injector(patch.id),
                        audit=f"injected {schema_type} schema on {page.url}",
                    )
                )

        ctx.db.flush()
        return actions

    def _pages_to_review(self, ctx: AgentContext) -> list[SeoPage]:
        """Pages changed since their graph was written, oldest first."""
        return list(
            ctx.db.execute(
                select(SeoPage)
                .where(SeoPage.tenant_id == ctx.tenant_id)
                .order_by(SeoPage.updated_at.desc())
                .limit(PAGES_PER_RUN)
            ).scalars()
        )

    def _wanted_types(self, ctx: AgentContext, body: str, page: SeoPage) -> list[str]:
        """Schema types this page should carry.

        An explicit scope wins; otherwise the types are inferred from what the
        copy contains, so a service page does not get a Product graph.
        """
        if ctx.scope:
            return [t.strip() for t in ctx.scope.split(",") if t.strip()]

        haystack = f"{page.title} {page.url} {body}".lower()
        wanted = [
            schema_type
            for schema_type, needles in _SIGNALS
            if any(needle in haystack for needle in needles)
        ]
        # Every page can carry a breadcrumb trail, and an FAQ graph is the
        # natural companion to whatever the Q&A injector added.
        if prompts.SECTION_MARKER in body and "FAQPage" not in wanted:
            wanted.append("FAQPage")
        return wanted[:3]

    def _unchanged(self, ctx: AgentContext, patch: SchemaPatch, json_ld: dict) -> bool:
        current = ctx.cipher.decrypt_json(
            patch.json_ld_encrypted, context=Ctx.SCHEMA_JSONLD, default={}
        )
        return json.dumps(current, sort_keys=True) == json.dumps(json_ld, sort_keys=True)

    def _absolute(self, ctx: AgentContext, path: str) -> str:
        if path.startswith("http"):
            return path
        domain = ctx.org.primary_domain or "example.com"
        return f"https://{domain.rstrip('/')}{path}"


# ── Applying a patch ───────────────────────────────────────────────────────
def _make_injector(patch_id: str):  # noqa: ANN202
    def inject(ctx: AgentContext) -> None:
        patch = ctx.db.get(SchemaPatch, patch_id)
        if patch is None:
            return
        page = ctx.db.get(SeoPage, patch.page_id)
        if page is None:
            return

        json_ld = ctx.cipher.decrypt_json(
            patch.json_ld_encrypted, context=Ctx.SCHEMA_JSONLD, default={}
        )
        if not json_ld:
            return

        # Only the system this page came from. There is no safe
        # guess: with_capability would hand back the first writer in
        # catalogue order, which in a workspace with two content
        # systems means publishing to the wrong website and
        # reporting that it worked.
        cms = ctx.optional_connector(page.cms_slug)
        if cms is None:
            from app.core.exceptions import ConnectorError

            raise ConnectorError(
                f"{ctx.connector_name(page.cms_slug)} is the system "
                f"this page came from, and it is not connected, so this structured data "
                f"cannot be published. Reconnect it and approve this again."
            )

        writer: CmsConnector = cms  # type: ignore[assignment]
        try:
            writer.inject_schema(page.cms_id, json_ld=json_ld)
        except NotImplementedError:
            # The CMS has no structured-data field, so the graph goes into the
            # body as a script tag instead — same result for a crawler.
            body = ctx.cipher.decrypt(page.body_encrypted, context=Ctx.PAGE_BODY)
            merged = prompts.merge_script_tag(body, json_ld, patch.schema_type)
            writer.write_page(page.cms_id, body=merged, title=page.title)
            page.body_encrypted = ctx.cipher.encrypt(merged, context=Ctx.PAGE_BODY)

        if patch.replaces_patch_id:
            superseded = ctx.db.get(SchemaPatch, patch.replaces_patch_id)
            if superseded is not None:
                superseded.status = "rejected"

        patch.status = "injected"
        patch.injected_at = utcnow()
        types = set(page.schema_types or [])
        types.add(patch.schema_type)
        page.schema_types = sorted(types)
        ctx.db.flush()

    return inject


@register_applier("schema_patch")
def apply_schema_patch(
    db: Session, org: Organization, item: ApprovalItem, payload: dict
) -> None:
    from app.models.agent import AgentRecord
    from app.orchestration.runner import build_context

    record = db.execute(
        select(AgentRecord).where(
            AgentRecord.tenant_id == org.id, AgentRecord.slug == "knowledge_graph_schema"
        )
    ).scalar_one_or_none()
    if record is None:
        raise LookupError("Knowledge Graph & Schema is not installed in this workspace")

    ctx = build_context(db, record, org, trigger="approval")
    _make_injector(payload.get("patch_id") or item.target_id or "")(ctx)


AGENT = KnowledgeGraphSchemaAgent()
