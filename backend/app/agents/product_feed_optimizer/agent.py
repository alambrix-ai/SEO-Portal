"""Propose Merchant-oriented product fixes and Product JSON-LD through approvals."""
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
from app.models.creative import Product
from app.models.product_feed import ProductFeedFinding
from app.models.seo import SchemaPatch, SeoPage
from app.models.workspace import Organization
from app.services import product_feed as feed_svc
from app.services.approvals import register_applier, register_rejecter
from app.services.encryption import Ctx, OrgCipher

TARGET_KIND = "product_feed_finding"
MAX_ACTIONS = 5


class ProductFeedOptimizerAgent(BaseAgent):
    spec = AgentSpec(
        slug="product_feed_optimizer",
        name="Product Feed Optimizer",
        category=AgentCategory.SEO_AEO,
        description=(
            "Checks workspace products for Merchant-ready gaps and proposes "
            "description/URL fixes plus Product JSON-LD through the approval gate."
        ),
        default_interval=timedelta(days=1),
        default_schedule="Daily",
        required_capabilities=(),
        max_impact=Impact.HIGH,
        default_max_actions_per_day=15,
        requires_llm=False,
    )

    def run(self, ctx: AgentContext) -> AgentResult:
        products = list(
            ctx.db.execute(
                select(Product).where(
                    Product.tenant_id == ctx.tenant_id,
                    Product.is_archived.is_(False),
                ).limit(200)
            ).scalars()
        )
        if not products:
            return AgentResult.skip("No active products in the workspace catalogue")

        actions: list[AgentAction] = []
        findings_open = 0
        for product in products:
            if len(actions) >= min(MAX_ACTIONS, max(ctx.remaining_actions, 1)):
                break
            issues = feed_svc.inspect_product(
                name=product.name,
                description=product.description or "",
                url=product.url or "",
                price_text=product.price_text or "",
                approved_claims=list(product.approved_claims or []),
            )
            for issue in issues:
                findings_open += 1
                finding = self._upsert_finding(ctx, product, issue)
                if finding.status == "proposed":
                    continue
                if not issue.patch and issue.kind != "missing_description":
                    # Informational only until an operator supplies data.
                    continue
                if len(actions) >= min(MAX_ACTIONS, max(ctx.remaining_actions, 1)):
                    break

                json_ld = None
                if (product.url or "").strip() and (product.description or issue.patch.get("description")):
                    try:
                        json_ld = feed_svc.build_product_json_ld(
                            name=product.name,
                            description=str(
                                issue.patch.get("description") or product.description or ""
                            ),
                            url=product.url or "",
                            price_text=product.price_text or "",
                        )
                    except ValueError:
                        json_ld = None

                finding.status = "proposed"
                finding.proposed_patch = {
                    "patch": issue.patch,
                    "json_ld": json_ld,
                }
                ctx.db.flush()
                actions.append(
                    AgentAction(
                        kind="apply_product_feed_fix",
                        title=f'Product fix: {issue.kind.replace("_", " ")} on "{product.name}"',
                        impact=Impact.HIGH,
                        approval_type=ApprovalType.PRODUCT_FEED_FIX.value,
                        target_kind=TARGET_KIND,
                        target_id=finding.id,
                        payload={
                            "finding_id": finding.id,
                            "product_id": product.id,
                            "kind": issue.kind,
                            "patch": issue.patch,
                            "json_ld": json_ld,
                            "summary": issue.summary,
                        },
                        apply=_make_applier(finding.id),
                        audit=f"applied product feed fix ({issue.kind}) on {product.name}",
                        metrics={"product_feed_fixes": 1},
                    )
                )

        ctx.db.flush()
        if not actions:
            return AgentResult(
                summary=f"Reviewed {len(products)} products; no auto-fixable gaps",
                metric_label="No product fixes",
                skipped=True,
                skip_reason="No fixable product feed gaps",
                metrics={"product_findings": findings_open},
            )
        return AgentResult(
            summary=f"Proposed {len(actions)} product feed fix{'es' if len(actions) != 1 else ''}",
            actions=actions,
            metric_label=f"{len(actions)} product fix proposed",
            metrics={"product_feed_fixes_proposed": len(actions)},
        )

    def _upsert_finding(
        self, ctx: AgentContext, product: Product, issue: feed_svc.FeedIssue
    ) -> ProductFeedFinding:
        existing = ctx.db.execute(
            select(ProductFeedFinding).where(
                ProductFeedFinding.tenant_id == ctx.tenant_id,
                ProductFeedFinding.product_id == product.id,
                ProductFeedFinding.kind == issue.kind,
            )
        ).scalar_one_or_none()
        if existing is not None:
            if existing.status in {"fixed", "ignored"}:
                return existing
            existing.summary = issue.summary
            existing.recommendation = issue.recommendation
            existing.detected_at = utcnow()
            return existing
        row = ProductFeedFinding(
            tenant_id=ctx.tenant_id,
            product_id=product.id,
            kind=issue.kind,
            summary=issue.summary,
            recommendation=issue.recommendation,
            evidence={},
            status="open",
            proposed_patch={},
            detected_at=utcnow(),
        )
        ctx.db.add(row)
        ctx.db.flush()
        return row


