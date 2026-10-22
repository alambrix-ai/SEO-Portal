"""Prompts for the Social Media Manager."""
from __future__ import annotations

import re

from app.agents.social_media_manager.platforms import NETWORKS

SYSTEM = (
    "You are the social media manager for a business. You write posts that sound like "
    "the business and use only facts from its brand profile and the material you are "
    "given. Never invent prices, offers, discounts, statistics, awards, testimonials, "
    "customer names or quotes. Do not promise anything the profile does not state. "
    "Write in the business's language and tone of voice."
)

SCHEMA_HINT = """{
  "posts": [
    {
      "index": 1,
      "topic": "3 to 6 words naming what the post is about",
      "text": "the post text, without the hashtags",
      "hashtags": ["WordsWithoutTheHashSign"],
      "link": "one of the allowed links, or empty",
      "title": "Pinterest only: the Pin title",
      "image_prompt": "one or two sentences describing a photograph-style image for this post"
    }
  ]
}"""

STYLE = {
    "facebook": (
        "Conversational, about 40 to 80 words, with one clear call to action. The link, if "
        "any, is attached separately, so do not write it in the text."
    ),
    "instagram": (
        "Warm and visual, with a first line that makes people stop scrolling; up to about "
        "150 words. Never write a URL — links are not clickable in Instagram captions. Only "
        "when the post is about a page or article on the website, end with \"Link in bio\"."
    ),
    "linkedin": (
        "Professional and useful, 50 to 150 words in short paragraphs. Put the link, if any, "
        "in the link field, not in the text."
    ),
    "x": (
        "Punchy, at most 200 characters of text so the link and hashtags fit within 280. "
        "Put the link, if any, in the link field, not in the text."
    ),
    "pinterest": (
        "A title of at most 90 characters with the words people search for, and a "
        "description of one to three sentences (under 400 characters). Every Pin links to "
        "the website, so always choose a link."
    ),
}

POST_TYPES = {
    "tip": "A practical tip for the business's customers, related to what the business does.",
    "highlight": "Spotlight one product or service the business offers: what it is and who it is for.",
    "question": "Ask the audience an engaging question related to the business, inviting replies.",
    "article": "Share the business's article named below and why it is worth reading.",
    "social_proof": (
        "Social proof, using ONLY the claim quoted below exactly as the profile states it. "
        "No invented numbers, names or quotes."
    ),
}


def week_plan(
    *,
    brand_brief: str,
    business: str,
    network: str,
    items: list[dict],
    hashtags_per_post: int,
    allowed_links: list[str],
    avoid_topics: list[str],
) -> str:
    rules = NETWORKS[network]
    lines = [
        brand_brief or f"BUSINESS: {business}\n(No brand profile has been written; infer only "
        "what the business name makes certain, and keep claims general.)",
        "",
        f"Write {len(items)} {rules.label} posts for the coming week.",
        f"{rules.label} style: {STYLE[network]}",
        f"Hashtags: {hashtags_per_post} per post, relevant and specific (none if 0)."
        if hashtags_per_post
        else "Hashtags: none.",
        "Image prompt: describe a realistic scene that fits the post. No text, letters, logos "
        "or brand names in the image.",
        "",
        "Allowed links (use only these, exactly as written, or leave empty):",
        *(f"- {link}" for link in allowed_links),
        *(["- (none)"] if not allowed_links else []),
    ]
    if avoid_topics:
        lines += ["", "Recent posts already covered these topics; choose different angles:"]
        lines += [f"- {topic}" for topic in avoid_topics[:30]]
    lines += ["", "The posts, in order:"]
    for number, item in enumerate(items, start=1):
        line = f"{number}. {item['type']}: {POST_TYPES[item['type']]}"
        if item.get("subject"):
            line += f" Subject: {item['subject']}"
        if item.get("link"):
            line += f" Link: {item['link']}"
        lines.append(line)
    lines += ["", "Return one entry per post, with \"index\" matching the numbers above."]
    return "\n".join(lines)


#: Profile statements that can carry a social-proof post (reviews, ratings,
#: awards, track record). Without one, no social-proof post is planned.
_PROOF = re.compile(
    r"\b(reviews?|rated|ratings?|testimonials?|award|award-winning|certified|accredited|"
    r"trusted by|since (?:19|20)\d\d|\d[\d,]*\+?\s+(?:customers|clients|years|projects|"
    r"students|patients|families|businesses))\b",
    re.I,
)


def proof_claims(profile: dict) -> list[str]:
    """Statements in the brand profile that support a social-proof post."""
    found: list[str] = []
    for key in ("value_propositions", "differentiators", "summary"):
        value = profile.get(key)
        for item in value if isinstance(value, list) else [value]:
            text = str(item or "").strip()
            if text and _PROOF.search(text) and text not in found:
                found.append(text)
    return found[:5]


def parse_posts(data: object) -> dict[int, dict]:
    """The model's posts keyed by their index; anything malformed is dropped."""
    posts = (data or {}).get("posts") if isinstance(data, dict) else data
    out: dict[int, dict] = {}
    for position, item in enumerate(posts if isinstance(posts, list) else [], start=1):
        if not isinstance(item, dict) or not str(item.get("text") or "").strip():
            continue
        try:
            index = int(item.get("index") or position)
        except (TypeError, ValueError):
            index = position
        out.setdefault(index, item)
    return out

