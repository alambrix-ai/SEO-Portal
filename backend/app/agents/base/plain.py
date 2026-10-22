"""Plain-language guide to each agent, for operators who are not technical.

Every sentence here describes what the agent's code actually does (see each
agent's ``agent.py``). When an agent's behaviour changes, change its entry
in the same commit — this text is shown to customers as fact.

Prerequisites are *not* written here: they are derived from each agent's
``AgentSpec`` in :mod:`app.services.agent_requirements`. Only the facts the
spec cannot express live here — data collected by another agent, optional
integrations, and what the scope box means.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from app.connectors.base.connector import Capability


@dataclass(frozen=True, slots=True)
class Optional_:
    capability: Capability
    note: str


@dataclass(frozen=True, slots=True)
class SettingField:
    """One agent-specific setting the agent reads with ``ctx.setting(key)``.

    Declared here so the setup screen can offer it and the API can validate
    it; before this, these keys were read by agents but no screen could set
    them, so every one silently used its default.
    """

    key: str
    label: str
    help: str = ""
    #: integer | number | text
    kind: str = "text"
    default: str = ""
    minimum: float | None = None
    maximum: float | None = None
    max_length: int = 200

    def clean(self, raw: str) -> str:
        """The stored form of ``raw``, or raise ValueError with a plain reason."""
        value = (raw or "").strip()
        if not value:
            return ""
        if self.kind in ("integer", "number"):
            try:
                number = int(value) if self.kind == "integer" else float(value)
            except ValueError:
                raise ValueError(f"{self.label} must be a number.") from None
            if self.minimum is not None and number < self.minimum:
                raise ValueError(f"{self.label} must be at least {self.minimum:g}.")
            if self.maximum is not None and number > self.maximum:
                raise ValueError(f"{self.label} must be at most {self.maximum:g}.")
            return str(number)
        if len(value) > self.max_length:
            raise ValueError(f"{self.label} must be {self.max_length} characters or fewer.")
        return value


@dataclass(frozen=True, slots=True)
class AgentGuide:
    summary: str
    effect: str
    makes_changes: bool = True
    #: Agent whose collected data this one works from (it creates no data itself).
    uses_data_from: str = ""
    optional: tuple[Optional_, ...] = field(default_factory=tuple)
    scope_label: str = "Limit to (optional)"
    scope_help: str = ""
    #: When the agent will not run without a value in the scope box, the
    #: agent setting it falls back to (the run reads scope, then this key).
    scope_required_setting: str = ""
    #: False for agents that have no use for the scope box at all.
    uses_scope: bool = True
    #: Extra settings this agent reads (see SettingField).
    settings: tuple[SettingField, ...] = field(default_factory=tuple)


GUIDES: dict[str, AgentGuide] = {
    # ── SEO & AEO ─────────────────────────────────────────────────────────
    "on_page_seo_sync": AgentGuide(
        summary=(
            "Reads the pages on your website, scores how well each one covers its "
            "topic, and writes an improved version of pages that score poorly."
        ),
        effect=(
            "Replaces the text of a page on your website with the improved version — from 6 "
            "up to 25 pages a run, depending on your daily limit. It also collects your pages "
            "for the other SEO agents. Pages it can only read through the Site Crawler are "
            "audited but never rewritten; connect your CMS or repository for rewrites."
        ),
        scope_label="Only these pages (optional)",
        scope_help=(
            "Leave blank for the whole site. To limit it, enter page paths that start "
            "with /, separated by commas — for example /blog/* or /products/*."
        ),
    ),
    "technical_seo_auditor": AgentGuide(
        summary=(
            "Checks your pages for common SEO problems — thin content, missing titles, "
            "headings or descriptions, broken internal links and pages nothing links "
            "to — and gives a recommended fix for each."
        ),
        effect=(
            "Never changes your website. Findings appear on the Technical SEO screen. A "
            "problem counts as fixed when it stops appearing; page speed is measured on a "
            "rotation across the site."
        ),
        makes_changes=False,
        uses_data_from="on_page_seo_sync",
        optional=(
            Optional_(
                Capability.READ_PAGE_SPEED,
                "Adds mobile page-speed checks. Without it, speed is not measured.",
            ),
        ),
        scope_label="Only these pages (optional)",
        scope_help=(
            "Leave blank for the whole site, or enter page paths that start with /, "
            "separated by commas — for example /blog/*."
        ),
        settings=(
            SettingField(
                "thin_words",
                "Thin content below (words)",
                "Pages with fewer words than this are flagged as thin content. "
                "Leave blank for the default.",
                kind="integer",
                default="300",
                minimum=50,
                maximum=5000,
            ),
        ),
    ),
    "aeo_qa_injector": AgentGuide(
        summary=(
            "Writes short question-and-answer sections from your own page content, so "
            "AI assistants such as ChatGPT can quote your site."
        ),
        effect=(
            "Adds or updates one “Questions and answers” section on a page, merged into the "
            "live page so recent edits are kept. The rest of the page is left as it is."
        ),
        uses_data_from="on_page_seo_sync",
        optional=(
            Optional_(
                Capability.CHECK_CITATIONS,
                "Checks whether AI assistants mention your site for those questions.",
            ),
        ),
        scope_label="Only these pages (optional)",
        scope_help=(
            "Leave blank for every collected page, or enter page paths that start "
            "with /, separated by commas — for example /pricing/*."
        ),
    ),
    "knowledge_graph_schema": AgentGuide(
        summary=(
            "Adds structured data (schema markup) to your pages so search engines can "
            "recognise products, services, FAQs and business locations."
        ),
        effect=(
            "Adds structured data to a page. Visitors do not see it; search engines do. It "
            "works through every page in turn, and keeps one approval per page and type."
        ),
        uses_data_from="on_page_seo_sync",
        scope_label="Only these schema types (optional)",
        scope_help=(
            "Leave blank to choose the right types for each page automatically. To "
            "force types, enter schema.org names separated by commas — for example "
            "Product, FAQPage, LocalBusiness."
        ),
    ),
    "referral_spam_guard": AgentGuide(
        summary=(
            "Spots fake and spam websites that send junk visits to your analytics, so "
            "your traffic reports stay accurate."
        ),
        effect=(
            "Records each spam site it finds, with the visits involved, so you can "
            "exclude them from your reports. Google Analytics has no API for blocking "
            "referrers, so the list is for you to add to GA4 yourself. Your own domain "
            "is never flagged."
        ),
        scope_label="Always block these sites (optional)",
        scope_help="Domains only, separated by commas — for example spam-site.xyz.",
    ),
    # ── Off-Page ──────────────────────────────────────────────────────────
    "backlink_node_discovery": AgentGuide(
        summary=(
            "Suggests websites in your field that could link to you, using your page "
            "topics and search queries."
        ),
        effect=(
            "Adds suggested websites to your Off-Page list. Nothing is sent to anyone. "
            "With DataForSEO connected, each suggestion is checked against its backlink "
            "index: sites it has never seen are dropped and the rest get a measured authority."
        ),
        optional=(
            Optional_(
                Capability.READ_BACKLINKS,
                "Checks each suggestion against a real backlink index and measures its authority.",
            ),
        ),
        scope_label="Topics to look for (optional)",
        scope_help=(
            "Up to 8 topics, separated by commas. Leave blank to use topics from your "
            "own pages and search queries."
        ),
        settings=(
            SettingField(
                "min_relevance",
                "Minimum relevance (0 to 1)",
                "Suggestions that fit your topics less than this are dropped. 0.6 is the default; "
                "raise it for fewer, closer matches.",
                kind="number",
                default="0.6",
                minimum=0,
                maximum=1,
            ),
        ),
    ),
    "digital_pr_outreach": AgentGuide(
        summary=(
            "Writes pitch emails to the websites on your Off-Page list, built around "
            "the story you give it."
        ),
        effect=(
            "Emails the pitch to the publisher when you have added their contact address on "
            "the Off-Page screen and an email (SMTP) integration is connected. Otherwise the "
            "draft goes to your team through your notification integration to send yourself. "
            "Each approval says which will happen."
        ),
        optional=(
            Optional_(
                Capability.SEND_EMAIL,
                "Emails each pitch to the publisher when you have added their contact address. "
                "Without it, every pitch goes to your team as a draft.",
            ),
        ),
        scope_label="Story angle (required)",
        scope_required_setting="angle",
        scope_help=(
            "The claim every pitch is built on, for example “Our 2026 survey of 5,000 "
            "drivers”. Up to 3, separated by commas — each pitch uses one, in turn. The "
            "agent will not pitch without one."
        ),
    ),
    "competitor_link_monitor": AgentGuide(
        summary=(
            "Finds websites that link to your competitors but not to you, using DataForSEO "
            "backlink data, and writes a counter-pitch to each one."
        ),
        effect=(
            "Emails the counter-pitch to the publisher when you have added their contact "
            "address and an email (SMTP) integration is connected; otherwise sends the draft "
            "to your team. Needs your website domain set on the workspace."
        ),
        optional=(
            Optional_(
                Capability.SEND_NOTIFICATION,
                "Sends draft counter-pitches to your team. Without it or an email "
                "integration, an approved counter-pitch cannot be delivered.",
            ),
            Optional_(
                Capability.SEND_EMAIL,
                "Emails each counter-pitch to the publisher when you have added their "
                "contact address.",
            ),
        ),
        scope_label="Competitors (required)",
        scope_required_setting="competitors",
        scope_help=(
            "Competitor website domains, separated by commas — up to 10, for example "
            "rival.com, other.co.uk. Names cannot be looked up; use domains."
        ),
        settings=(
            SettingField(
                "counter_angle",
                "Counter-pitch angle (optional)",
                "What makes your content worth linking to instead, for example "
                "“independent test data from 40 salons”. Leave blank to let it write from your pages.",
                max_length=200,
            ),
        ),
    ),
    # ── Ads ───────────────────────────────────────────────────────────────
    "dynamic_creative_optimizer": AgentGuide(
        summary=(
            "Writes ad headlines and text for your connected ad platforms, using only "
            "facts found on your product pages."
        ),
        effect=(
            "Uploads new ad text to your ad platforms — created paused or as a draft for you "
            "to switch on (Google, Meta, LinkedIn and TikTok), "
            "linking to the product page it was written from. On Google Ads the variants "
            "for a page are combined into one responsive search ad. Pages are written for "
            "again when they change, for example after a price change. It does not change budgets."
        ),
        uses_data_from="on_page_seo_sync",
        scope_label="Build ads from these pages (optional)",
        scope_help=(
            "Leave blank to pick product pages automatically, or enter page paths that "
            "start with /, separated by commas — for example /products/*."
        ),
    ),
    "predictive_budget_engine": AgentGuide(
        summary=(
            "Compares cost per conversion across your ad platforms and moves budget "
            "toward the ones that perform best."
        ),
        effect=(
            "Changes the daily budget on your ad platforms. A platform’s share moves by at "
            "most 8 points at a time and at most once a day (sooner only if its cost per "
            "conversion moves 10% or more), every platform with spend keeps at least 5%, and "
            "nothing moves for less than a 2% expected gain. Only platforms with at least 5 "
            "conversions are adjusted. Daily budgets come from the monthly budget on each ad "
            "integration — enter it there; if it is blank, 10,000 is assumed."
        ),
        optional=(
            Optional_(
                Capability.WRITE_AD_BUDGET,
                "Needed to apply the new budgets on the platform. Without it, the new "
                "split is only recorded here.",
            ),
        ),
        scope_label="Never change these platforms (optional)",
        scope_help=(
            "Enter any of google, meta, linkedin, tiktok, microsoft, dsp — separated by commas."
        ),
    ),
    "first_party_audience_modeler": AgentGuide(
        summary=(
            "Groups your customers by how they engage with you, using your CRM data, "
            "and creates matching audiences on your ad platforms."
        ),
        effect=(
            "Creates each group once as an audience on platforms that can build one without "
            "personal data — Meta (from your pixel's standard events, so the pixel must be "
            "installed) and DSP exchanges. Google, LinkedIn and TikTok audiences need customer "
            "contact lists, which this agent never sends. Needs at least 100 customer profiles. "
            "Retired groups are switched off here only."
        ),
        optional=(
            Optional_(
                Capability.PUSH_AUDIENCE,
                "Needed to send audiences to an ad platform. Without it, groups are only "
                "built here.",
            ),
        ),
        scope_label="Only keep these groups (optional)",
        scope_help=(
            "Leave blank to keep every group. Names must match the group names the "
            "agent creates, for example Cart abandoners."
        ),
    ),
    "click_fraud_controller": AgentGuide(
        summary="Checks the clicks on your ads and blocks sources that look like bots.",
        effect=(
            "Blocks suspicious sources on the ad platform the traffic came from — currently "
            "Google Ads and DSP exchanges support blocking — and records the reason. On other "
            "platforms the source is recorded so you can exclude it there. Saved spend is an "
            "estimate unless the platform reports its cost per click."
        ),
        optional=(
            Optional_(
                Capability.BLOCK_PLACEMENT,
                "Needed to block on the ad platform. Without it, blocks are only "
                "recorded here.",
            ),
        ),
        scope_label="Always block these sources (optional)",
        scope_help="Source names exactly as your ad platform reports them, separated by commas.",
    ),
    # ── Local business ────────────────────────────────────────────────────
    "local_search_tracker": AgentGuide(
        summary=(
            "Checks where your business appears when people search Google Maps for "
            "things like “salon near me”, and shows who is listed above you."
        ),
        effect=(
            "Never changes anything. Each run records your position for each search, "
            "with the rating and review count of the businesses listed."
        ),
        makes_changes=False,
        scope_label="Searches to track (optional)",
        scope_help=(
            "Up to 5, separated by commas — for example: salon near me, unisex salon. "
            "Leave blank to use your Google Maps category, such as “Hair salon near me” "
            "and “Hair salon”."
        ),
    ),
    "review_responder": AgentGuide(
        summary="Writes a reply to each Google review you have not answered yet, in your business's name.",
        effect=(
            "Posts public replies to your Google reviews, as many per run as your daily "
            "limit allows (up to 20). Drafts that promise discounts or refunds, or contain "
            "links, emails or phone numbers, are thrown away and tried again a week later. "
            "A review is only ever replied to once."
        ),
        uses_scope=False,
    ),
}


def guide_for(slug: str) -> AgentGuide | None:
    """The guide for an agent: listed here, or declared as ``GUIDE`` in its package.

    New agents declare their own, next to the code it describes, so the text
    and the behaviour are changed together.
    """
    if slug in GUIDES:
        return GUIDES[slug]
    import importlib

    try:
        module = importlib.import_module(f"app.agents.{slug}")
    except ImportError:
        return None
    guide = getattr(module, "GUIDE", None)
    return guide if isinstance(guide, AgentGuide) else None
