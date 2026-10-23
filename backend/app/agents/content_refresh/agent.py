"""Content Refresh — propose rewrites for pages losing clicks or rankings.

Reads per-page Search Console windows, matches them to synced ``SeoPage``
rows, asks the model for a refresh, and proposes high-impact publish actions
through the same approval / CMS write path as On-Page SEO Sync.
"""
from __future__ import annotations

from datetime import timedelta
from urllib.parse import urlsplit

from sqlalchemy import select

from app.agents.base import pages as page_rules
from app.agents.base import scope as scope_rules
from app.agents.base.context import AgentContext
from app.agents.base.contracts import (
    AgentAction,
    AgentCategory,
    AgentResult,
    AgentSpec,
    BaseAgent,
    Impact,
)
from app.agents.base.contracts import ConnectorUse, UseMode
from app.agents.base.style import humanise
from app.agents.content_refresh import prompts
from app.agents.on_page_seo_sync.agent import _make_publisher
from app.connectors.base.connector import Capability
from app.db.base import utcnow
from app.models.approval import ApprovalItem, ApprovalStatus, ApprovalType
from app.models.content_refresh import ContentRefreshCandidate
from app.models.seo import PageStatus, SeoPage
from app.services import content_refresh as refresh_svc
from app.services.encryption import Ctx

WINDOW_DAYS = 28
MAX_CANDIDATES = 8
PAGES_PER_RUN = 3