def _make_applier(finding_id: str):  # noqa: ANN202
    def apply(ctx: AgentContext) -> None:
        _apply_finding(ctx.db, ctx.org, finding_id, cipher=ctx.cipher, payload=None)

    return apply


def _apply_finding(
    db: Session,
    org: Organization,
    finding_id: str,
    *,
    cipher: OrgCipher,
    payload: dict | None,
) -> None:
    finding = db.get(ProductFeedFinding, finding_id)
    if finding is None or finding.tenant_id != org.id:
        raise LookupError("Finding no longer exists")
    product = db.get(Product, finding.product_id)
    if product is None or product.tenant_id != org.id:
        raise LookupError("Product no longer exists")

    data = feed_svc.validate_patch(
        {
            "finding_id": finding_id,
            "product_id": product.id,
            "kind": finding.kind,
            "patch": (payload or {}).get("patch") or (finding.proposed_patch or {}).get("patch") or {},
            "json_ld": (payload or {}).get("json_ld")
            if payload and "json_ld" in payload
            else (finding.proposed_patch or {}).get("json_ld"),
        }
    )
    patch = data["patch"]
    if "description" in patch and patch["description"]:
        product.description = patch["description"]
    if "url" in patch and patch["url"]:
        product.url = patch["url"]

    json_ld = data.get("json_ld")
    if isinstance(json_ld, dict) and product.url:
        page = db.execute(
            select(SeoPage).where(
                SeoPage.tenant_id == org.id,
                SeoPage.url == product.url,
            )
        ).scalar_one_or_none()
        if page is not None:
            schema = SchemaPatch(
                tenant_id=org.id,
                page_id=page.id,
                schema_type="Product",
                json_ld_encrypted=cipher.encrypt_json(json_ld, context=Ctx.SCHEMA_JSONLD),
                status="queued",
            )
            db.add(schema)

    finding.status = "fixed"
    finding.proposed_patch = {"patch": patch, "json_ld": json_ld}
    db.flush()


@register_applier(TARGET_KIND)
def apply_product_feed_fix(
    db: Session, org: Organization, item: ApprovalItem, payload: dict
) -> None:
    data = feed_svc.validate_patch(
        {**(payload or {}), "finding_id": (payload or {}).get("finding_id") or item.target_id}
    )
    cipher = OrgCipher.for_org(org)
    _apply_finding(db, org, data["finding_id"], cipher=cipher, payload=data)
    finding = db.get(ProductFeedFinding, data["finding_id"])
    if finding is not None:
        finding.approval_item_id = item.id


@register_rejecter(TARGET_KIND)
def reject_product_feed_fix(
    db: Session, org: Organization, item: ApprovalItem, payload: dict
) -> None:
    finding_id = str((payload or {}).get("finding_id") or item.target_id or "")
    finding = db.get(ProductFeedFinding, finding_id) if finding_id else None
    if finding is None or finding.tenant_id != org.id:
        return
    finding.status = "ignored"
    finding.approval_item_id = item.id
    db.flush()


AGENT = ProductFeedOptimizerAgent()
