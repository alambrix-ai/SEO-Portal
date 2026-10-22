"""Search Campaign Builder agent package.

Importing it registers the ``search_campaign`` publisher and exporters
(see ``library``).
"""
from app.agents.base.plain import AgentGuide, Optional_, SettingField
from app.agents.search_campaign_builder import library  # noqa: F401 - registers publisher/exporters
from app.agents.search_campaign_builder.agent import AGENT, SLUG, SearchCampaignBuilderAgent
from app.connectors.base.connector import Capability
from app.services.agent_results import asset_results

GUIDE = AgentGuide(
    summary=(
        "Plans one search ad campaign a run for products and services you do not advertise "
        "yet: themed ad groups with keywords and a responsive search ad each, negative "
        "keywords, sitelinks, callouts and a daily budget, written from your brand profile "
        "and your website pages."
    ),
    effect=(
        "Each campaign waits in the Content library for approval. With Google Ads connected, "
        "approving creates it in your Google Ads account paused, with Maximize clicks bidding "
        "on Google Search only, so nothing runs or spends until you switch it on there. "
        "Location and language targeting are not set, so set them in Google Ads before you "
        "switch it on. Without Google Ads connected, approving marks it ready to download as "
        "a Google Ads Editor file. A Microsoft Advertising version is also written when "
        "Microsoft Ads is connected or Include Microsoft Advertising is yes; it is "
        "download-only, as a bulk upload file. The daily budget is your monthly budget "
        "divided by 30.4, rounded down; with no monthly budget the AI model suggests one and "
        "the approval says it is a recommendation. Ad lines with a price, a number, a "
        "discount, a guarantee or words such as “best” or “#1” are removed unless your brand "
        "profile says the same, and ads link only to pages collected from your website or to "
        "your home page."
    ),
    optional=(
        Optional_(
            Capability.CREATE_SEARCH_CAMPAIGN,
            "Creates approved campaigns in Google Ads, paused. Without it, campaigns are "
            "download-only.",
        ),
        Optional_(
            Capability.READ_SEARCH_PERFORMANCE,
            "Uses the searches that already bring people to your site as keyword ideas.",
        ),
    ),
    scope_label="Products or services to advertise (optional)",
    scope_help=(
        "Leave blank to use the products and services in your brand profile. Separate several "
        "with commas. Each run covers up to 6 that no earlier campaign from this agent covers."
    ),
    settings=(
        SettingField(
            "monthly_budget",
            "Monthly budget",
            "In your ad account's currency. The daily budget is this divided by 30.4, rounded "
            "down. Leave blank or 0 and the AI model suggests a daily budget, marked as a "
            "recommendation.",
            kind="number",
            minimum=0,
        ),
        SettingField(
            "campaign_goal",
            "Campaign goal",
            "leads, sales or calls. It shapes the ad text; bidding is Maximize clicks either way.",
            default="leads",
            max_length=40,
        ),
        SettingField(
            "locations",
            "Locations served",
            "Cities or areas, separated by commas. Used in the ad text and keywords; it does not "
            "set location targeting on the ad platform. Leave blank to use your brand profile's.",
            max_length=200,
        ),
        SettingField(
            "include_bing",
            "Include Microsoft Advertising",
            "yes to also write a Microsoft Advertising version when Microsoft Ads is not connected.",
            default="no",
            max_length=5,
        ),
        SettingField(
            "microsoft_time_zone",
            "Microsoft Advertising time zone",
            "As Microsoft Advertising names it, for example EasternTimeUSCanada. Written into the "
            "bulk file, which needs it for a new campaign; if blank, fill in the Time Zone column "
            "before uploading.",
            max_length=80,
        ),
    ),
)


RESULTS = lambda db, tenant_id, cipher: asset_results(  # noqa: E731
    db,
    tenant_id,
    agent_slug=SLUG,
    title="Search campaigns",
    description="Campaigns this agent planned, and whether each was created or is ready to download.",
    empty="No search campaigns yet. They appear here after the agent's first run.",
    channel_label="Platform",
)


__all__ = ["AGENT", "GUIDE", "RESULTS", "SearchCampaignBuilderAgent"]
