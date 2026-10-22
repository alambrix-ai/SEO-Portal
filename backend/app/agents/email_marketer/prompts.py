"""Prompts for the Email Marketer. The model writes copy only; markup is built in render.py."""
from __future__ import annotations

from app.agents.email_marketer import render

SYSTEM = (
    "You write marketing emails for one business, in its own tone of voice. Use only facts "
    "given in the brand profile and the article list. Never invent offers, discounts, prices, "
    "deadlines, statistics, testimonials, awards or customer names. Write like a person, not an "
    "advertisement: no ALL CAPS words, at most one exclamation mark, and none of the phrases "
    "spam filters flag (free money, act now, click here, guaranteed, risk-free). Do not use "
    "placeholders such as [First name], merge tags, HTML or web addresses in the copy — the "
    "button carries the link. Reply with JSON only."
)

SCHEMA_HINT = f"""{{
  "subject": "at most {render.SUBJECT_MAX} characters",
  "preview_text": "at most {render.PREVIEW_MAX} characters, shown after the subject in the inbox",
  "headline": "at most {render.HEADLINE_MAX} characters",
  "paragraphs": ["2 to 5 short paragraphs of plain text, {render.WORDS_MIN}-{render.WORDS_MAX} words in total"],
  "cta_label": "button text, 2-{render.CTA_LABEL_MAX} characters, e.g. Book an appointment",
  "cta_url": "exactly one of the listed links",
  "segment_suggestion": "who to send this to, e.g. Subscribers who joined in the last 30 days",
  "image_prompt": "a description of a photo-style header image with no text in it",
  "image_alt": "a short description of that image for screen readers"
}}"""

#: The welcome series: day after sign-up, and what each email is for.
WELCOME_SERIES: tuple[tuple[int, str], ...] = (
    (0, "Welcome the new subscriber, thank them for joining, say briefly what the business does and what they will receive from these emails."),
    (2, "Introduce the main products or services and who each one is for."),
    (5, "Explain why customers choose the business — its value propositions and what makes it different."),
    (9, "Invite them to take the business's main call to action, with a reason to do it now that is true."),
)

PROGRAM_BRIEFS = {
    "newsletter": (
        "A newsletter that summarises the articles listed below — one short paragraph each, in "
        "your own words — and points readers to the most useful one with the button."
    ),
    "winback": (
        "A win-back email to subscribers who have not engaged for a while: remind them what the "
        "business offers and why people choose it, and invite them back. Do not guilt them."
    ),
    "promo": (
        "A promotional email for the business's main product or service. Mention an offer only if "
        "the brand profile states one; otherwise promote what it is and why it is worth it."
    ),
}


def write_email(
    *,
    brand_brief: str,
    brief: str,
    links: list[str],
    articles: list[dict] | None = None,
    sequence: str = "",
    previous_problems: list[str] | None = None,
) -> str:
    parts = [
        "[TASK:write_email]",
        "BRAND PROFILE:",
        brand_brief,
        "",
        f"THIS EMAIL: {brief}",
    ]
    if sequence:
        parts.append(f"SEQUENCE: {sequence}")
    if articles:
        parts.append("ARTICLES:")
        for article in articles:
            line = f"- {article['title']}"
            if article.get("summary"):
                line += f" — {article['summary']}"
            if article.get("url"):
                line += f" ({article['url']})"
            parts.append(line)
    parts.append("LINKS THE BUTTON MAY USE (choose exactly one, copied exactly):")
    parts += [f"- {link}" for link in links]
    parts.append("Write in the language of the brand profile.")
    if previous_problems:
        parts.append(
            "Your previous answer could not be used because "
            + "; ".join(previous_problems)
            + ". Write it again and fix every one of these."
        )
    return "\n".join(parts)


def image_prompt(*, described: str, business: str, industry: str) -> str:
    subject = described or f"An inviting photograph that represents {business}"
    context = f" for a {industry} business" if industry else ""
    return (
        f"{subject}. Email header image{context}, landscape, natural light, realistic photography. "
        "No text, letters, logos or watermarks in the image."
    )
