"""Measure a public website: everything here is observed, nothing inferred.

The report's credibility rests on this module. Every fact it returns was read
from the site (or from Google's PageSpeed API) during this analysis, so the
report can say "measured" next to it. Judgement — what the business is, what
to write, what to say in an ad — is the model's job, in ``ai.py``, and the
report labels it as such.
"""
from __future__ import annotations

import html
import json
import re
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

import httpx

from app.connectors.base import html_pages as hp
from app.core.exceptions import InvalidInputError
from app.core.logging import get_logger
from app.core.netguard import check_public_url, safe_get

log = get_logger(__name__)

#: Pages read in full, home page included.
MAX_PAGES = 12
#: Further internal links checked only for whether they answer.
MAX_LINK_CHECKS = 30
#: Visible text kept per page for the model.
TEXT_PER_PAGE = 2500

_H2 = re.compile(r"<h2\b[^>]*>(.*?)</h2>", re.DOTALL | re.I)
_H1_TAG = re.compile(r"<h1\b", re.I)
_IMG = re.compile(r"<img\b[^>]*>", re.I)
_LD_JSON = re.compile(r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', re.DOTALL | re.I)
_HTML_LANG = re.compile(r"<html\b[^>]*\blang=[\"']([^\"']+)[\"']", re.I)
_VIEWPORT = re.compile(r"<meta\b[^>]*name=[\"']viewport[\"']", re.I)
_HREFLANG = re.compile(r"<link\b[^>]*hreflang=", re.I)
_ICON = re.compile(r"<link\b[^>]*rel=[\"'][^\"']*icon[^\"']*[\"']", re.I)
_FORM = re.compile(r"<form\b", re.I)
_META = re.compile(r"<meta\b([^>]*)>", re.I)
_ATTR = re.compile(r'([a-zA-Z:_-]+)\s*=\s*"([^"]*)"|([a-zA-Z:_-]+)\s*=\s*\'([^\']*)\'')
_EMAIL_TEXT = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")

#: Tracking and marketing tags, by what they look like in the HTML.
TRACKERS: dict[str, re.Pattern[str]] = {
    "Google Analytics 4": re.compile(r"gtag\(\s*['\"]config['\"]\s*,\s*['\"]G-|googletagmanager\.com/gtag/js\?id=G-", re.I),
    "Google Tag Manager": re.compile(r"googletagmanager\.com/gtm\.js|GTM-[A-Z0-9]{4,}", re.I),
    "Google Ads tag": re.compile(r"AW-\d{6,}", re.I),
    "Meta Pixel": re.compile(r"connect\.facebook\.net/[^\"']*/fbevents\.js|fbq\(\s*['\"]init", re.I),
    "LinkedIn Insight": re.compile(r"snap\.licdn\.com/li\.lms-analytics|_linkedin_partner_id", re.I),
    "Microsoft Clarity": re.compile(r"clarity\.ms/tag", re.I),
    "Hotjar": re.compile(r"static\.hotjar\.com|hjid", re.I),
    "TikTok Pixel": re.compile(r"analytics\.tiktok\.com", re.I),
    "Microsoft Ads (UET)": re.compile(r"bat\.bing\.com/bat\.js", re.I),
}

#: Platforms recognised in outbound links and in the business link.
PLATFORMS: dict[str, tuple[str, ...]] = {
    "Google Business Profile": ("google.com/maps", "maps.google.", "goo.gl/maps", "maps.app.goo.gl", "g.page", "business.google.com"),
    "Facebook": ("facebook.com", "fb.com"),
    "Instagram": ("instagram.com",),
    "LinkedIn": ("linkedin.com",),
    "X (Twitter)": ("twitter.com", "x.com"),
    "YouTube": ("youtube.com", "youtu.be"),
    "TikTok": ("tiktok.com",),
    "Pinterest": ("pinterest.",),
    "Yelp": ("yelp.",),
    "Tripadvisor": ("tripadvisor.",),
    "Justdial": ("justdial.com",),
    "Trustpilot": ("trustpilot.com",),
    "WhatsApp": ("wa.me", "api.whatsapp.com"),
}

_GENERATORS = (
    ("WordPress", re.compile(r"wp-content|wp-includes|<meta[^>]*generator[^>]*WordPress", re.I)),
    ("Shopify", re.compile(r"cdn\.shopify\.com|Shopify\.theme", re.I)),
    ("Wix", re.compile(r"static\.wixstatic\.com|<meta[^>]*generator[^>]*Wix", re.I)),
    ("Squarespace", re.compile(r"static1\.squarespace\.com|Squarespace", re.I)),
    ("Webflow", re.compile(r"data-wf-site|webflow\.com", re.I)),
    ("Next.js", re.compile(r"__NEXT_DATA__|/_next/static", re.I)),
    ("Framer", re.compile(r"framerusercontent\.com", re.I)),
    ("Drupal", re.compile(r"Drupal\.settings|/sites/default/files", re.I)),
    ("Joomla", re.compile(r"<meta[^>]*generator[^>]*Joomla", re.I)),
)


@dataclass(slots=True)
class PageFacts:
    url: str
    status: int
    response_ms: int
    html_kb: int
    title: str = ""
    meta_description: str = ""
    canonical: str = ""
    h1: list[str] = field(default_factory=list)
    h1_count: int = 0
    h2: list[str] = field(default_factory=list)
    word_count: int = 0
    images: int = 0
    images_without_alt: int = 0
    internal_links: int = 0
    external_links: int = 0
    schema_types: list[str] = field(default_factory=list)
    open_graph: bool = False
    twitter_card: bool = False
    viewport: bool = False
    lang: str = ""
    hreflang: bool = False
    noindex: bool = False
    client_side_rendered: bool = False
    has_form: bool = False
    text: str = ""


@dataclass(slots=True)
class SiteFacts:
    input_url: str
    final_url: str
    https: bool
    http_redirects_to_https: bool | None
    platform: str
    robots_txt: bool
    robots_blocks_all: bool
    sitemap_url: str
    sitemap_url_count: int | None
    llms_txt: bool
    favicon: bool
    trackers: list[str]
    social_links: dict[str, str]
    phones: list[str]
    emails: list[str]
    local_business_schema: dict[str, Any] | None
    pages: list[PageFacts]
    links_checked: int
    broken_links: list[dict[str, Any]]
    pagespeed: dict[str, Any]
    business: dict[str, Any]
    duration_s: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        out = asdict(self)
        for page in out["pages"]:
            page.pop("text", None)
        return out


def normalise(raw: str) -> str:
    url = (raw or "").strip()
    if not url:
        raise InvalidInputError("Enter your website address")
    if "://" not in url:
        url = "https://" + url
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname or "." not in parts.hostname:
        raise InvalidInputError("Enter a full website address, for example https://yourbusiness.com")
    return f"{parts.scheme}://{parts.netloc.lower()}"


def _fetch(url: str, *, max_bytes: int = 3_000_000, timeout: float = 15.0) -> tuple[httpx.Response, int]:
    started = time.perf_counter()
    response = safe_get(url, timeout=timeout, max_bytes=max_bytes)
    return response, int((time.perf_counter() - started) * 1000)


def _metas(page_html: str) -> list[dict[str, str]]:
    out = []
    for raw in _META.findall(page_html):
        attrs: dict[str, str] = {}
        for m in _ATTR.finditer(raw):
            key = (m.group(1) or m.group(3) or "").lower()
            attrs[key] = html.unescape(m.group(2) if m.group(2) is not None else (m.group(4) or ""))
        out.append(attrs)
    return out


def _text(fragment: str) -> str:
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", fragment)).split())


def page_facts(url: str, body: str, *, status: int, response_ms: int, site: str) -> PageFacts:
    parsed = hp.parse(url, body, status=status)
    metas = _metas(body)
    names = {(m.get("property") or m.get("name") or "").lower() for m in metas}
    internal = external = 0
    for href in parsed.links:
        absolute = hp.absolute(href, base=url + "/")
        if not absolute:
            continue
        if hp.same_site(absolute, site):
            internal += 1
        else:
            external += 1
    lang = _HTML_LANG.search(body)
    return PageFacts(
        url=url,
        status=status,
        response_ms=response_ms,
        html_kb=round(len(body.encode("utf-8", "ignore")) / 1024),
        title=parsed.title[:300],
        meta_description=parsed.description[:500],
        canonical=parsed.canonical,
        h1=[h[:200] for h in parsed.headings[:5]],
        h1_count=len(_H1_TAG.findall(body)),
        h2=[_text(h)[:160] for h in _H2.findall(body)[:12]],
        word_count=parsed.word_count,
        images=len(_IMG.findall(body)),
        images_without_alt=parsed.images_without_alt,
        internal_links=internal,
        external_links=external,
        schema_types=parsed.schema_types,
        open_graph="og:title" in names,
        twitter_card="twitter:card" in names,
        viewport=bool(_VIEWPORT.search(body)),
        lang=lang.group(1) if lang else "",
        hreflang=bool(_HREFLANG.search(body)),
        noindex=parsed.robots_noindex,
        client_side_rendered=parsed.client_side_rendered,
        has_form=bool(_FORM.search(body)),
        text=parsed.text[:TEXT_PER_PAGE],
    )


def _local_business(bodies: list[str]) -> dict[str, Any] | None:
    """Name, phone and address from LocalBusiness/Organization JSON-LD."""
    wanted = ("LocalBusiness", "Organization", "Store", "Restaurant", "ProfessionalService")

    def walk(node: Any):  # noqa: ANN202
        if isinstance(node, list):
            for item in node:
                yield from walk(item)
        elif isinstance(node, dict):
            yield node
            for key in ("@graph", "mainEntity"):
                if key in node:
                    yield from walk(node[key])

    for body in bodies:
        for block in _LD_JSON.findall(body):
            try:
                data = json.loads(block.strip())
            except ValueError:
                continue
            for node in walk(data):
                types = node.get("@type")
                types = types if isinstance(types, list) else [types]
                # schema.org has hundreds of LocalBusiness subtypes (HairSalon,
                # Dentist, Plumber...); any typed node with an address or phone
                # is describing the business.
                typed = any(isinstance(t, str) and (t in wanted or t.endswith("Business")) for t in types)
                if not typed and not (types[0] and (node.get("address") or node.get("telephone"))):
                    continue
                address = node.get("address")
                if isinstance(address, dict):
                    address = ", ".join(
                        str(address.get(k)) for k in ("streetAddress", "addressLocality", "addressRegion", "postalCode", "addressCountry")
                        if address.get(k) and not isinstance(address.get(k), dict)
                    )
                return {
                    "type": next((t for t in types if isinstance(t, str)), ""),
                    "name": str(node.get("name") or "")[:200],
                    "telephone": str(node.get("telephone") or "")[:60],
                    "address": str(address or "")[:300],
                    "has_opening_hours": bool(node.get("openingHours") or node.get("openingHoursSpecification")),
                    "has_rating": bool(node.get("aggregateRating")),
                    "same_as": [str(s) for s in (node.get("sameAs") or []) if isinstance(s, str)][:10]
                    if isinstance(node.get("sameAs"), list) else [],
                }
    return None


def _platform_of(url: str) -> str:
    low = url.lower()
    for name, needles in PLATFORMS.items():
        if any(n in low for n in needles):
            return name
    return ""


def business_listing(raw: str, site_social: dict[str, str]) -> dict[str, Any]:
    """What can honestly be read from the business link the visitor gave.

    Google Maps and most review sites render with JavaScript and refuse
    automated reads, so for them the link itself is the evidence: which
    platform, the business name in the address, and whether the website links
    back to it. Pages that do answer are read for their title and description.
    """
    url = (raw or "").strip()
    if not url:
        return {"provided": False}
    if "://" not in url:
        url = "https://" + url
    info: dict[str, Any] = {"provided": True, "url": url[:1000], "platform": _platform_of(url) or "Website or listing"}
    parts = urlsplit(url)
    match = re.search(r"/maps/place/([^/@]+)", parts.path)
    if match:
        info["name_in_link"] = unquote(match.group(1)).replace("+", " ")[:200]
    elif "q" in parse_qs(parts.query):
        info["name_in_link"] = parse_qs(parts.query)["q"][0][:200]
    try:
        check_public_url(url)
        response, _ = _fetch(url, max_bytes=1_500_000, timeout=10.0)
        info["reachable"] = response.status_code < 400
        info["final_url"] = str(response.url)[:1000]
        if response.status_code < 400 and "html" in response.headers.get("content-type", "html"):
            metas = {(m.get("property") or m.get("name") or "").lower(): m.get("content", "") for m in _metas(response.text)}
            title = re.search(r"<title[^>]*>(.*?)</title>", response.text, re.DOTALL | re.I)
            info["page_title"] = (metas.get("og:title") or (_text(title.group(1)) if title else ""))[:200]
            info["page_description"] = (metas.get("og:description") or metas.get("description") or "")[:400]
            final_platform = _platform_of(info["final_url"])
            if final_platform:
                info["platform"] = final_platform
    except Exception as exc:  # noqa: BLE001 - an unreadable listing is reported, not fatal
        info["reachable"] = False
        info["read_error"] = str(exc)[:200]
    info["linked_from_website"] = any(
        _platform_of(link) == info["platform"] for link in site_social.values()
    ) if info["platform"] in PLATFORMS else None
    return info


def pagespeed(url: str, api_key: str = "") -> dict[str, Any]:
    """Mobile Lighthouse lab scores and real-user field data from Google."""
    params: list[tuple[str, str]] = [("url", url), ("strategy", "mobile")]
    for category in ("PERFORMANCE", "SEO", "ACCESSIBILITY", "BEST_PRACTICES"):
        params.append(("category", category))
    if api_key:
        params.append(("key", api_key))
    try:
        response = httpx.get(
            "https://www.googleapis.com/pagespeedonline/v5/runPagespeed", params=params, timeout=90.0
        )
    except httpx.HTTPError as exc:
        return {"measured": False, "reason": f"PageSpeed Insights did not answer ({type(exc).__name__})."}
    if response.status_code == 429:
        return {"measured": False, "reason": "Google's PageSpeed quota is used up for now."}
    if response.status_code >= 400:
        return {"measured": False, "reason": f"PageSpeed Insights could not test the page ({response.status_code})."}
    data = response.json()
    lighthouse = data.get("lighthouseResult") or {}
    categories = lighthouse.get("categories") or {}
    audits = lighthouse.get("audits") or {}

    def score(key: str) -> int | None:
        value = (categories.get(key) or {}).get("score")
        return round(value * 100) if isinstance(value, (int, float)) else None

    def metric(key: str) -> dict[str, Any] | None:
        audit = audits.get(key) or {}
        if audit.get("numericValue") is None:
            return None
        return {"value": round(audit["numericValue"], 3), "display": audit.get("displayValue", "")}

    field_metrics = ((data.get("loadingExperience") or {}).get("metrics")) or {}

    def field_value(key: str) -> dict[str, Any] | None:
        entry = field_metrics.get(key) or {}
        if entry.get("percentile") is None:
            return None
        return {"p75": entry["percentile"], "rating": entry.get("category", "")}

    opportunities = []
    for audit in audits.values():
        details = audit.get("details") or {}
        if details.get("type") == "opportunity" and (details.get("overallSavingsMs") or 0) >= 300:
            opportunities.append({"title": audit.get("title", ""), "saving_ms": round(details["overallSavingsMs"])})
    opportunities.sort(key=lambda o: -o["saving_ms"])
    return {
        "measured": True,
        "strategy": "mobile",
        "scores": {
            "performance": score("performance"),
            "seo": score("seo"),
            "accessibility": score("accessibility"),
            "best_practices": score("best-practices"),
        },
        "lab": {
            "lcp": metric("largest-contentful-paint"),
            "cls": metric("cumulative-layout-shift"),
            "tbt": metric("total-blocking-time"),
            "fcp": metric("first-contentful-paint"),
            "speed_index": metric("speed-index"),
        },
        "field": {
            "lcp": field_value("LARGEST_CONTENTFUL_PAINT_MS"),
            "inp": field_value("INTERACTION_TO_NEXT_PAINT"),
            "cls": field_value("CUMULATIVE_LAYOUT_SHIFT_SCORE"),
        } if field_metrics else None,
        "opportunities": opportunities[:6],
    }


def collect(
    website: str,
    business_url: str = "",
    *,
    pagespeed_key: str = "",
    step: Callable[[str, str, str], None] = lambda key, state, detail: None,
) -> tuple[SiteFacts, list[PageFacts]]:
    """Read the site. ``step(key, state, detail)`` reports progress."""
    started = time.perf_counter()
    site = normalise(website)
    host = urlsplit(site).hostname or ""

    step("fetch", "running", f"Opening {host}")
    with ThreadPoolExecutor(max_workers=1) as speed_pool:
        # PageSpeed takes 20-60 seconds; run it while the crawl happens.
        speed_future = speed_pool.submit(pagespeed, site + "/", pagespeed_key)

        try:
            home, home_ms = _fetch(site + "/")
        except InvalidInputError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise InvalidInputError(f"{host} could not be reached ({type(exc).__name__}). Check the address and try again.") from None
        if home.status_code >= 400:
            raise InvalidInputError(f"{host} answered with an error ({home.status_code}), so it could not be analysed.")
        final = str(home.url).rstrip("/") if home.url else site
        final_root = f"{urlsplit(final).scheme}://{urlsplit(final).netloc}"
        step("fetch", "done", f"Reached {urlsplit(final).netloc} in {home_ms} ms")

        http_upgrade: bool | None = None
        if urlsplit(final_root).scheme == "https":
            try:
                plain, _ = _fetch(f"http://{urlsplit(final_root).netloc}/", max_bytes=200_000, timeout=8.0)
                http_upgrade = str(plain.url).startswith("https://")
            except Exception:  # noqa: BLE001 - port 80 closed is fine
                http_upgrade = None

        step("discover", "running", "Reading robots.txt and sitemap")
        robots_body = ""
        try:
            robots, _ = _fetch(final_root + "/robots.txt", max_bytes=300_000, timeout=8.0)
            if robots.status_code < 400 and "<html" not in robots.text[:500].lower():
                robots_body = robots.text
        except Exception:  # noqa: BLE001
            pass
        disallow = hp.disallowed_paths(robots_body) if robots_body else []
        sitemap_candidates = [
            line.split(":", 1)[1].strip()
            for line in robots_body.splitlines()
            if line.lower().startswith("sitemap:")
        ] or [final_root + "/sitemap.xml", final_root + "/sitemap_index.xml"]
        sitemap_used, sitemap_pages = "", []
        sitemap_count: int | None = None
        for candidate in sitemap_candidates[:3]:
            try:
                response, _ = _fetch(candidate, max_bytes=5_000_000, timeout=12.0)
            except Exception:  # noqa: BLE001
                continue
            if response.status_code >= 400 or "<" not in response.text[:200]:
                continue
            locations, is_index = hp.sitemap_urls(response.text)
            if not locations:
                continue
            sitemap_used = candidate
            if is_index:
                pages_total = 0
                for child in locations[:5]:
                    try:
                        child_response, _ = _fetch(child, max_bytes=5_000_000, timeout=12.0)
                        child_pages, _ = hp.sitemap_urls(child_response.text)
                        pages_total += len(child_pages)
                        sitemap_pages.extend(child_pages[:200])
                    except Exception:  # noqa: BLE001
                        continue
                sitemap_count = pages_total if len(locations) <= 5 else None
            else:
                sitemap_pages = locations
                sitemap_count = len(locations)
            break
        llms = False
        try:
            llms_response, _ = _fetch(final_root + "/llms.txt", max_bytes=200_000, timeout=8.0)
            llms = llms_response.status_code < 400 and "<html" not in llms_response.text[:500].lower() and bool(llms_response.text.strip())
        except Exception:  # noqa: BLE001
            pass
        step("discover", "done", (
            f"Sitemap lists {sitemap_count} pages" if sitemap_count is not None
            else "Sitemap found" if sitemap_used else "No sitemap found"
        ))

        # Which pages to read: a spread of the site, home first.
        step("crawl", "running", "Reading your pages")
        home_page = page_facts(final, home.text, status=home.status_code, response_ms=home_ms, site=final_root)
        bodies = [home.text]
        candidates: list[str] = []
        seen = {final.rstrip("/"), final_root}
        home_links = [hp.absolute(h, base=final + "/") for h in hp.parse(final, home.text).links]
        sitemap_spread = sitemap_pages[:: max(1, len(sitemap_pages) // 20)] if sitemap_pages else []
        for link in home_links + sitemap_spread:
            # Script templates leave href-looking text that is not an address.
            if not link or not hp.same_site(link, final_root) or re.search(r"[\s{}<>$`|]", link):
                continue
            clean = link.split("?", 1)[0].rstrip("/")
            path = urlsplit(clean).path or "/"
            if clean in seen or re.search(r"\.(pdf|jpe?g|png|gif|webp|svg|zip|mp4|xml|css|js)$", path, re.I):
                continue
            if not hp.is_allowed(path, disallow):
                continue
            seen.add(clean)
            candidates.append(clean)

        def read(link: str) -> tuple[PageFacts, str] | None:
            try:
                response, ms = _fetch(link)
            except Exception:  # noqa: BLE001
                return None
            if "html" not in response.headers.get("content-type", "html"):
                return None
            return page_facts(link, response.text, status=response.status_code, response_ms=ms, site=final_root), response.text

        pages = [home_page]
        with ThreadPoolExecutor(max_workers=6) as pool:
            for result in pool.map(read, candidates[: MAX_PAGES - 1]):
                if result is not None:
                    pages.append(result[0])
                    bodies.append(result[1])
        step("crawl", "done", f"Read {len(pages)} pages")

        step("links", "running", "Checking links")
        to_check = [c for c in candidates[MAX_PAGES - 1 :] if c not in {p.url for p in pages}][:MAX_LINK_CHECKS]
        broken = [{"url": p.url, "status": p.status} for p in pages if p.status >= 400]

        def probe(link: str) -> dict[str, Any] | None:
            try:
                response, _ = _fetch(link, max_bytes=50_000, timeout=10.0)
            except Exception:  # noqa: BLE001
                return {"url": link, "status": 0}
            return {"url": link, "status": response.status_code} if response.status_code >= 400 else None

        with ThreadPoolExecutor(max_workers=8) as pool:
            broken.extend(r for r in pool.map(probe, to_check) if r)
        step("links", "done", f"{len(broken)} broken of {len(pages) + len(to_check)} checked")

        social: dict[str, str] = {}
        phones: list[str] = []
        emails: list[str] = []
        for body in bodies:
            for href in hp.parse("", body).links:
                low = href.lower()
                if low.startswith("tel:"):
                    number = re.sub(r"[^\d+]", "", href[4:])
                    if number and number not in phones:
                        phones.append(number)
                elif low.startswith("mailto:"):
                    address = href[7:].split("?")[0].strip()
                    if address and address not in emails:
                        emails.append(address)
                else:
                    name = _platform_of(href)
                    if name and name not in social and not hp.same_site(href, final_root):
                        social[name] = href[:300]
        if not emails:
            emails = list(dict.fromkeys(e for e in _EMAIL_TEXT.findall(home_page.text) if not e.endswith((".png", ".jpg"))))[:3]

        trackers = [name for name, pattern in TRACKERS.items() if any(pattern.search(body) for body in bodies)]
        platform = next((name for name, pattern in _GENERATORS if pattern.search(home.text)), "")

        step("speed", "running", "Measuring speed with Google PageSpeed (up to a minute)")
        try:
            speed = speed_future.result(timeout=100)
        except Exception as exc:  # noqa: BLE001
            speed = {"measured": False, "reason": f"PageSpeed Insights did not finish ({type(exc).__name__})."}
        step("speed", "done" if speed.get("measured") else "skipped", (
            f"Mobile performance {speed['scores']['performance']}/100" if speed.get("measured") and speed["scores"].get("performance") is not None
            else speed.get("reason", "Not measured")
        ))

    step("listing", "running", "Checking your business listing")
    business = business_listing(business_url, social)
    step("listing", "done" if business.get("provided") else "skipped", (
        f"{business.get('platform')} link checked" if business.get("provided") else "No business link given"
    ))

    facts = SiteFacts(
        input_url=website,
        final_url=final,
        https=final.startswith("https://"),
        http_redirects_to_https=http_upgrade,
        platform=platform,
        robots_txt=bool(robots_body),
        robots_blocks_all="/" in disallow,
        sitemap_url=sitemap_used,
        sitemap_url_count=sitemap_count,
        llms_txt=llms,
        favicon=bool(_ICON.search(home.text)),
        trackers=trackers,
        social_links=social,
        phones=phones[:5],
        emails=emails[:5],
        local_business_schema=_local_business(bodies),
        pages=pages,
        links_checked=len(pages) + len(to_check),
        broken_links=broken[:20],
        pagespeed=speed,
        business=business,
        duration_s=round(time.perf_counter() - started, 1),
    )
    return facts, pages

