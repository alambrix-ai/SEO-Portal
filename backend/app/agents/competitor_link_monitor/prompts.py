"""Prompt for the competitor counter-pitch."""
from __future__ import annotations

from app.agents.base.style import HOUSE_STYLE

SYSTEM = (
    "You write outreach to a publisher that has just linked to one of your "
    "client's competitors. Your job is to offer something demonstrably better "
    "than what they published: more original data, a wider sample, a local "
    "angle they lack, or a format their readers can act on. You never mention "
    "the competitor by name, never criticise what they ran, and never mention "
    "links, SEO or rankings. Under 140 words."
)
SYSTEM = SYSTEM + HOUSE_STYLE

SCHEMA_HINT = """{
  "subject": "specific subject line",
  "body": "the email body, under 140 words, no signature block",
  "tone": "warm | professional | concise | enthusiastic",
  "sentiment_score": 0.0-1.0,
  "variant": "A | B | C"
}"""

def counter_pitch(
    *,
    publisher: str,
    competitor: str,
    competitor_anchor: str,
    brand: str,
    authority: int,
    preferred_angle: str = "",
) -> str:
    context = f"\nTHEIR ANCHOR TEXT: {competitor_anchor}" if competitor_anchor else ""
    angle = f"\nPREFERRED_ANGLE: {preferred_angle}" if preferred_angle else ""
    return f"""[TASK:pr_pitch]
Write a counter-pitch to a publisher that just covered a competitor.

PUBLISHER: {publisher}
DOMAIN_AUTHORITY: {authority}
BRAND: {brand}
COMPETITOR_COVERED: {competitor}{context}{angle}

They have shown they publish on this subject and will link out. Offer a
distinctly stronger contribution on the same beat, not the same story. Do not
reference the competitor's piece at all; write as though approaching them
cold with something good."""
