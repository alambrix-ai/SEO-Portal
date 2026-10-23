"""Propose internal links between related synced pages through approvals."""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.base import pages as page_rules
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
from app.models.agent import AgentRecord
from app.models.approval import ApprovalItem, ApprovalStatus, ApprovalType
from app.models.internal_link import InternalLinkSuggestion
from app.models.seo import SeoPage
from app.models.workspace import Organization
from app.services import internal_linking as linking
from app.services.approvals import register_applier, register_rejecter
from app.services.encryption import Ctx, OrgCipher

TARGET_KIND = "internal_link_suggestion"
MAX_ACTIONS = 5


class InternalLinkingAgent(BaseAgent):
    spec = AgentSpec(
        slug="internal_linking",
        name="Internal Linking",
        category=AgentCategory.SEO_AEO,
        description=(
            "Finds related synced pages and proposes one internal link insertion "
            "per source page through the approval gate."
        ),
        default_interval=timedelta(days=1),
        default_schedule="Daily",
        required_capabilities=(Capability.WRITE_PAGE,),
        max_impact=Impact.HIGH,
        default_max_actions_per_day=15,
        scope_placeholder="/blog/* or leave blank for every writable synced page",
        requires_llm=False,
        connector_use=(
            ConnectorUse(
                Capability.WRITE_PAGE,
                UseMode.PAGE_SOURCE,
                question="Which websites may receive internal links?",
                multi=True,
                selected="links pages from {names}",
                everything="links pages on every website it can write to",
                note="Each link is written back to the website its source page came from.",
            ),
        ),
    )

    def run(self, ctx: AgentContext) -> AgentResult:
        pages = self._load_pages(ctx)
        if len(pages) < 2:
            return AgentResult.skip("Need at least two writable synced pages to suggest links")

        domain = (ctx.org.primary_domain or "").strip()
        payloads = [
            {
                "id": page.id,
                "url": page.url,
                "title": page.title,
                "body": ctx.cipher.decrypt(page.body_encrypted, context=Ctx.PAGE_BODY) or "",
                "target_keywords": list(page.target_keywords or []),
            }
            for page in pages
        ]
        candidates = linking.rank_candidates(payloads, domain=domain, limit=MAX_ACTIONS * 3)
        if not candidates:
            return AgentResult(
                summary="No strong internal-link opportunities above the score threshold",
                metric_label="No links needed",
                skipped=True,
                skip_reason="No candidates above the similarity threshold",
            )

        by_id = {page.id: page for page in pages}
        actions: list[AgentAction] = []
        for index, candidate in enumerate(candidates, start=1):
            if len(actions) >= min(MAX_ACTIONS, max(ctx.remaining_actions, 1)):
                break
            if self._pending(ctx, candidate.source_page_id, candidate.target_page_id):
                continue
            source = by_id.get(candidate.source_page_id)
            if source is None:
                continue
            body = next(p["body"] for p in payloads if p["id"] == source.id)
            try:
                patched = linking.insert_link(
                    body, href=candidate.target_url, anchor=candidate.anchor
                )
            except ValueError as exc:
                ctx.log_warning("Skipped link on %s: %s", source.url, exc)
                continue
            if patched == body:
                continue

            suggestion = InternalLinkSuggestion(
                tenant_id=ctx.tenant_id,
                source_page_id=candidate.source_page_id,
                target_page_id=candidate.target_page_id,
                source_url=candidate.source_url[:512],
                target_url=candidate.target_url[:512],
                anchor=candidate.anchor[:80],
                score=candidate.score,
                status="proposed",
                proposed_at=utcnow(),
            )
            ctx.db.add(suggestion)
            ctx.db.flush()

            ctx.progress(
                f"Proposing link on {source.url}",
                done=index - 1,
                total=len(candidates),
            )
            actions.append(
                AgentAction(
                    kind="insert_internal_link",
                    title=f'Link "{candidate.anchor}" → {candidate.target_url}',
                    impact=Impact.HIGH,
                    approval_type=ApprovalType.INTERNAL_LINK.value,
                    target_kind=TARGET_KIND,
                    target_id=suggestion.id,
                    payload={
                        "suggestion_id": suggestion.id,
                        "source_page_id": candidate.source_page_id,
                        "target_page_id": candidate.target_page_id,
                        "source_url": candidate.source_url,
                        "target_url": candidate.target_url,
                        "anchor": candidate.anchor,
                        "score": candidate.score,
                        "proposed_body": patched,
                    },
                    apply=_make_linker(suggestion.id),
                    audit=f"inserted internal link on {source.url}",
                    metrics={"internal_links": 1},
                )
            )

        ctx.db.flush()
        if not actions:
            return AgentResult.skip("Candidates existed, but none produced a safe insertion")
        return AgentResult(
            summary=f"Proposed {len(actions)} internal link{'s' if len(actions) != 1 else ''}",
            actions=actions,
            metric_label=f"{len(actions)} link proposed",
            metrics={"internal_links_proposed": len(actions)},
        )

    def _load_pages(self, ctx: AgentContext) -> list[SeoPage]:
        from app.agents.base import scope as scope_rules

        stmt = select(SeoPage).where(
            SeoPage.tenant_id == ctx.tenant_id,
            SeoPage.body_encrypted != "",
        )
        in_scope = scope_rules.sql_filter(ctx.scope or "", SeoPage.url)
        if in_scope is not None:
            stmt = stmt.where(in_scope)
        writable = page_rules.writable_rows(SeoPage.cms_slug)
        if writable is not None:
            stmt = stmt.where(writable)
        chosen = page_rules.source_filter(ctx, SeoPage.cms_slug, Capability.WRITE_PAGE)
        if chosen is not None:
            stmt = stmt.where(chosen)
        return list(ctx.db.execute(stmt.order_by(SeoPage.created_at.asc()).limit(200)).scalars())

    def _pending(self, ctx: AgentContext, source_id: str, target_id: str) -> bool:
        pending = ctx.db.execute(
            select(ApprovalItem.id)
            .join(
                InternalLinkSuggestion,
                InternalLinkSuggestion.id == ApprovalItem.target_id,
            )
            .where(
                ApprovalItem.tenant_id == ctx.tenant_id,
                ApprovalItem.status == ApprovalStatus.PENDING.value,
                ApprovalItem.target_kind == TARGET_KIND,
                InternalLinkSuggestion.source_page_id == source_id,
                InternalLinkSuggestion.target_page_id == target_id,
            )
            .limit(1)
        ).scalar_one_or_none()
        return pending is not None


