"""Prompts and the safety check for review replies."""
from __future__ import annotations

import re

SYSTEM = (
    "You write replies from a local business to its Google reviews. Replies are "
    "public. Be warm, specific and brief. Never invent facts about the business, "
    "never promise discounts, refunds, free services or compensation, and never "
    "include phone numbers, email addresses or links."
)

SCHEMA_HINT = '{"reply": "the reply text"}'

#: Google allows long replies; short ones read as personal.
MIN_CHARS = 15
MAX_CHARS = 600

_PROMISES = re.compile(
    r"\b(discount|refund|free of charge|complimentary|compensat|voucher|coupon|% off)\b", re.I
)
_CONTACT = re.compile(r"(https?://|www\.|@[a-z0-9-]+\.[a-z]|\+?\d[\d\s-]{7,}\d)", re.I)


def draft_reply(*, business: str, reviewer: str, stars: int, review: str) -> str:
    tone = (
        "Thank them sincerely and mention something specific they said."
        if stars >= 4
        else "Apologise sincerely for their experience, without arguing or making excuses, "
        "and invite them to contact the business directly so it can be put right. Do not "
        "give contact details."
        if stars and stars <= 3
        else "Thank them for taking the time to leave a review."
    )
    who = reviewer or "the customer"
    text = review.strip() or "(no written comment — rating only)"
    return (
        f"Business: {business}\n"
        f"Reviewer: {who}\n"
        f"Rating: {stars or 'not given'} out of 5\n"
        f"Review: {text}\n\n"
        f"Write the business's reply. {tone} Reply in the same language as the review. "
        "Keep it under 80 words. Sign off with the business name only."
    )


def check_reply(reply: str) -> str | None:
    """Return a reason to discard the draft, or ``None`` if it can be proposed."""
    cleaned = reply.strip()
    if len(cleaned) < MIN_CHARS:
        return "too short"
    if len(cleaned) > MAX_CHARS:
        return f"too long ({len(cleaned)} characters)"
    if _PROMISES.search(cleaned):
        return "promises a discount, refund or compensation"
    if _CONTACT.search(cleaned):
        return "contains a link, email address or phone number"
    return None
