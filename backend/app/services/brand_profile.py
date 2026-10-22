"""Brand profile: what the business is, read from its own website.

Paste a URL and the platform reads the home page and a handful of the pages
that describe the business (about, services, products, pricing, contact),
then asks the workspace's connected AI model to summarise what it sells, who
for, how it talks, what makes it different and what people search for to find
it. The result is stored on the organisation and every content agent writes
from it — so copy sounds like the business instead of like a template.

The profile is the customer's to correct: edits are kept, and regenerating
replaces it only when they ask.
"""
from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy.orm import Session

from app.connectors.base import html_pages as hp
from app.core.exceptions import ConflictError, InvalidInputError
from app.core.logging import get_logger
from app.core.netguard import safe_get
from app.db.base import utcnow
from app.models.workspace import Organization

log = get_logger(__name__)

#: Pages read in total, home page included.
MAX_PAGES = 6
#: Characters of visible text kept per page, so one long page cannot crowd
#: out the rest of the site in the prompt.
CHARS_PER_PAGE = 3500

#: Paths that usually describe the business, most useful first.
_USEFUL = re.compile(
    r"/(about|about-us|who-we-are|company|services?|what-we-do|products?|shop|"
    r"pricing|plans|menu|treatments|solutions|contact|locations?)(/|$)",
    re.I,
)

#: Fields of a profile, in the order the console shows them.
LIST_FIELDS = (
    "products_services",
    "value_propositions",
    "differentiators",
    "audiences",
    "competitors",
    "keywords",
    "locations",
)
TEXT_FIELDS = ("business_name", "summary", "industry", "tone", "language", "call_to_action")

SYSTEM = (
    "You analyse a business's own website and describe the business accurately. "
    "Only state what the pages support. If something is not on the pages, leave it "
    "empty rather than guessing. Competitors may be named only if the pages name "
    "them or they are well-known direct competitors in the same local market; "
    "otherwise leave the list empty."
)

SCHEMA_HINT = """{
  "business_name": "string",
  "summary": "one or two sentences: what the business does and for whom",
  "industry": "short category, e.g. hair salon, B2B SaaS, used car dealer",
  "products_services": ["main products or services"],
  "audiences": ["who buys, e.g. working professionals in Noida"],
  "value_propositions": ["why customers choose it, in its own claims"],
  "differentiators": ["what is different from alternatives"],
  "tone": "how it writes, e.g. warm and informal",
  "keywords": ["10-20 phrases customers would search for"],
  "competitors": ["names or domains"],
  "locations": ["cities or areas served, if local"],
  "language": "main language of the site, e.g. English",
  "call_to_action": "the main thing it wants visitors to do"
}"""


def normalise_url(raw: str) -> str:
    url = (raw or "").strip()
    if not url:
        raise InvalidInputError("Enter your website address")
    if "://" not in url:
        url = "https://" + url
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


def read_site(url: str) -> list[hp.RenderedPage]:
    """The home page plus the pages most likely to describe the business."""
    site = normalise_url(url)
    home = safe_get(site + "/")
    if home.status_code >= 400:
        raise InvalidInputError(f"{site} answered with an error ({home.status_code}).")
    final = str(home.url).rstrip("/") if home.url else site
    pages = [hp.parse(final, home.text, status=home.status_code)]

    candidates: list[str] = []
    for href in pages[0].links:
        absolute = hp.absolute(href, base=final + "/")
        if hp.same_site(absolute, final) and _USEFUL.search(urlsplit(absolute).path or ""):
            clean = absolute.split("#")[0].rstrip("/")
            if clean not in candidates and clean != final:
                candidates.append(clean)

    for link in candidates[: MAX_PAGES - 1]:
        try:
            response = safe_get(link)
        except Exception as exc:  # noqa: BLE001 - one unreadable page is not fatal
            log.info("Brand profile: skipped %s (%s)", link, exc)
            continue
        if response.status_code < 400 and "html" in response.headers.get("content-type", "html"):
            pages.append(hp.parse(link, response.text, status=response.status_code))
    return pages


def _prompt(pages: list[hp.RenderedPage]) -> str:
    blocks = []
    for page in pages:
        text = page.text[:CHARS_PER_PAGE]
        blocks.append(
            f"URL: {page.url}\nTITLE: {page.title}\nDESCRIPTION: {page.description}\n"
            f"HEADINGS: {' | '.join(page.headings[:12])}\nTEXT: {text}"
        )
    return "Describe this business from its website pages.\n\n" + "\n\n---\n\n".join(blocks)


def _clean(raw: Any) -> dict[str, Any]:
    data = raw if isinstance(raw, dict) else {}
    out: dict[str, Any] = {}
    for key in TEXT_FIELDS:
        out[key] = " ".join(str(data.get(key) or "").split())[:400]
    for key in LIST_FIELDS:
        items = data.get(key) or []
        if not isinstance(items, list):
            items = [items]
        seen: list[str] = []
        for item in items:
            text = " ".join(str(item or "").split())[:120]
            if text and text.lower() not in {s.lower() for s in seen}:
                seen.append(text)
        out[key] = seen[:25]
    return out