def _make_linker(suggestion_id: str):  # noqa: ANN202
    def apply(ctx: AgentContext) -> None:
        _apply_suggestion(ctx.db, ctx.org, suggestion_id, cipher=ctx.cipher, ctx=ctx)

    return apply


def _apply_suggestion(
    db: Session,
    org: Organization,
    suggestion_id: str,
    *,
    cipher: OrgCipher,
    ctx: AgentContext | None = None,
    payload: dict | None = None,
) -> None:
    from app.orchestration.runner import build_context

    suggestion = db.get(InternalLinkSuggestion, suggestion_id)
    if suggestion is None or suggestion.tenant_id != org.id:
        raise LookupError("The link suggestion no longer exists")
    page = db.get(SeoPage, suggestion.source_page_id)
    if page is None:
        raise LookupError("The source page no longer exists")

    body = (payload or {}).get("proposed_body")
    if not body:
        current = cipher.decrypt(page.body_encrypted, context=Ctx.PAGE_BODY) or ""
        body = linking.insert_link(
            current,
            href=(payload or {}).get("target_url") or suggestion.target_url,
            anchor=(payload or {}).get("anchor") or suggestion.anchor,
        )

    if ctx is None:
        record = db.execute(
            select(AgentRecord).where(
                AgentRecord.tenant_id == org.id,
                AgentRecord.slug == "internal_linking",
            )
        ).scalar_one_or_none()
        if record is None:
            raise LookupError("Internal Linking is not installed in this workspace")
        ctx = build_context(db, record, org, trigger="approval")

    cms = ctx.optional_connector(page.cms_slug)
    if cms is None:
        from app.core.exceptions import ConnectorError

        raise ConnectorError(
            f"{ctx.connector_name(page.cms_slug)} is the system this page came "
            "from, and it is not connected, so this link cannot be published. "
            "Reconnect it and approve this again."
        )

    try:
        cms.write_page(page.cms_id, body=body, title=None)  # type: ignore[union-attr]
    except TypeError:
        cms.write_page(page.cms_id, body=body)  # type: ignore[union-attr]

    page.body_encrypted = cipher.encrypt(body, context=Ctx.PAGE_BODY)
    page.last_synced_at = utcnow()
    suggestion.status = "applied"
    suggestion.decided_at = utcnow()
    db.flush()


@register_applier(TARGET_KIND)
def apply_internal_link(
    db: Session, org: Organization, item: ApprovalItem, payload: dict
) -> None:
    suggestion_id = str((payload or {}).get("suggestion_id") or item.target_id or "")
    if not suggestion_id:
        raise ValueError("Missing suggestion id")
    href = str((payload or {}).get("target_url") or "")
    anchor = str((payload or {}).get("anchor") or "")
    if href:
        linking.validate_target_href(href)
    if anchor:
        linking.validate_anchor(anchor)
    cipher = OrgCipher.for_org(org)
    _apply_suggestion(db, org, suggestion_id, cipher=cipher, payload=payload or {})


@register_rejecter(TARGET_KIND)
def reject_internal_link(
    db: Session, org: Organization, item: ApprovalItem, payload: dict
) -> None:
    suggestion_id = str((payload or {}).get("suggestion_id") or item.target_id or "")
    suggestion = db.get(InternalLinkSuggestion, suggestion_id) if suggestion_id else None
    if suggestion is None or suggestion.tenant_id != org.id:
        return
    suggestion.status = "rejected"
    suggestion.decided_at = utcnow()
    db.flush()


AGENT = InternalLinkingAgent()
