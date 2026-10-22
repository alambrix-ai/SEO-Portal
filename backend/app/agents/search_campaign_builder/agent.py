"""Search Campaign Builder — plans one search campaign per run.

Inputs: the brand profile (what the business sells, where, how it talks,
what it claims), the pages collected from the website (the only landing pages
an ad may use), the searches that already bring people to the site when a
search-performance integration is connected, and this agent's settings
(monthly budget, goal, locations).

Each run picks the products and services that no earlier campaign from this
agent covers, asks the AI model for one campaign with 2 to 6 ad groups, and
checks every line strictly (see ``validate``). The campaign is stored in the
content library: once for Google Ads, and once more for Microsoft Advertising
when that integration is connected or the include_bing setting is on.
Approving the Google one creates it in Google Ads, paused; the Microsoft one
(and the Google one when Google Ads is not connected) is download-only.
"""
from __future__ import annotations

import math
from datetime import timedelta

from sqlalchemy import select

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
from app.agents.search_campaign_builder import prompts, validate
from app.connectors.base.connector import Capability
from app.core.config import settings
from app.core.money import money
from app.models.content import AssetKind, AssetStatus, MarketingAsset
from app.models.seo import SeoPage
from app.services import assets

SLUG = "search_campaign_builder"
#: Average days in a month: a monthly budget divided by this is the daily one.
DAYS_PER_MONTH = 30.4
#: Themes offered to the model per campaign (one ad group each, at most).
THEMES_PER_CAMPAIGN = validate.MAX_AD_GROUPS
#: Website pages offered as landing pages.
MAX_PAGES = 40
#: Search queries offered as keyword ideas.
MAX_QUERIES = 25
GOALS = {"leads": "Leads", "sales": "Sales", "calls": "Phone calls"}
_YES = {"yes", "y", "true", "1", "on"}


def daily_budget(monthly: float) -> float:
    """Monthly budget / 30.4, rounded *down* to the cent so a month never exceeds it."""
    if monthly <= 0:
        return 0.0
    return math.floor(monthly / DAYS_PER_MONTH * 100) / 100


def _number(raw: object) -> float | None:
    text = str(raw or "").replace(",", "").replace(settings.currency_symbol, "").strip()
    if not text:
        return None
    try:
        value = float(text)
    except ValueError:
        return None
    return value if value > 0 and math.isfinite(value) else None


def _key(theme: str) -> str:
    return " ".join(theme.lower().split())


def _split(text: str) -> list[str]:
    return [part.strip() for part in (text or "").split(",") if part.strip()]


