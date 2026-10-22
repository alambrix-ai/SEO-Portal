"""SEO Article Writer agent package."""
from app.agents.base.plain import AgentGuide, Optional_, SettingField
from app.agents.seo_article_writer.agent import AGENT, SLUG, SeoArticleWriterAgent
from app.connectors.base.connector import Capability
from app.services.agent_results import asset_results

GUIDE = AgentGuide(
    summary=(
        "Plans a calendar of blog articles from the searches your site already appears for, "
        "your brand profile and your existing pages, then writes each article with a title, "
        "meta description, FAQ and links to your own pages."
    ),
    effect=(
        "Each run keeps about two weeks of topics on the calendar, skipping topics your pages "
        "or earlier articles already cover, and writes the ones due in the next seven days: up "
        "to your articles per week, at most 5 a run and no more than your daily limit has "
        "left. The writer is told to use only facts from your brand profile and pages, and "
        "links are kept only when they point to a page of your site that has been collected. A draft that "
        "fails the length and structure checks is rewritten once; a topic that fails on two "
        "runs is skipped. Approving an article saves it as a draft in WordPress, Shopify or "
        "Webflow, or as a pull request on GitHub or Bitbucket, on its calendar date (straight "
        "away if that date has passed). It is "
        "never published live: you publish it there yourself. Header images stay in your "
        "content library and are not uploaded to your CMS. Every article can also be "
        "downloaded as HTML or Markdown."
    ),
    optional=(
        Optional_(
            Capability.CREATE_PAGE,
            "Saves articles as drafts in your CMS; without it, download them.",
        ),
        Optional_(
            Capability.GENERATE_IMAGE,
            "Creates a header image for each article with OpenAI or Google Gemini. Without it, "
            "articles have no image.",
        ),
        Optional_(
            Capability.READ_SEARCH_PERFORMANCE,
            "Finds searches your site appears for that no page covers yet, and asks the planner "
            "to prefer them.",
        ),
    ),
    scope_label="Topics to write about (optional)",
    scope_help=(
        "Leave blank to let the agent choose topics. To steer it, enter up to 12 topics "
        "separated by commas, for example bridal makeup, hair colour aftercare."
    ),
    settings=(
        SettingField(
            "articles_per_week",
            "Articles per week",
            "How many articles go on the calendar each week, from 1 to 14. Leave blank for 2.",
            kind="integer",
            default="2",
            minimum=1,
            maximum=14,
        ),
        SettingField(
            "min_words",
            "Minimum words per article",
            "Articles shorter than this are rewritten. From 600 to 3000; leave blank for 1000.",
            kind="integer",
            default="1000",
            minimum=600,
            maximum=3000,
        ),
        SettingField(
            "language",
            "Language",
            "The language articles are written in. Leave blank to use your brand profile's "
            "language, or English if it has none.",
            max_length=40,
        ),
    ),
)


RESULTS = lambda db, tenant_id, cipher: asset_results(  # noqa: E731 - the package's results hook
    db,
    tenant_id,
    agent_slug=SLUG,
    title="Articles",
    description=(
        "Articles this agent has written, with where each one goes and its status. Open the "
        "content library to read, edit, approve or download them."
    ),
    empty="No articles yet. The first ones are written on the next run.",
)


__all__ = ["AGENT", "GUIDE", "RESULTS", "SeoArticleWriterAgent"]
