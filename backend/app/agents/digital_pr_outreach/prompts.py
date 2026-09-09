"""Prompts, quality checks and rendering for digital PR outreach."""
from __future__ import annotations

import re

from app.agents.base.style import HOUSE_STYLE

SYSTEM = (
    "You write short, specific outreach emails to editors and webmasters. You "
    "write like one professional to another: you say what you are offering, why "
    "it suits their readers, and nothing else. You never flatter, never use "
    "mail-merge filler, never mention SEO, rankings, backlinks or link "
    "exchanges, and never claim to have read something you were not shown. "
    "You keep it under 140 words."
)
SYSTEM = SYSTEM + HOUSE_STYLE

SCHEMA_HINT = """{
  "subject": "specific subject line, no clickbait",
  "body": "the email body, under 140 words, no signature block",
  "tone": "warm | professional | concise | enthusiastic",
  "sentiment_score": 0.0-1.0,
  "variant": "A | B | C"
}"""

# Phrases that mark a pitch as templated or as link-buying. Any of these and
# the draft is discarded rather than sent under the brand's name.
_BANNED = (
    "dear webmaster", "dear sir/madam", "to whom it may concern",
    "i came across your website", "i stumbled upon", "link exchange",
    "do follow", "dofollow", "backlink", "link building", "guest post opportunity",
    "improve your seo", "domain authority", "paid post", "sponsored link",
    "as per my previous email", "kindly do the needful", "unsubscribe",
)
_MAX_WORDS = 180
_MIN_WORDS = 35

def write_pitch(
    *,
    domain: str,
    placement_type: str,
    brand: str,
    angle: str,
    authority: int,
    relevance: float,
) -> str:
    return f"""[TASK:pr_pitch]
Write an outreach email.

DOMAIN: {domain}
PLACEMENT_TYPE: {placement_type}
BRAND: {brand}
ANGLE: {angle}
TOPICAL_FIT: {relevance:.2f}
DOMAIN_AUTHORITY: {authority}

Pitch the angle above as something worth their readers' time. Be concrete
about what you are offering and what they would have to do with it. Choose the
tone that suits a publication of this kind and report it, along with a
sentiment score for how warm the wording is."""

def check_pitch(*, subject: str, body: str, brand: str) -> str | None:
    """Return a reason to discard the draft, or ``None`` if it is sendable."""
    haystack = f"{subject}\n{body}".lower()

    for phrase in _BANNED:
        if phrase in haystack:
            return f"contains templated or link-buying phrasing: {phrase!r}"

    words = len(body.split())
    if words > _MAX_WORDS:
        return f"too long ({words} words)"
    if words < _MIN_WORDS:
        return f"too short to be a real pitch ({words} words)"

    if len(subject) > 90:
        return "subject line is too long"
    if subject.isupper() or subject.count("!") > 1:
        return "subject line reads as clickbait"

    # A pitch that never names the sender is indistinguishable from spam.
    if brand and brand.split()[0].lower() not in haystack:
        return "does not identify the sender"

    # Placeholders left unfilled by a template.
    if re.search(r"\[[a-z_ ]+\]|\{\{.*?\}\}|<insert", haystack):
        return "contains an unfilled placeholder"

    return None

def render_email(*, body: str, recipient: str, brand: str, domain: str = "") -> str:
    """Final message text, with the signature the model was told to omit.

    With no recipient the pitch cannot reach the publication, so it goes to
    the team's own channel headed by what it is and what it still needs. It
    used to arrive as a message beginning "Hello," with no addressee — which
    reads as something that was sent, and it was not.

    Addresses are no longer guessed by the model: it produced plausible ones,
    and this function is what would have sent to them.
    """
    if not recipient:
        target = f" for {domain}" if domain else ""
        return (
            f"DRAFT, not sent. This pitch has no recipient address{target}. "
            "Find the right editor and send it yourself.\n\n"
            "----\n\n"
            f"Hello,\n\n{body.strip()}\n\nBest regards,\nThe {brand} team"
        )

    greeting = f"Hello {recipient.split('@')[0].title()}"
    return f"{greeting},\n\n{body.strip()}\n\nBest regards,\nThe {brand} team"