class SearchCampaignBuilderAgent(BaseAgent):
    spec = AgentSpec(
        slug=SLUG,
        name="Search Campaign Builder",
        category=AgentCategory.ADS,
        description=(
            "Plans complete search ad campaigns, with keywords, ads and a daily budget, "
            "for the products and services you do not advertise yet."
        ),
        default_interval=timedelta(days=7),
        default_schedule="Weekly",
        max_impact=Impact.HIGH,
        default_max_actions_per_day=4,
        scope_placeholder="Haircuts, Hair colour — products or services to advertise",
        requires_llm=True,
        connector_use=(
            ConnectorUse(
                Capability.READ_SEARCH_PERFORMANCE,
                UseMode.ONE,
                question="Which search data should this agent take keyword ideas from?",
            ),
            ConnectorUse(
                Capability.CREATE_SEARCH_CAMPAIGN,
                UseMode.ALL,
                question="Which ad platforms should this agent create campaigns in?",
                group="ad platforms that can create search campaigns",
                note=(
                    "A campaign for a platform that is left out, or not connected, is "
                    "ready to download instead."
                ),
            ),
        ),
    )

    def validate_scope(self, scope: str) -> str | None:
        if len(_split(scope)) > 20:
            return "Enter at most 20 products or services, separated by commas."
        return None

    # ── Inputs ─────────────────────────────────────────────────────────────
    @staticmethod
    def _home(ctx: AgentContext) -> str:
        domain = (ctx.org.primary_domain or "").strip().removeprefix("https://").removeprefix("http://")
        domain = domain.strip("/")
        return f"https://{domain}/" if domain else ""

    def _pages(self, ctx: AgentContext, home: str) -> list[tuple[str, str]]:
        """``(absolute url, title)`` of collected website pages."""
        rows = ctx.db.execute(
            select(SeoPage)
            .where(SeoPage.tenant_id == ctx.tenant_id)
            .order_by(SeoPage.updated_at.desc())
            .limit(MAX_PAGES)
        ).scalars()
        out: list[tuple[str, str]] = []
        seen: set[str] = set()
        for page in rows:
            url = (page.url or "").strip()
            if url.startswith("/"):
                if not home:
                    continue
                url = home.rstrip("/") + url
            key = validate.normalise_url(url)
            if key and key not in seen:
                seen.add(key)
                out.append((url, (page.title or "").strip()))
        return out

    @staticmethod
    def _covered(ctx: AgentContext) -> set[str]:
        """Themes earlier campaigns from this agent cover.

        A campaign somebody rejected, or one the platform refused, does not
        count: its themes can be planned again.
        """
        covered: set[str] = set()
        rows = ctx.db.execute(
            select(MarketingAsset).where(
                MarketingAsset.tenant_id == ctx.tenant_id,
                MarketingAsset.agent_slug == SLUG,
                MarketingAsset.kind == AssetKind.SEARCH_CAMPAIGN,
            )
        ).scalars()
        for asset in rows:
            if asset.status in (AssetStatus.REJECTED, AssetStatus.FAILED):
                continue
            covered.update(_key(t) for t in assets.content_of(asset, ctx.cipher).get("themes") or [])
        return covered

    @staticmethod
    def _queries(ctx: AgentContext) -> list[str]:
        search = ctx.with_capability(Capability.READ_SEARCH_PERFORMANCE)
        if search is None:
            return []
        try:
            data = search.read_search_performance(days=28)  # type: ignore[union-attr]
        except Exception as exc:  # noqa: BLE001 - keyword ideas are optional
            ctx.log_warning("Search performance unavailable: %s", exc)
            return []
        return [str(q) for q in (data or {}).get("top_queries") or [] if str(q).strip()][:MAX_QUERIES]

    # ── Run ────────────────────────────────────────────────────────────────
    def run(self, ctx: AgentContext) -> AgentResult:
        profile = ctx.brand_profile
        brief = ctx.brand_brief
        home = self._home(ctx)
        pages = self._pages(ctx, home)
        if home and validate.normalise_url(home) not in {validate.normalise_url(u) for u, _ in pages}:
            pages.append((home, "Home page"))
        if not pages:
            return AgentResult.skip(
                "No landing pages yet. Set your website address on the workspace, or run "
                "On-Page SEO Sync to collect your pages."
            )

        themes = _split(ctx.scope) or list(profile.get("products_services") or [])
        if not themes:
            themes = [title for _, title in pages if title and title != "Home page"]
        if not themes:
            return AgentResult.skip(
                "No products or services to advertise. Fill in the brand profile, or list "
                "them in this agent's scope box."
            )
        covered = self._covered(ctx)
        uncovered = [t for t in dict.fromkeys(themes) if _key(t) not in covered]
        if not uncovered:
            return AgentResult(
                summary=(
                    f"All {len(themes)} products and services already have a search campaign "
                    "from this agent. Add more to the brand profile or the scope box for new ones."
                ),
                metric_label="Every theme has a campaign",
            )
        planned = uncovered[:THEMES_PER_CAMPAIGN]

        monthly = _number(ctx.setting("monthly_budget"))
        goal_raw = str(ctx.setting("campaign_goal") or "").strip()
        goal = GOALS.get(goal_raw.lower(), goal_raw or "Leads")
        locations = str(ctx.setting("locations") or "").strip() or ", ".join(profile.get("locations") or [])
        business = str(profile.get("business_name") or ctx.org.name)
        rules = validate.Rules.build(
            profile_text=brief,
            business_name=business,
            pages=[u for u, _ in pages],
            fallback_url=home or pages[0][0],
            locations=locations,
        )

        ctx.progress("Reading the searches that bring people to your site")
        queries = self._queries(ctx)

        def draft(feedback: list[str] | None = None) -> dict:
            return ctx.ask_json(
                prompts.build(
                    business=business,
                    brand_brief=brief,
                    themes=planned,
                    goal=goal,
                    locations=locations,
                    queries=queries,
                    pages=pages,
                    budget_known=monthly is not None,
                    currency=settings.currency_symbol,
                    feedback=feedback,
                ),
                system=prompts.SYSTEM,
                schema_hint=prompts.SCHEMA_HINT,
                max_tokens=6000,
            ) or {}

        ctx.progress("Writing the campaign", done=0, total=1)
        try:
            raw = draft()
        except Exception as exc:  # noqa: BLE001 - reported as the run's outcome
            ctx.log_warning("Campaign draft failed: %s", exc)
            return AgentResult(summary="The AI model did not return a campaign this run. It will try again next run.")
        cleaned, report, problems = validate.campaign(raw, rules)
        regenerated = False
        if not validate.usable(cleaned):
            regenerated = True
            ctx.progress("Writing the campaign again", done=0, total=1)
            try:
                second_raw = draft(problems + report.reasons)
            except Exception as exc:  # noqa: BLE001
                ctx.log_warning("Campaign redraft failed: %s", exc)
                second_raw = {}
            second, second_report, problems = validate.campaign(second_raw, rules)
            cleaned = validate.merge(cleaned, second)
            report.dropped_lines += second_report.dropped_lines
            report.dropped_groups += second_report.dropped_groups
            raw = second_raw or raw
        if not validate.usable(cleaned):
            return AgentResult(
                summary=(
                    "The drafted campaign did not pass the checks twice, so nothing was saved: "
                    + "; ".join(problems[:4])
                ),
                detail={"themes": len(planned), "regenerated": regenerated},
            )

        budget_note = ""
        if monthly is not None:
            daily = daily_budget(monthly)
            recommended = False
        else:
            daily = round(_number((raw or {}).get("recommended_daily_budget")) or 0, 2)
            recommended = True
            if daily <= 0:
                return AgentResult(
                    summary=(
                        "No monthly budget is set and the AI model did not suggest one, so the "
                        "campaign was not saved. Set a monthly budget in this agent's settings."
                    ),
                )
            budget_note = "Recommended by the AI model. Set a monthly budget in this agent's settings to control it."
        if daily <= 0:
            return AgentResult(summary="The monthly budget is too small for a daily budget.")

        # Themes the ad groups say they cover; all planned ones if none match,
        # so the same list is not offered again next week.
        matched = [t for t in planned if _key(t) in {_key(g["theme"]) for g in cleaned["ad_groups"]}]
        name = (cleaned["name"] or f"Search {planned[0]}")[:180]
        name = f"{name} {ctx.today.isoformat()}"
        content = {
            "name": name,
            "goal": goal,
            "daily_budget": daily,
            "monthly_budget": monthly,
            "budget_is_recommendation": recommended,
            "currency": settings.currency_symbol,
            "locations": _split(locations),
            "themes": matched or planned,
            "microsoft_time_zone": str(ctx.setting("microsoft_time_zone") or "").strip(),
            "bidding": "Maximize clicks",
            "status": "Paused",
            "ad_groups": cleaned["ad_groups"],
            "negative_keywords": cleaned["negative_keywords"],
            "sitelinks": cleaned["sitelinks"],
            "callouts": cleaned["callouts"],
        }

        google = ctx.connectors.get("google_ads")
        # Created in Google Ads only when it is one of this agent's platforms.
        google_can_create = (
            google is not None
            and google.supports(Capability.CREATE_SEARCH_CAMPAIGN)
            and ctx.uses_connector("google_ads", Capability.CREATE_SEARCH_CAMPAIGN)
        )
        include_bing = str(ctx.setting("include_bing") or "").strip().lower() in _YES
        channels = ["google_ads"]
        if "microsoft_ads" in ctx.connectors or ctx.has_connector("microsoft_ads") or include_bing:
            channels.append("microsoft_ads")

        keyword_count = sum(len(g["keywords"]) for g in cleaned["ad_groups"])
        actions: list[AgentAction] = []
        for channel in channels:
            export_only = channel != "google_ads" or not google_can_create
            platform = "Google Ads" if channel == "google_ads" else "Microsoft Advertising"
            asset = assets.create(
                ctx.db,
                tenant_id=ctx.tenant_id,
                cipher=ctx.cipher,
                agent_slug=SLUG,
                kind=AssetKind.SEARCH_CAMPAIGN,
                channel=channel,
                title=f"{name} ({platform})",
                content=content,
                run_id=ctx.run_id or "",
                detail={
                    "export_only": export_only,
                    "ad_groups": len(cleaned["ad_groups"]),
                    "keywords": keyword_count,
                    "negative_keywords": len(cleaned["negative_keywords"]),
                    "sitelinks": len(cleaned["sitelinks"]),
                    "callouts": len(cleaned["callouts"]),
                    "daily_budget": daily,
                    "budget_is_recommendation": recommended,
                    "regenerated": regenerated,
                    "lines_dropped": report.dropped_lines,
                },
            )
            if channel == "google_ads" and not export_only:
                outcome = "Approving creates it in Google Ads, paused: nothing runs until you switch it on there."
            elif channel == "google_ads":
                outcome = (
                    "Google Ads is not connected, so approving marks it ready to download as a "
                    "Google Ads Editor file."
                )
            else:
                outcome = (
                    "Approving marks it ready to download as a Microsoft Advertising bulk file "
                    "to upload under Bulk Operations."
                )
            actions.append(
                AgentAction(
                    kind="create_search_campaign",
                    title=f"{platform} search campaign: {name}",
                    impact=Impact.HIGH,
                    approval_type="Search Campaign",
                    target_kind=assets.TARGET_KIND,
                    target_id=asset.id,
                    payload={
                        "asset_id": asset.id,
                        "platform": platform,
                        "campaign": name,
                        "goal": goal,
                        "daily_budget": f"{money(daily, decimals=2)} a day",
                        "budget_note": budget_note,
                        "keyword_count": keyword_count,
                        "negative_keyword_count": len(cleaned["negative_keywords"]),
                        "ad_groups": [
                            {
                                "name": g["name"],
                                "landing_page": g["landing_url"],
                                "keywords": len(g["keywords"]),
                                "headlines": g["ad"]["headlines"][:3],
                            }
                            for g in cleaned["ad_groups"]
                        ],
                        "what_happens": outcome,
                        # For the workspace's daily limit on new campaigns:
                        # a download file creates nothing in an ad account.
                        "export_only": export_only,
                    },
                    apply=lambda c, a=asset: assets.release(c.db, c.org, a, c.cipher),
                    audit=f"planned {platform} search campaign {name}",
                    writes_via=() if export_only else (channel,),
                )
            )

        source = (
            "written from your brand profile"
            if brief
            else "no brand profile yet, so written from your pages only (fill it in for better ads)"
        )
        summary = (
            f"Planned {name!r}: {len(cleaned['ad_groups'])} ad groups, {keyword_count} keywords, "
            f"{money(daily, decimals=2)} a day"
            + (" (recommended; no monthly budget set)" if recommended else "")
            + f"; {source}"
        )
        if report.dropped_lines or report.dropped_groups:
            summary += f"; {report.dropped_lines} lines and {report.dropped_groups} ad groups failed the checks"
        if len(uncovered) > len(planned):
            summary += f"; {len(uncovered) - len(planned)} more products or services next run"
        return AgentResult(
            summary=summary,
            actions=actions,
            metric_label=f"{len(actions)} search campaign drafts",
            detail={
                "themes": len(planned),
                "ad_groups": len(cleaned["ad_groups"]),
                "keywords": keyword_count,
                "regenerated": regenerated,
                "channels": channels,
            },
        )


AGENT = SearchCampaignBuilderAgent()
