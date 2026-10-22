"""Prompts for planning and writing SEO articles."""
from __future__ import annotations

from app.agents.base.style import HOUSE_STYLE

SYSTEM = (
    "You are a senior SEO content strategist and writer for one business. You plan "
    "and write articles that answer what people search for, in the business's own "
    "voice. Every fact about the business (what it sells, prices, locations, years, "
    "awards, customers, guarantees) must come from the business profile or the page "
    "list you are given. Never invent statistics, studies, quotes, testimonials, "
    "prices, product names or claims. When a fact you would like is not given, write "
    "around it in general terms instead of making one up. Links may only point to "
    "the exact URLs you are given.\n" + HOUSE_STYLE
)

PLAN_SCHEMA = """{
  "topics": [
    {
      "target_keyword": "the main search phrase this article should rank for",
      "title": "working headline",
      "angle": "one sentence: what the article covers and for whom",
      "secondary_keywords": ["2 to 5 related phrases"]
    }
  ]
}"""

ARTICLE_SCHEMA = """{
  "title": "headline, at most 70 characters",
  "slug": "url-slug-in-lowercase-with-hyphens",
  "meta_title": "at most 60 characters, includes the target keyword",
  "meta_description": "70 to 155 characters, includes the target keyword",
  "target_keyword": "the target keyword",
  "secondary_keywords": ["related phrases used in the article"],
  "outline": ["each h2 heading, in order"],
  "body_html": "the article body as HTML",
  "faq_heading": "the FAQ section heading, in the article's language",
  "faq": [{"q": "a question people ask", "a": "a two to four sentence answer"}],
  "internal_links": [{"url": "one of the given URLs", "anchor": "the link text used"}]
}"""


def business_block(brief: str, *, name: str, industry: str, domain: str) -> str:
    """The brand profile, or the little that is known without one."""
    if brief:
        return f"BUSINESS PROFILE:\n{brief}"
    lines = [f"BUSINESS: {name}"]
    if industry:
        lines.append(f"INDUSTRY: {industry}")
    if domain:
        lines.append(f"WEBSITE: {domain}")
    lines.append(
        "(No detailed profile is available. Keep claims about the business general "
        "and do not guess what it offers beyond what is stated here.)"
    )
    return "BUSINESS PROFILE:\n" + "\n".join(lines)


def plan(
    *,
    business: str,
    count: int,
    language: str,
    scope_topics: list[str],
    opportunities: list[str],
    brand_keywords: list[str],
    existing_pages: list[tuple[str, str]],
    written: list[str],
) -> str:
    parts = [business, ""]
    parts.append(f"Plan {count} new blog articles for this business's website, written in {language}.")
    if scope_topics:
        parts.append("Only plan articles about these topics: " + "; ".join(scope_topics) + ".")
    if opportunities:
        parts.append(
            "Searches the site already appears for in Google or Bing that no existing page "
            "covers (prefer these, they are proven demand):\n- " + "\n- ".join(opportunities)
        )
    if brand_keywords:
        parts.append("Phrases customers search for, from the business profile:\n- " + "\n- ".join(brand_keywords))
    if existing_pages:
        parts.append(
            "Pages that already exist (do not plan an article that would compete with one of "
            "these for the same search):\n"
            + "\n".join(f"- {title} ({url})" for url, title in existing_pages)
        )
    if written:
        parts.append(
            "Articles already written or planned (do not repeat these topics):\n- " + "\n- ".join(written)
        )
    parts.append(
        "Rules: each topic targets one distinct search phrase a real customer of this "
        "business would type. No two topics may target the same intent. Prefer topics the "
        "business can answer with authority from what it actually offers. Do not plan news, "
        "reviews of competitors, or topics that need facts not given above."
    )
    return "\n\n".join(parts)


def article(
    *,
    business: str,
    language: str,
    min_words: int,
    topic: dict,
    links: list[tuple[str, str]],
    problems: list[str] | None = None,
) -> str:
    secondary = ", ".join(topic.get("secondary") or []) or "none given"
    parts = [
        business,
        "",
        f"Write a complete SEO article in {language}.",
        f"TARGET KEYWORD: {topic.get('keyword')}",
        f"SECONDARY KEYWORDS: {secondary}",
        f"WORKING TITLE: {topic.get('title') or topic.get('keyword')}",
    ]
    if topic.get("angle"):
        parts.append(f"ANGLE: {topic['angle']}")
    if links:
        parts.append(
            "PAGES ON THIS SITE YOU MAY LINK TO (use 2 to 5 where they genuinely help the "
            "reader, with natural anchor text, and use each URL exactly as written):\n"
            + "\n".join(f"- {url} : {title}" for url, title in links)
        )
    else:
        parts.append("There are no pages to link to. Do not include any links.")
    parts.append(
        "Requirements:\n"
        f"- body_html of at least {min_words} words of real substance.\n"
        "- Structure with <h2> sections and <h3> subsections where useful. Never use <h1>; "
        "the title is shown separately. Every heading must have content under it.\n"
        "- Use only these tags: h2, h3, p, ul, ol, li, strong, em, a, blockquote, table, "
        "thead, tbody, tr, th, td. No scripts, styles, classes, images or inline attributes.\n"
        "- Use the target keyword in the title, the first paragraph and at least one h2, "
        "naturally. Use secondary keywords where they fit.\n"
        "- Links only to the URLs listed above. No other links.\n"
        "- 3 to 5 FAQ entries answering real follow-up questions. Do not repeat them inside body_html.\n"
        "- meta_title at most 60 characters; meta_description between 70 and 155 characters.\n"
        "- End the body with a short, specific next step for the reader that fits the business."
    )
    if problems:
        parts.append(
            "Your previous draft was rejected for these reasons. Fix every one:\n- " + "\n- ".join(problems)
        )
    return "\n\n".join(parts)


def image(*, title: str, industry: str) -> str:
    setting = f" for a {industry} business" if industry else ""
    return (
        f"A clean, realistic editorial header photograph for a blog article titled "
        f"\"{title}\"{setting}. Natural light, uncluttered composition, wide landscape "
        "framing. No text, no letters, no logos, no watermarks."
    )
