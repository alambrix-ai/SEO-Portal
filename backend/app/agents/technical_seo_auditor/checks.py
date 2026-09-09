"""The technical SEO checks, as pure functions over a set of pages.

No database, no network, no model. Everything here takes the crawled pages and
returns findings, which is what makes the rules testable one at a time — and
these are rules a customer will argue with, so being able to point at the
exact test for "why is this page thin" matters.

Three principles the checks follow:

**Every finding says what to do.** "Missing schema" is an observation; "add
Product markup so this page is eligible for rich results" is something a
marketing lead can act on or delegate. A list of observations is a report; a
list of actions is a service.

**Nothing is invented.** A check either has the data it needs or does not run.
Core Web Vitals cannot be computed from page HTML — they are field
measurements — so the LCP check produces nothing at all unless a page-speed
source is connected, rather than estimating from word count and calling it a
score.

**Severity means expected impact, not tidiness.** A broken internal link on a
money page and a missing alt attribute are both real, and treating them as
equally urgent is how audit tools train people to ignore them.
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from enum import StrEnum


class IssueKind(StrEnum):
    """The checks this module performs.

    Stored on the row, so renaming one is a migration — they are the stable
    identity of a finding across runs.
    """

    THIN_CONTENT = "thin_content"
    MISSING_SCHEMA = "missing_schema"
    BROKEN_INTERNAL_LINK = "broken_internal_link"
    ORPHAN_PAGE = "orphan_page"
    MISSING_META_DESCRIPTION = "missing_meta_description"
    MISSING_H1 = "missing_h1"
    MULTIPLE_H1 = "multiple_h1"
    DUPLICATE_TITLE = "duplicate_title"
    TITLE_TOO_LONG = "title_too_long"
    IMAGE_MISSING_ALT = "image_missing_alt"
    MISSING_CANONICAL = "missing_canonical"
    SLOW_LCP_MOBILE = "slow_lcp_mobile"
    POOR_CLS_MOBILE = "poor_cls_mobile"
    SLOW_INP_MOBILE = "slow_inp_mobile"


class Severity(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


#: Below this, a page rarely has enough substance to rank for anything
#: competitive. Google has no published minimum — this is a working threshold,
#: which is why it is a setting rather than a constant.
DEFAULT_THIN_WORDS = 300
#: Field thresholds published as the Core Web Vitals "good" boundaries.
LCP_GOOD_MS = 2_500
LCP_POOR_MS = 4_000
CLS_GOOD = 0.1
INP_GOOD_MS = 200
#: Titles are truncated in results beyond roughly this width.
TITLE_MAX_CHARS = 60

_HTML_LINK = re.compile(r'<a\b[^>]*href=["\']([^"\']+)["\']', re.I)
_MD_LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)")
_HTML_IMG = re.compile(r"<img\b[^>]*>", re.I)
_IMG_ALT = re.compile(r'\balt\s*=\s*["\'][^"\']', re.I)
_MD_IMG = re.compile(r"!\[([^\]]*)\]\([^)]*\)")
_H1_HTML = re.compile(r"<h1\b[^>]*>(.*?)</h1>", re.I | re.DOTALL)
_H1_MD = re.compile(r"^#\s+\S", re.M)
_META_DESC = re.compile(
    r'<meta\b[^>]*name=["\']description["\'][^>]*content=["\']([^"\']*)', re.I
)
_CANONICAL = re.compile(r'<link\b[^>]*rel=["\']canonical["\']', re.I)
_CODE_FENCE = re.compile(r"```.*?```", re.DOTALL)


@dataclass(slots=True)
class PageSnapshot:
    """One crawled page, as the checks see it."""

    id: str
    url: str
    title: str
    body: str
    word_count: int
    schema_types: list[str] = field(default_factory=list)
    #: Set only where a page-speed source is connected. None means unmeasured,
    #: which is different from good and must never be reported as good.
    lcp_ms: int | None = None
    cls: float | None = None
    inp_ms: int | None = None
    #: Frontmatter keys, for repository-backed sites where the description and
    #: canonical live there rather than in a meta tag.
    frontmatter_keys: set[str] = field(default_factory=set)
    #: True for a page the operator has told the platform to leave alone.
    excluded: bool = False


@dataclass(slots=True)
class Finding:
    """One issue on one page, with what to do about it."""

    kind: IssueKind
    severity: Severity
    page_id: str
    url: str
    summary: str
    #: What to do. The difference between a report and a service.
    recommendation: str
    #: Non-sensitive specifics — the broken target, the word count, the LCP.
    evidence: dict = field(default_factory=dict)


# ── URLs ───────────────────────────────────────────────────────────────────
def normalise_url(raw: str) -> str:
    """The comparable form of an internal path.

    Trailing slashes, fragments and query strings all name the same document
    for the purpose of "does this page exist", and treating them as different
    is how a link checker generates false positives nobody trusts.
    """
    value = raw.strip()
    for cut in ("#", "?"):
        if cut in value:
            value = value.split(cut, 1)[0]
    if len(value) > 1:
        value = value.rstrip("/")
    return value or "/"


def is_internal(href: str, *, domain: str = "") -> bool:
    """Whether a link points inside this site.

    Absolute links to the site's own domain count — plenty of CMS content is
    written with the full URL, and calling those external would hide real
    broken links.
    """
    value = href.strip().lower()
    if not value or value.startswith(("#", "mailto:", "tel:", "javascript:", "data:")):
        return False
    if value.startswith("/"):
        return True
    if value.startswith(("http://", "https://")):
        if not domain:
            return False
        host = value.split("//", 1)[1].split("/", 1)[0]
        bare = domain.lower().removeprefix("www.")
        return host.removeprefix("www.") == bare
    # A bare relative link such as "about" or "./about".
    return not value.startswith("//")


def links_in(body: str, *, domain: str = "") -> list[str]:
    """Internal link targets in a page body, HTML and Markdown alike.

    Code fences are stripped first: a documentation page showing an example
    URL is not linking to it, and auditing your own snippets is noise.
    """
    text = _CODE_FENCE.sub(" ", body)
    found: list[str] = []
    for pattern in (_HTML_LINK, _MD_LINK):
        for href in pattern.findall(text):
            if is_internal(href, domain=domain):
                found.append(normalise_url(href))
    return found


# ── Individual checks ──────────────────────────────────────────────────────
def check_thin(page: PageSnapshot, *, minimum: int) -> Finding | None:
    if page.word_count >= minimum:
        return None
    return Finding(
        kind=IssueKind.THIN_CONTENT,
        # A page with almost nothing on it is a ranking problem; one just
        # under the line is a priority call, not an emergency.
        severity=Severity.HIGH if page.word_count < minimum // 2 else Severity.MEDIUM,
        page_id=page.id,
        url=page.url,
        summary=f"{page.word_count} words — under the {minimum}-word threshold",
        recommendation=(
            f"Expand this page to at least {minimum} words of genuinely useful "
            "copy, or merge it into a stronger page and redirect. Padding it "
            "will not help: the threshold is a symptom, not the goal."
        ),
        evidence={"word_count": page.word_count, "threshold": minimum},
    )


def check_missing_schema(page: PageSnapshot) -> Finding | None:
    if page.schema_types:
        return None
    return Finding(
        kind=IssueKind.MISSING_SCHEMA,
        severity=Severity.MEDIUM,
        page_id=page.id,
        url=page.url,
        summary="No structured data on the page",
        recommendation=(
            "Add JSON-LD describing what this page is — Product, FAQPage, "
            "LocalBusiness or Article. Without it the page cannot appear as a "
            "rich result. The Knowledge Graph & Schema agent can propose and "
            "inject it."
        ),
    )


def check_broken_links(
    page: PageSnapshot, *, known_urls: set[str], domain: str = ""
) -> list[Finding]:
    """Internal links whose target is not among the crawled pages.

    Reported per target rather than per link, because a navigation link
    repeated on forty pages is one broken URL to fix, not forty findings.
    """
    broken = sorted(
        {target for target in links_in(page.body, domain=domain) if target not in known_urls}
    )
    return [
        Finding(
            kind=IssueKind.BROKEN_INTERNAL_LINK,
            # A link into nothing wastes the crawl and the visitor.
            severity=Severity.HIGH,
            page_id=page.id,
            url=page.url,
            summary=f"Links to {target}, which is not a known page",
            recommendation=(
                f"Point the link at a live URL or remove it. If {target} should "
                "exist, create it — an internal link is a promise to a reader "
                "and to a crawler."
            ),
            evidence={"target": target},
        )
        for target in broken
    ]


def check_orphans(pages: list[PageSnapshot], *, domain: str = "") -> list[Finding]:
    """Pages nothing links to.

    Computed across the whole set, because "orphan" is a property of the site
    rather than of the page. The home page is excluded: it is reached
    directly, and flagging it every run would be a permanent false positive.
    """
    inbound: dict[str, int] = defaultdict(int)
    for page in pages:
        source = normalise_url(page.url)
        for target in set(links_in(page.body, domain=domain)):
            if target != source:
                inbound[target] += 1

    findings: list[Finding] = []
    for page in pages:
        url = normalise_url(page.url)
        if url == "/" or inbound.get(url):
            continue
        findings.append(
            Finding(
                kind=IssueKind.ORPHAN_PAGE,
                severity=Severity.HIGH,
                page_id=page.id,
                url=page.url,
                summary="No internal links point to this page",
                recommendation=(
                    "Link to it from a relevant page — a category listing, a "
                    "related article, or the navigation. A page with no "
                    "inbound links is crawled rarely, ranks poorly, and is "
                    "invisible to readers browsing the site."
                ),
            )
        )
    return findings


def check_head_tags(page: PageSnapshot) -> list[Finding]:
    """Meta description, canonical, and the H1.

    Skipped on pages with frontmatter that already declares the equivalent
    key: on a repository-backed site the template writes the tag from the
    frontmatter, so the raw file has no meta tag and reporting one missing
    would be wrong.
    """
    findings: list[Finding] = []
    body = page.body

    has_description = bool(_META_DESC.search(body)) or bool(
        {"description", "summary", "excerpt", "seo_description"} & page.frontmatter_keys
    )
    if not has_description:
        findings.append(
            Finding(
                kind=IssueKind.MISSING_META_DESCRIPTION,
                severity=Severity.MEDIUM,
                page_id=page.id,
                url=page.url,
                summary="No meta description",
                recommendation=(
                    "Write a 140–160 character description that says what the "
                    "page offers. It is not a ranking factor, but it is the "
                    "sentence that decides whether anyone clicks."
                ),
            )
        )

    has_canonical = bool(_CANONICAL.search(body)) or bool(
        {"canonical", "canonical_url", "canonicalUrl"} & page.frontmatter_keys
    )
    if not has_canonical:
        findings.append(
            Finding(
                kind=IssueKind.MISSING_CANONICAL,
                severity=Severity.LOW,
                page_id=page.id,
                url=page.url,
                summary="No canonical URL declared",
                recommendation=(
                    "Declare a canonical URL so parameterised and duplicated "
                    "versions of this page consolidate their ranking signals "
                    "instead of competing."
                ),
            )
        )

    headings = len(_H1_HTML.findall(body)) + len(_H1_MD.findall(body))
    if headings == 0:
        findings.append(
            Finding(
                kind=IssueKind.MISSING_H1,
                severity=Severity.MEDIUM,
                page_id=page.id,
                url=page.url,
                summary="No H1 heading",
                recommendation=(
                    "Add one H1 stating the page's subject in the words a "
                    "reader would use. It is the strongest on-page signal of "
                    "what the page is about."
                ),
            )
        )
    elif headings > 1:
        findings.append(
            Finding(
                kind=IssueKind.MULTIPLE_H1,
                severity=Severity.LOW,
                page_id=page.id,
                url=page.url,
                summary=f"{headings} H1 headings",
                recommendation=(
                    "Keep one H1 and demote the rest to H2. Several competing "
                    "H1s dilute the page's stated subject."
                ),
                evidence={"count": headings},
            )
        )
    return findings


def check_images(page: PageSnapshot) -> Finding | None:
    """Images with no alt text.

    Both an accessibility failure and a lost signal, which is why it is worth
    reporting even though it is the least urgent thing here.
    """
    missing = sum(1 for tag in _HTML_IMG.findall(page.body) if not _IMG_ALT.search(tag))
    missing += sum(1 for alt in _MD_IMG.findall(page.body) if not alt.strip())
    if not missing:
        return None
    return Finding(
        kind=IssueKind.IMAGE_MISSING_ALT,
        severity=Severity.LOW,
        page_id=page.id,
        url=page.url,
        summary=f"{missing} image(s) without alt text",
        recommendation=(
            "Describe each image in alt text. Screen readers depend on it, and "
            "it is the only thing telling a crawler what the image shows."
        ),
        evidence={"count": missing},
    )


def check_title(page: PageSnapshot) -> Finding | None:
    if len(page.title) <= TITLE_MAX_CHARS:
        return None
    return Finding(
        kind=IssueKind.TITLE_TOO_LONG,
        severity=Severity.LOW,
        page_id=page.id,
        url=page.url,
        summary=f"Title is {len(page.title)} characters",
        recommendation=(
            f"Shorten it to about {TITLE_MAX_CHARS} characters so it is not "
            "truncated in results. Put the distinguishing words first."
        ),
        evidence={"length": len(page.title), "limit": TITLE_MAX_CHARS},
    )


def check_duplicate_titles(pages: list[PageSnapshot]) -> list[Finding]:
    """Pages sharing a title compete with each other for the same query."""
    counts = Counter(page.title.strip().lower() for page in pages if page.title.strip())
    duplicated = {title for title, count in counts.items() if count > 1}
    return [
        Finding(
            kind=IssueKind.DUPLICATE_TITLE,
            severity=Severity.MEDIUM,
            page_id=page.id,
            url=page.url,
            summary=f"Title is shared with {counts[page.title.strip().lower()] - 1} other page(s)",
            recommendation=(
                "Give each page a distinct title. Identical titles make the "
                "pages compete for the same query, and neither wins."
            ),
            evidence={"title": page.title, "pages_sharing": counts[page.title.strip().lower()]},
        )
        for page in pages
        if page.title.strip().lower() in duplicated
    ]


def check_vitals(page: PageSnapshot) -> list[Finding]:
    """Core Web Vitals, on mobile, from field data.

    Produces nothing when the measurements are absent. They cannot be derived
    from the page's HTML — they are what real devices on real networks
    recorded — so a page with no data is *unmeasured*, and reporting that as
    passing would be a fabrication about the customer's site speed.
    """
    findings: list[Finding] = []

    if page.lcp_ms is not None and page.lcp_ms > LCP_GOOD_MS:
        findings.append(
            Finding(
                kind=IssueKind.SLOW_LCP_MOBILE,
                severity=Severity.HIGH if page.lcp_ms > LCP_POOR_MS else Severity.MEDIUM,
                page_id=page.id,
                url=page.url,
                summary=f"Largest Contentful Paint is {page.lcp_ms / 1000:.1f}s on mobile",
                recommendation=(
                    "Find the element that paints last — usually a hero image "
                    "or a web font — and make it arrive sooner: compress and "
                    f"size the image, preload the font, and remove blocking "
                    f"scripts above the fold. The target is under "
                    f"{LCP_GOOD_MS / 1000:.1f}s."
                ),
                evidence={"lcp_ms": page.lcp_ms, "good_ms": LCP_GOOD_MS},
            )
        )

    if page.cls is not None and page.cls > CLS_GOOD:
        findings.append(
            Finding(
                kind=IssueKind.POOR_CLS_MOBILE,
                severity=Severity.MEDIUM,
                page_id=page.id,
                url=page.url,
                summary=f"Cumulative Layout Shift is {page.cls:.2f} on mobile",
                recommendation=(
                    "Reserve space for images, ads and embeds with explicit "
                    "width and height so content stops jumping as the page "
                    f"loads. The target is under {CLS_GOOD}."
                ),
                evidence={"cls": page.cls, "good": CLS_GOOD},
            )
        )

    if page.inp_ms is not None and page.inp_ms > INP_GOOD_MS:
        findings.append(
            Finding(
                kind=IssueKind.SLOW_INP_MOBILE,
                severity=Severity.MEDIUM,
                page_id=page.id,
                url=page.url,
                summary=f"Interaction to Next Paint is {page.inp_ms}ms on mobile",
                recommendation=(
                    "Break up long JavaScript tasks and defer what is not "
                    f"needed for the first interaction. The target is under "
                    f"{INP_GOOD_MS}ms."
                ),
                evidence={"inp_ms": page.inp_ms, "good_ms": INP_GOOD_MS},
            )
        )
    return findings


# ── The whole audit ────────────────────────────────────────────────────────
def audit(
    pages: list[PageSnapshot], *, domain: str = "", thin_words: int = DEFAULT_THIN_WORDS
) -> list[Finding]:
    """Every finding across the crawled set, worst first.

    Site-wide checks (orphans, duplicate titles) need the whole set, which is
    why the audit is one call over all pages rather than a loop of
    per-page audits.
    """
    live = [page for page in pages if not page.excluded]
    if not live:
        return []

    known = {normalise_url(page.url) for page in live}
    findings: list[Finding] = []

    for page in live:
        for single in (
            check_thin(page, minimum=thin_words),
            check_missing_schema(page),
            check_images(page),
            check_title(page),
        ):
            if single is not None:
                findings.append(single)
        findings.extend(check_broken_links(page, known_urls=known, domain=domain))
        findings.extend(check_head_tags(page))
        findings.extend(check_vitals(page))

    findings.extend(check_orphans(live, domain=domain))
    findings.extend(check_duplicate_titles(live))

    rank = {Severity.HIGH: 0, Severity.MEDIUM: 1, Severity.LOW: 2}
    findings.sort(key=lambda f: (rank[f.severity], f.kind.value, f.url))
    return findings