class ContentRefreshAgent(BaseAgent):
    spec = AgentSpec(
        slug="content_refresh",
        name="Content Refresh",
        category=AgentCategory.SEO_AEO,
        description=(
            "Finds pages losing clicks or rankings in Search Console and proposes "
            "refreshed copy through the approval gate."
        ),
        default_interval=timedelta(days=1),
        default_schedule="Daily",
        required_capabilities=(
            Capability.READ_SEARCH_PERFORMANCE,
            Capability.WRITE_PAGE,
        ),
        max_impact=Impact.HIGH,
        default_max_actions_per_day=10,
        scope_placeholder="/blog/* or leave blank for every synced page",
        requires_llm=True,
        connector_use=(
            ConnectorUse(
                Capability.READ_SEARCH_PERFORMANCE,
                UseMode.ONE,
                question="Which search performance source should detect declining pages?",
            ),
            ConnectorUse(
                Capability.WRITE_PAGE,
                UseMode.PAGE_SOURCE,
                question="Which websites may receive refreshed copy?",
                multi=True,
                selected="refreshes collected pages from {names}",
                everything="refreshes declining pages on every website it can write to",
                note="Each refresh is saved to the website its page was collected from.",
            ),
        ),
    )

    def validate_scope(self, scope: str) -> str | None:
        return scope_rules.paths(scope)

    def run(self, ctx: AgentContext) -> AgentResult:
        search = ctx.with_capability(Capability.READ_SEARCH_PERFORMANCE)
        if search is None:
            return AgentResult.skip("Waiting on Search Console (or another search source)")

        reader = getattr(search, "read_page_performance_windows", None)
        if not callable(reader):
            return AgentResult.skip(
                f"{search.name} cannot report per-page windows yet — connect Google Search Console."
            )

        ctx.progress("Reading Search Console page windows")
        try:
            rows = reader(window_days=WINDOW_DAYS, row_limit=100)
        except Exception as exc:  # noqa: BLE001
            return AgentResult.skip(f"Could not read page performance: {exc}")

        signals = refresh_svc.decline_signals(list(rows or []))[:MAX_CANDIDATES]
        if not signals:
            return AgentResult(
                summary="No material click or ranking drops in the compared windows",
                metric_label="No refresh needed",
                skipped=True,
                skip_reason="No declining pages above the threshold",
            )

        pages_by_key = self._synced_pages(ctx)
        actions: list[AgentAction] = []
        detected = 0
        refreshed = 0

        for index, signal in enumerate(signals, start=1):
            if len(actions) >= min(PAGES_PER_RUN, max(ctx.remaining_actions, 1)):
                break
            page = pages_by_key.get(refresh_svc.path_key(signal.url))
            if page is None:
                # GSC sometimes returns path-only keys for domain properties.
                page = pages_by_key.get(
                    (urlsplit(signal.url).path or "/").rstrip("/") or "/"
                )
            if page is None:
                continue
            if self._pending_rewrite(ctx, page.id):
                continue

            detected += 1
            candidate = ContentRefreshCandidate(
                tenant_id=ctx.tenant_id,
                page_id=page.id,
                url=page.url[:512],
                reason_codes=list(signal.reason_codes),
                prior_clicks=signal.prior_clicks,
                recent_clicks=signal.recent_clicks,
                prior_position=signal.prior_position,
                recent_position=signal.recent_position,
                status="open",
                detected_at=utcnow(),
            )
            ctx.db.add(candidate)
            ctx.db.flush()

            body = ctx.cipher.decrypt(page.body_encrypted, context=Ctx.PAGE_BODY)
            if not body:
                candidate.status = "skipped"
                continue

            ctx.progress(
                f"Refreshing {page.url} ({index} of {len(signals)})",
                done=index - 1,
                total=len(signals),
            )
            try:
                data = ctx.ask_json(
                    prompts.refresh_page(
                        title=page.title,
                        url=page.url,
                        body=body,
                        reason_codes=list(signal.reason_codes),
                        prior_clicks=signal.prior_clicks,
                        recent_clicks=signal.recent_clicks,
                        prior_position=signal.prior_position,
                        recent_position=signal.recent_position,
                        keywords=list(page.target_keywords or []),
                    ),
                    system=prompts.SYSTEM,
                    schema_hint=prompts.SCHEMA_HINT,
                )
            except Exception as exc:  # noqa: BLE001
                ctx.log_warning("Refresh draft failed for %s: %s", page.url, exc)
                candidate.status = "skipped"
                continue

            proposed = humanise(data.get("proposed_body") or "")
            rationale = humanise(data.get("rationale") or "")
            if not proposed or proposed.strip() == body.strip():
                candidate.status = "skipped"
                continue

            page.proposed_body_encrypted = ctx.cipher.encrypt(
                proposed, context=Ctx.PAGE_PROPOSED
            )
            page.rewrite_rationale_encrypted = ctx.cipher.encrypt(
                rationale, context=Ctx.PAGE_RATIONALE
            )
            page.status = PageStatus.QUEUED.value
            candidate.rationale_encrypted = page.rewrite_rationale_encrypted
            candidate.status = "proposed"

            actions.append(
                AgentAction(
                    kind="publish_refresh",
                    title=f'Refresh: "{page.title}" (losing traffic)',
                    impact=Impact.HIGH,
                    approval_type=ApprovalType.CONTENT_REWRITE.value,
                    target_kind="seo_page",
                    target_id=page.id,
                    payload={
                        "page_id": page.id,
                        "url": page.url,
                        "title": page.title,
                        "reason_codes": list(signal.reason_codes),
                        "proposed_body": proposed,
                        "refresh": True,
                    },
                    apply=_make_publisher(page.id),
                    audit=f"published content refresh on {page.url}",
                    metrics={"pages_refreshed": 1},
                )
            )
            refreshed += 1

        ctx.db.flush()
        if not actions and detected == 0:
            return AgentResult.skip(
                "Declining URLs were found, but none match synced pages this agent can write."
            )
        return AgentResult(
            summary=(
                f"Detected {detected} declining page{'s' if detected != 1 else ''}; "
                f"proposed {refreshed} refresh{'es' if refreshed != 1 else ''}"
            ),
            actions=actions,
            metric_label=f"{refreshed} refresh proposed" if refreshed else "No refresh proposed",
            metrics={"refresh_candidates": detected, "refresh_proposed": refreshed},
            detail={"detected": detected, "proposed": refreshed},
        )

    def _synced_pages(self, ctx: AgentContext) -> dict[str, SeoPage]:
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
        pages_list = list(ctx.db.execute(stmt.limit(2000)).scalars())
        by_key: dict[str, SeoPage] = {}
        for page in pages_list:
            by_key[refresh_svc.path_key(page.url)] = page
            path = urlsplit(page.url).path or "/"
            by_key.setdefault(path.rstrip("/") or "/", page)
        return by_key

    def _pending_rewrite(self, ctx: AgentContext, page_id: str) -> bool:
        pending = ctx.db.execute(
            select(ApprovalItem.id).where(
                ApprovalItem.tenant_id == ctx.tenant_id,
                ApprovalItem.status == ApprovalStatus.PENDING.value,
                ApprovalItem.target_kind == "seo_page",
                ApprovalItem.target_id == page_id,
            ).limit(1)
        ).scalar_one_or_none()
        return pending is not None


AGENT = ContentRefreshAgent()
