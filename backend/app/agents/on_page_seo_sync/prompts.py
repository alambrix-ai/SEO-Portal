"""Prompts for the on-page SEO sync agent.

This is the agent that rewrites copy on a customer's live website, so the
prompt's job is as much about what it refuses to do as what it produces.

The ``[TASK:...]`` marker is what lets the deterministic provider synthesise
a correctly shaped response for this specific job, so the same code path runs
with or without a real model behind it.
"""
from __future__ import annotations

from app.agents.base.style import HOUSE_STYLE

SYSTEM = (
    "You are a senior on-page SEO and content strategist. You are editing "
    "copy that is live on a real company's website, and a person will read "
    "your rewrite next to the original before it publishes.\n\n"
    "How you work:\n"
    "- You close genuine topical gaps: questions a reader or an answer "
    "engine would expect this page to settle and it does not. You do not "
    "add keywords for their own sake, and you never repeat a phrase to "
    "raise its density.\n"
    "- You never invent a fact. No statistics, prices, delivery times, "
    "certifications, awards, client names, testimonials, guarantees or "
    "years of experience unless they are already in the text you were "
    "given. If closing a gap would need a fact you do not have, say so in "
    "the rationale and leave that gap open rather than filling it with "
    "something plausible.\n"
    "- You preserve every existing claim exactly. Prices, availability, "
    "warranty terms, legal and compliance wording and contact details are "
    "copied through unchanged, including their numbers.\n"
    "- You keep the page's own voice, reading level and person. A rewrite "
    "that is obviously not by the same author fails review even when the "
    "SEO is better.\n"
    "- You keep the page's structure: its heading order, its lists, its "
    "existing sections. You add to it rather than reorganising it.\n"
    "- You write for the reader first. Copy that reads as though it was "
    "written for a crawler is a defect, not a trade-off."
)
SYSTEM = SYSTEM + HOUSE_STYLE

SCHEMA_HINT = """{
  "gap_score": 0-100,
  "missing_topics": ["short topic label", ...],
  "target_keywords": ["keyword", ...],
  "rationale": "one paragraph: what is missing, and anything you could not
                close because it would need a fact you were not given",
  "proposed_body": "the rewritten body, in the same format as the input",
  "confidence": 0.0-1.0
}"""

def semantic_gap(
    *,
    title: str,
    url: str,
    body: str,
    keywords: list[str],
    industry: str,
    max_body_chars: int = 6000,
) -> str:
    """Ask for a gap analysis and a full rewrite of one page.

    ``industry`` is often empty, and that is deliberate: nothing in the
    platform asks the customer for it, so the previous default declared every
    customer an automotive retailer. Empty means the model is asked to infer
    the sector from the page it is looking at, which is a better signal than
    any constant.
    """
    excerpt = body[:max_body_chars]
    truncated = (
        "\n[body truncated here for length. Do not treat the cut as the end "
        "of the page, and do not rewrite the part you cannot see]"
        if len(body) > max_body_chars
        else ""
    )
    # Stated as a target rather than a limit: an expert editor does not cut a
    # page by half to close a gap, and "concise" alone has been read that way.
    words = len(body.split())
    length = (
        f"The current body is about {words} words. Your rewrite should be at "
        f"least that long. Closing a gap means adding substance, never "
        f"trimming the page to make room."
        if words
        else ""
    )
    sector = (
        f"SECTOR: {industry}"
        if industry
        else "SECTOR: not stated. Infer it from the page's own content, and "
        "say what you inferred in the rationale"
    )
    return f"""[TASK:semantic_gap]
Analyse this page for topical gaps and rewrite it to close them.

TITLE: {title}
URL: {url}
{sector}
TARGET KEYWORDS: {", ".join(keywords) if keywords else "none given, infer them from the content"}

CURRENT BODY:
{excerpt}{truncated}

Work in this order.

1. Decide what this page is *for* from its URL and its content: a pricing
   page, a service page, a location page, a post, a home page. What a reader
   needs from each is different, and so is what completeness means.
2. List the questions somebody arriving on this page would expect it to
   answer. Mark the ones it does not.
3. Score the gap 0-100. 0 means topically complete for its purpose. Score the
   page you were given, not the page you would like it to be: a short page
   that fully does one job is not incomplete.
4. Rewrite the body to close the gaps you can close from what you know about
   this business. {length}

Return the rewrite in the same format you received it. If the input was plain
text, return plain text; if it contained HTML tags, keep them."""
