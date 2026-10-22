"""Turn measured facts into checks and scores.

Deterministic on purpose: the same site gives the same scores, every finding
quotes its evidence, and nothing here comes from the model. Each check names
the agent that works on that kind of problem, so the report can show what the
platform would do about it — using the agent's own plain-language summary,
never a claim written here.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass
from typing import Any

from app.services.demo_analysis.collect import PageFacts, SiteFacts

PASS, WARN, FAIL, INFO = "pass", "warn", "fail", "info"

#: Category key → (label, what it covers).
CATEGORIES: dict[str, tuple[str, str]] = {
    "technical": ("Technical health", "Can search engines reach, read and trust the site?"),
    "on_page": ("On-page SEO", "Titles, descriptions, headings and images on the pages read."),
    "content": ("Content", "Is there enough useful text for people and search engines?"),
    "performance": ("Speed & experience", "Google's mobile measurements of load speed and stability."),
    "ai_search": ("AI search readiness", "Can ChatGPT, Gemini and Perplexity understand and quote the site?"),
    "local": ("Local presence", "Can nearby customers find, trust and contact the business?"),
    "tracking": ("Tracking & conversion", "Can visits be measured and turned into leads or sales?"),
    "social": ("Social & brand", "How the site presents itself when shared and where it links out."),
}

_WEIGHT = {"high": 3, "medium": 2, "low": 1}


@dataclass(slots=True)
class Check:
    id: str
    category: str
    title: str
    status: str
    evidence: str
    why: str
    fix: str
    priority: str = "medium"
    effort: str = "quick"
    agent: str = ""
    pages: list[str] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v not in (None, "")}


def _short(url: str) -> str:
    return url.split("://", 1)[-1]


def _list(urls: list[str], limit: int = 3) -> str:
    shown = ", ".join(_short(u) for u in urls[:limit])
    return shown + (f" and {len(urls) - limit} more" if len(urls) > limit else "")


def build(facts: SiteFacts, pages: list[PageFacts]) -> list[Check]:
    checks: list[Check] = []
    add = checks.append
    n = len(pages)
    ok_pages = [p for p in pages if p.status < 400]

    # ── Technical ───────────────────────────────────────────────────────
    add(Check(
        "https", "technical", "Secure connection (HTTPS)",
        PASS if facts.https else FAIL,
        "The site loads over HTTPS." if facts.https else "The site loads over plain HTTP.",
        "Browsers mark HTTP sites as “Not secure” and Google ranks secure pages first.",
        "Install an SSL certificate (most hosts offer one free) and redirect every HTTP address to HTTPS.",
        "high", "quick",
    ))
    if facts.https and facts.http_redirects_to_https is False:
        add(Check(
            "http_redirect", "technical", "HTTP redirects to HTTPS", WARN,
            "The http:// address does not redirect to the secure version.",
            "Two versions of every page split ranking signals and can show visitors the insecure one.",
            "Add a permanent (301) redirect from http:// to https:// at your host or CDN.",
            "medium", "quick",
        ))
    add(Check(
        "robots", "technical", "robots.txt",
        FAIL if facts.robots_blocks_all else PASS if facts.robots_txt else WARN,
        "robots.txt blocks the whole site (Disallow: /)." if facts.robots_blocks_all
        else "robots.txt is present." if facts.robots_txt else "No robots.txt file was found.",
        "robots.txt tells search engines what they may crawl; blocking everything removes the site from search.",
        "Remove “Disallow: /” for all crawlers." if facts.robots_blocks_all
        else "Keep it, and make sure it lists your sitemap." if facts.robots_txt
        else "Add a robots.txt at the site root that allows crawling and points to your sitemap.",
        "high" if facts.robots_blocks_all else "low", "quick",
    ))
    add(Check(
        "sitemap", "technical", "XML sitemap",
        PASS if facts.sitemap_url else FAIL,
        (f"Sitemap found at {_short(facts.sitemap_url)}"
         + (f" listing {facts.sitemap_url_count} pages." if facts.sitemap_url_count is not None else "."))
        if facts.sitemap_url else "No sitemap was found in robots.txt or at /sitemap.xml.",
        "A sitemap is how search engines discover every page, especially new ones.",
        "Keep it updated automatically and submit it in Google Search Console." if facts.sitemap_url
        else "Generate a sitemap (most CMSs have a setting or plugin) and submit it in Google Search Console.",
        "medium", "quick", "technical_seo_auditor",
    ))
    noindex = [p.url for p in ok_pages if p.noindex]
    add(Check(
        "noindex", "technical", "Pages hidden from search",
        FAIL if noindex else PASS,
        f"{len(noindex)} of {n} pages read tell search engines not to index them: {_list(noindex)}." if noindex
        else f"None of the {n} pages read are hidden from search.",
        "A “noindex” tag removes a page from Google entirely — fine for thank-you pages, damaging on real ones.",
        "Remove the noindex robots tag from pages that should appear in search.",
        "high", "quick", "technical_seo_auditor", noindex or None,
    ))
    broken = facts.broken_links
    add(Check(
        "broken_links", "technical", "Broken internal links",
        FAIL if len(broken) >= 3 else WARN if broken else PASS,
        f"{len(broken)} of {facts.links_checked} internal links checked do not work: "
        + _list([b["url"] for b in broken]) + "." if broken
        else f"All {facts.links_checked} internal links checked answered.",
        "Broken links waste visitors' clicks and the crawl budget search engines give the site.",
        "Fix or redirect each broken address to the closest working page.",
        "medium", "quick", "technical_seo_auditor", [b["url"] for b in broken] or None,
    ))
    csr = [p.url for p in ok_pages if p.client_side_rendered]
    if csr:
        add(Check(
            "csr", "technical", "Content built only by JavaScript", FAIL,
            f"{len(csr)} pages send almost no text until JavaScript runs: {_list(csr)}.",
            "Search engines and AI crawlers often read the HTML before scripts run, so they see an empty page.",
            "Enable server-side rendering or pre-rendering in your framework so the HTML contains the content.",
            "high", "project",
        ))
    no_canonical = [p.url for p in ok_pages if not p.canonical]
    add(Check(
        "canonical", "technical", "Canonical tags",
        PASS if not no_canonical else WARN if len(no_canonical) < n else FAIL,
        f"{len(no_canonical)} of {n} pages have no canonical tag: {_list(no_canonical)}." if no_canonical
        else f"All {n} pages declare a canonical address.",
        "Without a canonical, copies of a page (tracking parameters, www and non-www) compete with each other.",
        "Add <link rel=\"canonical\"> pointing at each page's preferred address.",
        "low", "quick", "technical_seo_auditor", no_canonical or None,
    ))
    no_viewport = [p.url for p in ok_pages if not p.viewport]
    add(Check(
        "viewport", "technical", "Mobile-friendly setup",
        FAIL if no_viewport else PASS,
        f"{len(no_viewport)} pages have no mobile viewport tag: {_list(no_viewport)}." if no_viewport
        else "Every page read declares a mobile viewport.",
        "Google ranks the mobile version of a site; without a viewport tag pages render as tiny desktop pages.",
        "Add <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\"> to every page.",
        "high", "quick",
    ))
    slow = [p for p in ok_pages if p.response_ms > 1500]
    avg_ms = round(sum(p.response_ms for p in ok_pages) / max(len(ok_pages), 1))
    add(Check(
        "server_time", "technical", "Server response time",
        FAIL if avg_ms > 2000 else WARN if avg_ms > 800 else PASS,
        f"Pages took {avg_ms} ms on average to arrive at our analysis server (distance to your host adds to this)"
        + (f"; {len(slow)} took over 1.5 s." if slow else "."),
        "A slow server delays everything else on the page, for every visitor.",
        "Use page caching or a CDN, and check your hosting plan's capacity.",
        "medium", "medium",
    ))

    # ── On-page ─────────────────────────────────────────────────────────
    no_title = [p.url for p in ok_pages if not p.title]
    bad_title = [p.url for p in ok_pages if p.title and not 20 <= len(p.title) <= 65]
    add(Check(
        "titles", "on_page", "Page titles",
        FAIL if no_title else WARN if bad_title else PASS,
        (f"{len(no_title)} pages have no title: {_list(no_title)}. " if no_title else "")
        + (f"{len(bad_title)} titles are shorter than 20 or longer than 65 characters." if bad_title else "")
        or f"All {n} titles are present and a good length.",
        "The title is the blue headline in Google results — the single biggest on-page ranking and click factor.",
        "Write a unique 50–60 character title per page, leading with what the page offers and the place if local.",
        "high", "quick", "technical_seo_auditor", (no_title + bad_title) or None,
    ))
    titles = Counter(p.title.strip().lower() for p in ok_pages if p.title)
    dup_titles = [p.url for p in ok_pages if p.title and titles[p.title.strip().lower()] > 1]
    if dup_titles:
        add(Check(
            "dup_titles", "on_page", "Duplicate titles", WARN,
            f"{len(dup_titles)} pages share a title with another page: {_list(dup_titles)}.",
            "Pages with the same title compete with each other and Google may pick the wrong one.",
            "Give every page its own title that says what is different about it.",
            "medium", "quick", "technical_seo_auditor", dup_titles,
        ))
    no_desc = [p.url for p in ok_pages if not p.meta_description]
    bad_desc = [p.url for p in ok_pages if p.meta_description and not 70 <= len(p.meta_description) <= 165]
    add(Check(
        "descriptions", "on_page", "Meta descriptions",
        FAIL if len(no_desc) > n / 2 else WARN if no_desc or bad_desc else PASS,
        (f"{len(no_desc)} of {n} pages have no meta description. " if no_desc else "")
        + (f"{len(bad_desc)} are shorter than 70 or longer than 165 characters." if bad_desc else "")
        or f"All {n} pages have a meta description of a good length.",
        "The description is the grey text under the title in Google; a good one raises click-through.",
        "Write a 140–160 character description per page with the benefit and a call to action.",
        "medium", "quick", "technical_seo_auditor", (no_desc + bad_desc) or None,
    ))
    no_h1 = [p.url for p in ok_pages if p.h1_count == 0]
    many_h1 = [p.url for p in ok_pages if p.h1_count > 1]
    add(Check(
        "h1", "on_page", "Main heading (H1)",
        FAIL if no_h1 else WARN if many_h1 else PASS,
        (f"{len(no_h1)} pages have no H1: {_list(no_h1)}. " if no_h1 else "")
        + (f"{len(many_h1)} pages have more than one H1." if many_h1 else "")
        or "Every page read has exactly one H1.",
        "The H1 tells readers and search engines what the page is about.",
        "Give each page one H1 that matches the search it should rank for.",
        "medium", "quick", "technical_seo_auditor", (no_h1 + many_h1) or None,
    ))
    images = sum(p.images for p in ok_pages)
    missing_alt = sum(p.images_without_alt for p in ok_pages)
    add(Check(
        "alt", "on_page", "Image descriptions (alt text)",
        PASS if not missing_alt else WARN if missing_alt / max(images, 1) < 0.5 else FAIL,
        f"{missing_alt} of {images} images have no alt text." if images else "No images on the pages read.",
        "Alt text makes images accessible to screen readers and findable in Google Images.",
        "Describe each meaningful image in a short phrase; leave purely decorative images with empty alt.",
        "low", "quick",
    ))

    # ── Content ─────────────────────────────────────────────────────────
    thin = [p.url for p in ok_pages if not p.client_side_rendered and p.word_count < 300]
    add(Check(
        "thin", "content", "Thin pages",
        FAIL if len(thin) > n / 2 else WARN if thin else PASS,
        f"{len(thin)} of {n} pages have under 300 words: {_list(thin)}." if thin
        else "Every page read has at least 300 words.",
        "Short pages rarely rank: they cannot answer the questions people search for.",
        "Expand key pages with what customers ask — prices, process, proof, FAQs — in your own words.",
        "medium", "medium", "on_page_seo_sync", thin or None,
    ))
    blogish = facts.sitemap_url_count or 0
    has_blog = any(seg in p.url.lower() for p in pages for seg in ("/blog", "/news", "/articles", "/insights", "/resources", "/guides"))
    add(Check(
        "publishing", "content", "Regular articles",
        PASS if has_blog else WARN,
        "The site has a blog or articles section." if has_blog
        else "No blog, news or guides section was found on the pages read.",
        "Articles answering customer questions are how most sites win searches beyond their own name.",
        "Publish one or two helpful articles a month on the questions customers ask before buying.",
        "medium", "medium", "seo_article_writer",
    ))
    if blogish and blogish < 10:
        add(Check(
            "site_size", "content", "Size of the site", WARN,
            f"The sitemap lists only {blogish} pages.",
            "Few pages means few chances to appear for the many different searches customers make.",
            "Add a page per service or product and per area served, plus articles.",
            "medium", "project", "seo_article_writer",
        ))

    # ── Performance ─────────────────────────────────────────────────────
    speed = facts.pagespeed or {}
    if speed.get("measured"):
        perf = speed["scores"].get("performance")
        lcp = (speed.get("lab") or {}).get("lcp") or {}
        cls = (speed.get("lab") or {}).get("cls") or {}
        tbt = (speed.get("lab") or {}).get("tbt") or {}
        add(Check(
            "psi_performance", "performance", "Mobile performance score",
            PASS if (perf or 0) >= 90 else WARN if (perf or 0) >= 50 else FAIL,
            f"Google PageSpeed scores the home page {perf}/100 on mobile.",
            "Slow mobile pages lose visitors before they load — and speed is a Google ranking signal.",
            "Start with the biggest savings Google lists: "
            + "; ".join(f"{o['title']} (~{o['saving_ms'] / 1000:.1f} s)" for o in speed.get("opportunities", [])[:3])
            if speed.get("opportunities") else "Compress images, defer non-essential scripts and enable caching.",
            "high" if (perf or 0) < 50 else "medium", "medium", "technical_seo_auditor",
        ))
        if lcp:
            seconds = lcp["value"] / 1000
            add(Check(
                "lcp", "performance", "Largest Contentful Paint",
                PASS if seconds <= 2.5 else WARN if seconds <= 4 else FAIL,
                "The main content appears after " + (lcp.get("display") or f"{seconds:.1f} s") + " on a mobile test (good is under 2.5 s).",
                "This is how long visitors wait to see the page — one of Google's Core Web Vitals.",
                "Compress and correctly size the hero image, preload it, and reduce render-blocking CSS and fonts.",
                "medium", "medium",
            ))
        if cls:
            add(Check(
                "cls", "performance", "Layout stability (CLS)",
                PASS if cls["value"] <= 0.1 else WARN if cls["value"] <= 0.25 else FAIL,
                f"Layout shift measured {cls['value']:.2f} (good is 0.1 or less).",
                "Content jumping while loading causes mis-taps and frustration — another Core Web Vital.",
                "Set width and height on images and embeds, and reserve space for banners and ads.",
                "low", "quick",
            ))
        if tbt:
            add(Check(
                "tbt", "performance", "Responsiveness (Total Blocking Time)",
                PASS if tbt["value"] <= 200 else WARN if tbt["value"] <= 600 else FAIL,
                "Scripts blocked the page for " + (tbt.get("display") or f"{tbt['value']:.0f} ms") + " (good is under 200 ms).",
                "While scripts block, taps and clicks do nothing.",
                "Remove unused scripts and plugins, and load chat widgets and trackers after the page.",
                "low", "medium",
            ))
        a11y = speed["scores"].get("accessibility")
        if a11y is not None:
            add(Check(
                "accessibility", "performance", "Accessibility",
                PASS if a11y >= 90 else WARN if a11y >= 70 else FAIL,
                f"Google's accessibility audit scores the home page {a11y}/100.",
                "Accessible sites work for everyone, including the 1 in 6 people with a disability, and avoid legal risk.",
                "Fix colour contrast, label form fields and buttons, and add alt text.",
                "low", "medium",
            ))
    else:
        add(Check(
            "psi_unmeasured", "performance", "Speed not measured", INFO,
            speed.get("reason", "PageSpeed Insights did not return a result."),
            "Speed could not be measured this time, so it is not scored.",
            "Run the analysis again later, or test at pagespeed.web.dev.",
            "low", "quick",
        ))

    # ── AI search ───────────────────────────────────────────────────────
    schema = sorted({t for p in ok_pages for t in p.schema_types})
    add(Check(
        "schema", "ai_search", "Structured data (schema)",
        PASS if len(schema) >= 2 else WARN if schema else FAIL,
        f"Schema types found: {', '.join(schema[:8])}." if schema else "No structured data was found on the pages read.",
        "Schema tells Google and AI assistants exactly what the business, products and FAQs are, and enables rich results.",
        "Add Organization or LocalBusiness schema site-wide, plus Product, Service and FAQPage where they fit.",
        "high", "medium", "knowledge_graph_schema",
    ))
    faq_schema = "FAQPage" in schema
    question_headings = sum(1 for p in ok_pages for h in p.h2 if h.strip().endswith("?"))
    add(Check(
        "qa", "ai_search", "Question-and-answer content",
        PASS if faq_schema or question_headings >= 3 else WARN if question_headings else FAIL,
        ("FAQ schema is present. " if faq_schema else "")
        + f"{question_headings} section headings on the pages read are written as questions.",
        "AI assistants answer questions; pages that state questions and direct answers are the ones they quote.",
        "Add a short FAQ to key pages with the questions customers really ask, each answered in 2–3 sentences.",
        "high", "quick", "aeo_qa_injector",
    ))
    add(Check(
        "llms_txt", "ai_search", "llms.txt guide for AI crawlers",
        PASS if facts.llms_txt else INFO,
        "An /llms.txt file is present." if facts.llms_txt else "No /llms.txt file was found.",
        "llms.txt is an emerging, optional convention that points AI tools at your most important pages. Not yet a ranking factor.",
        "Optionally publish /llms.txt listing your key pages with one-line descriptions.",
        "low", "quick",
    ))
    org_entity = facts.local_business_schema is not None or any(t in schema for t in ("Organization", "LocalBusiness"))
    add(Check(
        "entity", "ai_search", "Business identity for AI",
        PASS if org_entity and facts.social_links else WARN if org_entity or facts.social_links else FAIL,
        ("Organization/LocalBusiness schema is present. " if org_entity else "No Organization or LocalBusiness schema. ")
        + (f"The site links to {len(facts.social_links)} profiles elsewhere." if facts.social_links else "The site links to no profiles elsewhere."),
        "AI assistants trust businesses they can confirm across sources — schema plus consistent profiles.",
        "Add Organization schema with sameAs links to your Google, LinkedIn and social profiles.",
        "medium", "quick", "knowledge_graph_schema",
    ))

    # ── Local ───────────────────────────────────────────────────────────
    lb = facts.local_business_schema
    contact = bool(facts.phones or facts.emails)
    add(Check(
        "contact", "local", "Contact details on the site",
        PASS if facts.phones and facts.emails else WARN if contact else FAIL,
        (f"Phone links: {', '.join(facts.phones[:2])}. " if facts.phones else "No clickable phone number. ")
        + (f"Email: {', '.join(facts.emails[:2])}." if facts.emails else "No email address found."),
        "Visitors ready to buy look for a phone number or email first; on mobile, one tap should call.",
        "Show a clickable phone number (tel: link) and email in the header or footer of every page.",
        "high" if not contact else "low", "quick",
    ))
    add(Check(
        "local_schema", "local", "Business details in schema",
        PASS if lb and lb.get("address") and lb.get("telephone") else WARN if lb else FAIL,
        (f"{lb.get('type') or 'Business'} schema: name “{lb.get('name')}”"
         + (f", phone {lb['telephone']}" if lb.get("telephone") else ", no phone")
         + (f", address {lb['address']}" if lb.get("address") else ", no address")
         + (", opening hours" if lb.get("has_opening_hours") else ", no opening hours") + ".") if lb
        else "No LocalBusiness or Organization schema with business details was found.",
        "Name, address and phone in schema help Google match the site to the business's Maps listing.",
        "Add LocalBusiness schema with name, address, phone, opening hours and your Google profile in sameAs.",
        "medium", "quick", "knowledge_graph_schema",
    ))
    business = facts.business or {}
    if business.get("provided"):
        linked = business.get("linked_from_website")
        add(Check(
            "listing_link", "local", f"Website links to your {business.get('platform', 'listing')}",
            PASS if linked else WARN if linked is False else INFO,
            "The website links to the listing's platform." if linked
            else f"No link from the website to {business.get('platform')} was found." if linked is False
            else "The business link is not a platform the site can link to.",
            "Linking site and listing both ways helps customers and search engines connect them.",
            "Link to your Google Business Profile (and review sites) from the footer or contact page.",
            "low", "quick", "local_search_tracker",
        ))
        add(Check(
            "local_tracking", "local", "Google Maps ranking",
            INFO,
            "Map-pack positions need a Google Maps data source and are not measured in this free analysis.",
            "Most local customers choose from the three businesses Google Maps shows first.",
            "Track your position for searches like “salon near me” weekly, and reply to every review.",
            "medium", "quick", "local_search_tracker",
        ))
    else:
        add(Check(
            "no_listing", "local", "Business listing", INFO,
            "No business listing link was given, so it was not checked.",
            "For businesses serving local customers, the Google Business Profile often brings more calls than the website.",
            "Claim and complete your Google Business Profile, then link it from the site.",
            "low", "quick", "local_search_tracker",
        ))

    # ── Tracking & conversion ───────────────────────────────────────────
    analytics = [t for t in facts.trackers if t in ("Google Analytics 4", "Google Tag Manager")]
    add(Check(
        "analytics", "tracking", "Website analytics",
        PASS if analytics else WARN,
        (f"Detected: {', '.join(facts.trackers)}. " if facts.trackers else "")
        + ("" if analytics else "No Google Analytics 4 or Tag Manager tag was found in the page HTML"
           + (f" ({facts.platform} has built-in statistics, which cannot be seen from outside)" if facts.platform in ("Squarespace", "Wix", "Shopify") else "")
           + "."),
        "Without analytics nobody can tell which pages, searches or ads bring customers.",
        "Install Google Analytics 4 (directly or via Google Tag Manager) and mark leads and sales as conversions.",
        "high", "quick",
    ))
    ad_pixels = [t for t in facts.trackers if t in ("Google Ads tag", "Meta Pixel", "LinkedIn Insight", "TikTok Pixel", "Microsoft Ads (UET)")]
    add(Check(
        "pixels", "tracking", "Advertising pixels",
        PASS if ad_pixels else WARN,
        f"Detected: {', '.join(ad_pixels)}." if ad_pixels else "No Google Ads, Meta, LinkedIn, TikTok or Microsoft Ads tag was detected.",
        "Ad platforms need their tag to measure conversions and to retarget visitors; without it ads run blind.",
        "Install the tag for each ad platform you use (Tag Manager makes this one place).",
        "medium", "quick", "first_party_audience_modeler",
    ))
    forms = [p.url for p in ok_pages if p.has_form]
    add(Check(
        "lead_capture", "tracking", "Lead capture",
        PASS if forms else WARN,
        f"{len(forms)} of {n} pages have a form." if forms else "None of the pages read has a form.",
        "A visitor who is not ready to call today may still leave an email — if asked.",
        "Add an enquiry or newsletter form on key pages and connect it to your email platform.",
        "medium", "quick", "email_marketer",
    ))

    # ── Social & brand ─────────────────────────────────────────────────
    no_og = [p.url for p in ok_pages if not p.open_graph]
    add(Check(
        "open_graph", "social", "Link previews (Open Graph)",
        PASS if not no_og else WARN if len(no_og) < n else FAIL,
        f"{len(no_og)} of {n} pages have no Open Graph tags." if no_og else "Every page read has Open Graph tags.",
        "Open Graph controls the image and headline shown when a page is shared on WhatsApp, LinkedIn or Facebook.",
        "Add og:title, og:description and og:image to every page.",
        "low", "quick", "social_media_manager",
    ))
    add(Check(
        "social_profiles", "social", "Social profiles linked",
        PASS if len(facts.social_links) >= 2 else WARN,
        f"Links to: {', '.join(facts.social_links)}." if facts.social_links else "The site links to no social profiles.",
        "Active profiles build trust and give every post a path back to the site.",
        "Link your active profiles from the footer and post regularly.",
        "low", "quick", "social_media_manager",
    ))
    add(Check(
        "favicon", "social", "Site icon (favicon)",
        PASS if facts.favicon else WARN,
        "A favicon is declared." if facts.favicon else "No favicon was declared on the home page.",
        "Google shows the favicon next to mobile results; a missing one looks unfinished.",
        "Add a square icon (at least 48×48) and declare it with <link rel=\"icon\">.",
        "low", "quick",
    ))
    return checks


def scores(checks: list[Check]) -> dict[str, dict[str, Any]]:
    """Per-category 0-100 scores. Info checks are not scored."""
    out: dict[str, dict[str, Any]] = {}
    for key, (label, about) in CATEGORIES.items():
        scored = [c for c in checks if c.category == key and c.status != INFO]
        if not scored:
            out[key] = {"label": label, "about": about, "score": None, "passed": 0, "total": 0}
            continue
        earned = total = 0.0
        for c in scored:
            weight = _WEIGHT.get(c.priority, 1)
            total += weight
            earned += weight * {PASS: 1.0, WARN: 0.5, FAIL: 0.0}[c.status]
        out[key] = {
            "label": label,
            "about": about,
            "score": round(100 * earned / total),
            "passed": sum(1 for c in scored if c.status == PASS),
            "total": len(scored),
        }
    return out


def overall(category_scores: dict[str, dict[str, Any]]) -> int | None:
    values = [c["score"] for c in category_scores.values() if c["score"] is not None]
    return round(sum(values) / len(values)) if values else None


def grade(score: int | None) -> str:
    if score is None:
        return "–"
    return "A" if score >= 90 else "B" if score >= 75 else "C" if score >= 60 else "D" if score >= 40 else "E"
