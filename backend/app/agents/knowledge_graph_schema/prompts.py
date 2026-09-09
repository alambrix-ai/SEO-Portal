"""Prompts, JSON-LD validation and rendering for the schema agent."""
from __future__ import annotations

import json
import re

from app.agents.base.style import HOUSE_STYLE

SYSTEM = (
    "You are a structured-data engineer. You write schema.org JSON-LD that "
    "validates against Google's rich-result requirements and describes only "
    "what is actually present on the page. You never fabricate prices, "
    "ratings, review counts, availability or opening hours: if the page does "
    "not state a value, you omit the property rather than guess it."
)
SYSTEM = SYSTEM + HOUSE_STYLE

SCHEMA_HINT = """{
  "patches": [
    {
      "schema_type": "FAQPage | Product | LocalBusiness | ItemList | Service",
      "json_ld": { "@context": "https://schema.org", "@type": "...", ... }
    }
  ]
}"""

# Marker the Q&A injector leaves behind; its presence means an FAQ graph has
# real questions on the page to describe.
SECTION_MARKER = "<!-- automarket:aeo:start -->"

_SCRIPT_RE_TEMPLATE = (
    r'<script type="application/ld\+json" data-automarket="{kind}">.*?</script>'
)

# Properties that must be present for each type to earn a rich result.
_REQUIRED: dict[str, tuple[str, ...]] = {
    "FAQPage": ("mainEntity",),
    "Product": ("name",),
    "LocalBusiness": ("name",),
    "ItemList": ("name",),
    "Service": ("name",),
}
# Properties a model should never invent. Present-but-empty is also rejected.
_UNVERIFIABLE: tuple[str, ...] = ("aggregateRating", "review", "reviewCount", "ratingValue")

def schema_graph(
    *,
    title: str,
    url: str,
    body: str,
    schema_types: list[str],
    organization: str,
    max_body_chars: int = 5000,
) -> str:
    return f"""[TASK:schema_graph]
Build schema.org JSON-LD for this page.

TITLE: {title}
URL: {url}
ORGANIZATION: {organization}
SCHEMA_TYPES: {", ".join(schema_types)}

PAGE CONTENT:
{body[:max_body_chars]}

Produce one graph per requested type. Use the absolute URL above for any
`url` property. Include only properties the content above supports, and never
include ratings or reviews unless the page itself shows them."""

def validate_json_ld(graph: dict, schema_type: str) -> str | None:
    """Return a reason to reject the graph, or ``None`` if it is usable."""
    if graph.get("@context") not in ("https://schema.org", "http://schema.org"):
        return "missing or wrong @context"
    if not graph.get("@type"):
        return "missing @type"

    for prop in _REQUIRED.get(schema_type, ()):
        value = graph.get(prop)
        if value in (None, "", [], {}):
            return f"missing required property {prop!r}"

    for prop in _UNVERIFIABLE:
        if prop in graph and not graph[prop]:
            return f"empty unverifiable property {prop!r}"

    if schema_type == "FAQPage":
        entities = graph.get("mainEntity")
        if not isinstance(entities, list) or not entities:
            return "mainEntity must be a non-empty list of Questions"
        for entity in entities:
            if not isinstance(entity, dict) or not entity.get("name"):
                return "each Question needs a name"
            answer = entity.get("acceptedAnswer") or {}
            if not isinstance(answer, dict) or not answer.get("text"):
                return "each Question needs an acceptedAnswer with text"

    try:
        json.dumps(graph)
    except (TypeError, ValueError):
        return "graph is not JSON-serialisable"
    return None

def merge_script_tag(body: str, graph: dict, schema_type: str) -> str:
    """Insert or replace this agent's script tag for one schema type."""
    payload = json.dumps(graph, indent=2, ensure_ascii=False)
    tag = (
        f'<script type="application/ld+json" data-automarket="{schema_type}">\n'
        f"{payload}\n"
        "</script>"
    )
    pattern = re.compile(
        _SCRIPT_RE_TEMPLATE.format(kind=re.escape(schema_type)), re.DOTALL
    )
    if pattern.search(body):
        return pattern.sub(tag, body)
    return f"{body.rstrip()}\n\n{tag}\n"
