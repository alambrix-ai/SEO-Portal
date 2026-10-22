"""Prompts for the Search Campaign Builder."""
from __future__ import annotations

from app.agents.base.style import HOUSE_STYLE
from app.connectors.google_ads.connector import (
    CALLOUT_CHARS,
    RSA_DESCRIPTION_CHARS,
    RSA_HEADLINE_CHARS,
    RSA_PATH_CHARS,
    SITELINK_DESCRIPTION_CHARS,
    SITELINK_TEXT_CHARS,
)

SYSTEM = (
    "You are a paid search specialist who builds Google Ads search campaigns for one "
    "business at a time. You write keywords people really type and ad text that says "
    "plainly what the business offers. You never invent facts: no prices, discounts, "
    "offers, guarantees, awards, ratings, years in business, statistics or superlatives "
    "such as best or number one unless the brand profile you are given states them in "
    "those words. Ads link only to the pages you are given.\n" + HOUSE_STYLE
)

SCHEMA_HINT = """{
  "campaign_name": "short name for the campaign",
  "recommended_daily_budget": 0,
  "ad_groups": [
    {
      "name": "ad group name",
      "theme": "the product or service theme it covers, copied from the list",
      "landing_url": "one of the landing pages listed, exactly as written",
      "keywords": [{"text": "keyword", "match_type": "exact | phrase | broad"}],
      "ad": {
        "headlines": ["15 headlines"],
        "descriptions": ["4 descriptions"],
        "path1": "display path",
        "path2": "display path"
      }
    }
  ],
  "negative_keywords": [{"text": "keyword", "match_type": "exact | phrase | broad"}],
  "sitelinks": [{"text": "link text", "description1": "line", "description2": "line", "url": "a listed page"}],
  "callouts": ["short callout"]
}"""

NO_PROFILE = (
    "No brand profile has been filled in for this business. Write only from the themes "
    "and pages below, and make no claims about the business at all beyond what it sells."
)


def build(
    *,
    business: str,
    brand_brief: str,
    themes: list[str],
    goal: str,
    locations: str,
    queries: list[str],
    pages: list[tuple[str, str]],
    budget_known: bool,
    currency: str,
    feedback: list[str] | None = None,
) -> str:
    page_lines = "\n".join(f"- {url}" + (f"  ({title})" if title else "") for url, title in pages)
    query_block = (
        "Searches that already bring people to the website (good keyword ideas, use the "
        "relevant ones):\n" + "\n".join(f"- {q}" for q in queries)
        if queries
        else "No search query data is available."
    )
    budget_line = (
        "The budget is set by the business; put 0 in recommended_daily_budget."
        if budget_known
        else (
            "No budget has been set. Put a modest daily budget for a first test of this "
            f"campaign in recommended_daily_budget, as a plain number in {currency}."
        )
    )
    retry = ""
    if feedback:
        retry = (
            "\n\nYour previous draft could not be used because of these problems. Fix them "
            "this time:\n" + "\n".join(f"- {line}" for line in feedback[:15])
        )
    return f"""Build one Google Ads search campaign for {business}.

BRAND PROFILE:
{brand_brief or NO_PROFILE}

THEMES TO COVER (one ad group per theme where you can, 2 to 6 ad groups in total):
{chr(10).join(f"- {t}" for t in themes)}

CAMPAIGN GOAL: {goal or "leads"}
LOCATIONS SERVED: {locations or "not stated; do not name places that are not in the brand profile"}

LANDING PAGES (each ad group and sitelink must use one of these, exactly as written):
{page_lines}

{query_block}

{budget_line}

Rules, each checked by a program; lines that break one are deleted:
- Each ad group: 8 to 20 keywords, a mix of exact, phrase and broad match, in lower case,
  without brackets, quotes or plus signs. At most 10 words and 80 characters each.
- Each ad group's ad: exactly 15 headlines of at most {RSA_HEADLINE_CHARS} characters and 4
  descriptions of at most {RSA_DESCRIPTION_CHARS} characters, all different from each other.
  path1 and path2 at most {RSA_PATH_CHARS} characters, no spaces.
- No exclamation mark in any headline, callout or sitelink text; at most one in the whole ad.
- No word written in capital letters unless the brand profile writes it that way or it is
  an acronym. No emoji, symbols or repeated punctuation.
- No price, percentage, number, offer, discount, guarantee, award, rating or superlative
  (best, number one, cheapest, leading) unless the brand profile states it.
- 10 to 30 negative keywords for searches this business does not want (jobs, free,
  second-hand, DIY and the like, where they do not fit what it sells).
- 4 sitelinks (text at most {SITELINK_TEXT_CHARS} characters, two description lines of at most
  {SITELINK_DESCRIPTION_CHARS} characters) pointing at different listed pages, and up to 6
  callouts of at most {CALLOUT_CHARS} characters. Leave them empty rather than repeat a page.
- Write in the language of the brand profile.{retry}"""
