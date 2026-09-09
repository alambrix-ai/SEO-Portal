"""Prompt for dynamic creative generation."""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from app.agents.dynamic_creative_optimizer.specs import Product

from app.agents.base.style import HOUSE_STYLE

SYSTEM = (
    "You are a performance copywriter producing ad variants for several "
    "platforms at once. You write to each platform's own register: search and "
    "display copy is short and literal, social copy is conversational, "
    "professional networks are plainer. You only state facts present in the "
    "product data you are given. Never invent a price, a discount, a "
    "deadline, a rating or an availability claim, and avoid superlatives "
    "no one could substantiate."
)
SYSTEM = SYSTEM + HOUSE_STYLE

SCHEMA_HINT = """{
  "variants": [
    {
      "platform": "Google Ads | Meta Ads | LinkedIn Ads | TikTok Ads | Microsoft Ads | DSP / SSP Exchange",
      "dimensions": "e.g. 300x250",
      "headline": "short headline",
      "body_copy": "one or two sentences",
      "call_to_action": "e.g. See offers",
      "audience_segment": "which segment this is written for"
    }
  ]
}"""

def creative_variants(
    *,
    product: Product,
    platforms: list[str],
    brand: str,
    segments: list[str],
    page_url: str,
) -> str:
    features = (
        "\nFEATURES:\n" + "\n".join(f"- {f}" for f in product.features)
        if product.features
        else ""
    )
    price = f"\nPRICE_ON_PAGE: {product.price}" if product.price else ""

    # The exact limits, not "respect each platform's usual copy length".
    # check_copy() rejects anything over them, so a prompt that keeps them
    # secret has the model write copy the validator then throws away — and
    # the operator sees fewer variants with no idea why.
    from app.agents.dynamic_creative_optimizer.specs import PLATFORMS

    limits = "\n".join(
        f"- {name}: headline ≤ {spec.headline_max} characters, "
        f"body ≤ {spec.body_max} characters, sizes {', '.join(spec.dimensions)}"
        for name in platforms
        if (spec := PLATFORMS.get(name)) is not None
    )
    return f"""[TASK:creative_variants]
Produce ad variants for this product.

PRODUCT: {product.name}
CATEGORY: {product.category}
BRAND: {brand}
LANDING_PAGE: {page_url}
SEGMENTS: {", ".join(segments)}{price}{features}

PLATFORM LIMITS. Copy over these is rejected, not truncated:
{limits}

Write one or two variants per platform, each aimed at one of the segments
above, and pick the `dimensions` from that platform's sizes listed above.

Count the characters. A headline one character over the limit is a variant
somebody has to rewrite by hand.

Write to each platform's register: search and display copy short and literal,
social conversational, professional networks plainer. No superlatives you
could not substantiate, no more than one exclamation mark, and never a
headline in all capitals. Those are rejected too.

If no price is given above, do not mention price at all."""