def workspace_llm(db: Session, org: Organization):  # noqa: ANN201
    """The first connected AI model in catalogue order, as a provider."""
    from app.connectors.base import registry
    from app.llm.from_connector import provider_from_connector, supports_agent_llm
    from app.services.connectors import build_connector_bundle

    bundle = build_connector_bundle(db, org)
    for slug in registry.connector_slugs():
        if slug in bundle.instances and supports_agent_llm(slug):
            return provider_from_connector(slug, bundle.instances, names=bundle.names)
    raise ConflictError(
        "Connect an AI model (OpenAI, Anthropic Claude, Google Gemini or Perplexity) "
        "under Integrations first — it writes the brand profile."
    )


def generate(db: Session, org: Organization, url: str) -> dict[str, Any]:
    """Read the site, build the profile, and store it on the organisation."""
    site = normalise_url(url)
    pages = read_site(site)
    if not any(page.word_count for page in pages):
        raise InvalidInputError(
            "That website shows almost no text to a crawler (it may be built entirely in "
            "JavaScript), so there is nothing to read. Fill in the profile by hand instead."
        )
    llm = workspace_llm(db, org)
    data, _usage = llm.complete_json(_prompt(pages), system=SYSTEM, schema_hint=SCHEMA_HINT)
    profile = _clean(data)
    profile.update(
        source_url=site,
        pages_read=[page.url for page in pages],
        generated_at=utcnow().isoformat(),
        edited=False,
    )
    _store(org, profile)
    if not org.primary_domain:
        org.primary_domain = urlsplit(site).hostname.removeprefix("www.") if urlsplit(site).hostname else ""
    db.flush()
    return profile


def update(db: Session, org: Organization, values: dict[str, Any]) -> dict[str, Any]:
    """Apply the customer's edits. Unknown keys are ignored.

    ``timezone`` is a workspace setting rather than a profile field: content
    agents schedule posts at local times from it.
    """
    if "timezone" in values:
        set_timezone(org, str(values.get("timezone") or ""))
    current = get(org) or {}
    edited = _clean({**current, **{k: v for k, v in values.items() if k in TEXT_FIELDS + LIST_FIELDS}})
    profile = {**current, **edited, "edited": True, "edited_at": utcnow().isoformat()}
    _store(org, profile)
    db.flush()
    return profile


def store(org: Organization, profile: dict[str, Any]) -> None:
    """Write ``profile`` as the organisation's brand profile (used by restore)."""
    _store(org, profile)


def _store(org: Organization, profile: dict[str, Any]) -> None:
    settings = dict(org.settings or {})
    settings["brand_profile"] = profile
    # Agents already read "industry"; keep it in step with the profile.
    if profile.get("industry"):
        settings["industry"] = profile["industry"]
    org.settings = settings


def get(org: Organization) -> dict[str, Any] | None:
    profile = (org.settings or {}).get("brand_profile")
    return profile if isinstance(profile, dict) else None


def brief(org: Organization) -> str:
    """The profile as a compact block for agent prompts, or "" when none."""
    profile = get(org)
    if not profile:
        return ""
    lines = [f"BUSINESS: {profile.get('business_name') or org.name}"]
    labels = {
        "summary": "WHAT IT DOES",
        "industry": "INDUSTRY",
        "tone": "TONE OF VOICE",
        "language": "LANGUAGE",
        "call_to_action": "MAIN CALL TO ACTION",
    }
    for key, label in labels.items():
        if profile.get(key):
            lines.append(f"{label}: {profile[key]}")
    list_labels = {
        "products_services": "PRODUCTS/SERVICES",
        "audiences": "AUDIENCES",
        "value_propositions": "VALUE PROPOSITIONS",
        "differentiators": "DIFFERENTIATORS",
        "locations": "LOCATIONS SERVED",
        "keywords": "SEARCH KEYWORDS",
    }
    for key, label in list_labels.items():
        if profile.get(key):
            lines.append(f"{label}: " + "; ".join(profile[key][:15]))
    return "\n".join(lines)


def set_timezone(org: Organization, name: str) -> None:
    """Store the workspace time zone (an IANA name such as Asia/Kolkata), or clear it."""
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    name = name.strip()
    settings = dict(org.settings or {})
    if not name:
        settings.pop("timezone", None)
    else:
        try:
            ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError):
            raise InvalidInputError(
                f"{name!r} is not a time zone name. Use a name like Asia/Kolkata or Europe/London."
            ) from None
        settings["timezone"] = name
    org.settings = settings


def timezone_of(org: Organization) -> str:
    return str((org.settings or {}).get("timezone") or "")
