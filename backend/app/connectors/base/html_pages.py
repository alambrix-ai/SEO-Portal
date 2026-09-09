"""Reading a page as a crawler sees it.

The repository connectors read source files, which works when the copy *is* a
file — Markdown with frontmatter, an HTML page. It does not work for a
component-based site, and that is not an edge case: on a Next or Nuxt or Vue
site a page file is usually a composition,

    export default function HomePage() {
      return (<><Header /><Hero /><Outcomes /><SiteFooter /></>)
    }

with no copy in it at all. The words live in a dozen component files, none of
which is "the page". Counting that file's words gives you the length of its
import block, and rewriting its body would replace working code with prose.

So for those sites the honest source of truth is the rendered page: the HTML a
crawler receives. That is what this module parses, and it works for every
stack — WordPress, Next, Rails, Laravel, a hand-written static site — because
it reads the output rather than the source.

Deliberately regex-based rather than pulling in a parser. The extraction here
is shallow by design (title, description, headings, visible text, JSON-LD,
links) and a real DOM parser would invite the temptation to do surgery on
somebody's live markup, which this platform does not do from the rendered
side — writes go through the CMS or the repository.
"""
from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlsplit

_SCRIPT_OR_STYLE = re.compile(
    r"<(script|style|noscript|template|svg)\b.*?</\1\s*>", re.DOTALL | re.I
)
_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_TAG = re.compile(r"<[^>]+>")
_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.DOTALL | re.I)
_H1 = re.compile(r"<h1\b[^>]*>(.*?)</h1>", re.DOTALL | re.I)
_META = re.compile(r"<meta\b([^>]*)>", re.I)
_ATTR = re.compile(r'([a-zA-Z:-]+)\s*=\s*"([^"]*)"|([a-zA-Z:-]+)\s*=\s*\'([^\']*)\'')
_CANONICAL = re.compile(r'<link\b[^>]*rel=["\']canonical["\'][^>]*>', re.I)
_HREF = re.compile(r'<a\b[^>]*href=["\']([^"\']+)["\']', re.I)
_LD_JSON = re.compile(
    r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', re.DOTALL | re.I
)
_IMG = re.compile(r"<img\b[^>]*>", re.I)
_SCRIPT_TAG = re.compile(r"<script\b", re.I)

#: Below this much visible text, with scripts present and a mount point in
#: the markup, a page is a shell rendered in the browser.
#:
#: Deliberately low. A forty-word page is *thin*, which is a different finding
#: with a different fix, and calling it client-side rendered would send
#: somebody to argue with their build pipeline about a copywriting problem.
_CSR_TEXT_FLOOR = 25
#: The mount points frameworks leave behind in an otherwise empty body.
_MOUNT_POINT = re.compile(
    r'<div[^>]*id=.(root|app|__next|__nuxt|svelte).[^>]*>', re.I
)


@dataclass(slots=True)
class RenderedPage:
    """A page as it arrived over HTTP."""

    url: str
    status: int = 200
    title: str = ""
    description: str = ""
    canonical: str = ""
    headings: list[str] = field(default_factory=list)
    text: str = ""
    word_count: int = 0
    schema_types: list[str] = field(default_factory=list)
    links: list[str] = field(default_factory=list)
    images_without_alt: int = 0
    #: True when the HTML a crawler receives is essentially empty because the
    #: content is assembled by JavaScript. This is not a parsing failure — it
    #: is one of the most consequential findings the platform can report.
    client_side_rendered: bool = False
    robots_noindex: bool = False


def _text_of(fragment: str) -> str:
    return html.unescape(_TAG.sub(" ", fragment)).strip()


def _meta_attrs(page_html: str) -> list[dict[str, str]]:
    tags: list[dict[str, str]] = []
    for raw in _META.findall(page_html):
        attrs: dict[str, str] = {}
        for match in _ATTR.finditer(raw):
            key = (match.group(1) or match.group(3) or "").lower()
            value = match.group(2) if match.group(2) is not None else (match.group(4) or "")
            attrs[key] = html.unescape(value)
        if attrs:
            tags.append(attrs)
    return tags


def visible_text(page_html: str) -> str:
    """The words a reader would see, with markup and scripts removed."""
    stripped = _COMMENT.sub(" ", _SCRIPT_OR_STYLE.sub(" ", page_html))
    return re.sub(r"\s+", " ", _text_of(stripped)).strip()


def parse(url: str, page_html: str, *, status: int = 200) -> RenderedPage:
    """Everything the SEO pipeline needs from one fetched page."""
    text = visible_text(page_html)
    words = [w for w in text.split() if any(c.isalnum() for c in w)]

    description = ""
    noindex = False
    for attrs in _meta_attrs(page_html):
        name = (attrs.get("name") or attrs.get("property") or "").lower()
        if name == "description" and not description:
            description = attrs.get("content", "").strip()
        if name == "robots" and "noindex" in attrs.get("content", "").lower():
            noindex = True

    canonical = ""
    link_tag = _CANONICAL.search(page_html)
    if link_tag:
        for match in _ATTR.finditer(link_tag.group(0)):
            key = (match.group(1) or match.group(3) or "").lower()
            if key == "href":
                canonical = html.unescape(match.group(2) or match.group(4) or "")

    schema: list[str] = []
    for block in _LD_JSON.findall(page_html):
        try:
            data = json.loads(block.strip())
        except ValueError:
            # Malformed JSON-LD is itself worth knowing about, but it is the
            # auditor's finding to make, not this parser's guess to repair.
            continue
        for entry in data if isinstance(data, list) else [data]:
            if isinstance(entry, dict):
                value = entry.get("@type")
                if value:
                    schema.extend(value if isinstance(value, list) else [str(value)])

    links = [html.unescape(href) for href in _HREF.findall(page_html)]
    missing_alt = sum(
        1
        for tag in _IMG.findall(page_html)
        if not re.search(r'\balt\s*=\s*["\'][^"\']', tag, re.I)
    )

    scripts = len(_SCRIPT_TAG.findall(page_html))
    shell = len(words) < _CSR_TEXT_FLOOR and scripts > 0
    return RenderedPage(
        url=url,
        status=status,
        title=_text_of(_TITLE.search(page_html).group(1)) if _TITLE.search(page_html) else "",
        description=description,
        canonical=canonical,
        headings=[_text_of(h) for h in _H1.findall(page_html)],
        text=text,
        word_count=len(words),
        schema_types=list(dict.fromkeys(schema)),
        links=links,
        images_without_alt=missing_alt,
        # Either signal alone is weak; a nearly empty body *with* scripts, or
        # an explicit framework mount point, is not.
        client_side_rendered=shell or (
            bool(_MOUNT_POINT.search(page_html)) and len(words) < _CSR_TEXT_FLOOR * 2
        ),
        robots_noindex=noindex,
    )


# ── Discovery ──────────────────────────────────────────────────────────────
_LOC = re.compile(r"<loc>\s*(.*?)\s*</loc>", re.DOTALL | re.I)
_IS_INDEX = re.compile(r"<sitemapindex\b", re.I)


def sitemap_urls(xml: str) -> tuple[list[str], bool]:
    """``(locations, is_index)`` from a sitemap document.

    A sitemap index points at more sitemaps rather than at pages, so the
    caller has to know which it got — fetching an index and treating its
    entries as pages is a common way to end up auditing nothing.
    """
    return [html.unescape(loc) for loc in _LOC.findall(xml)], bool(_IS_INDEX.search(xml))


def same_site(candidate: str, site: str) -> bool:
    """Whether a link stays on the site, ignoring the www prefix."""
    host = urlsplit(candidate).netloc.lower().removeprefix("www.")
    return not host or host == urlsplit(site).netloc.lower().removeprefix("www.")


def absolute(href: str, *, base: str) -> str:
    """Resolve a link against the page it was found on, dropping the fragment."""
    joined = urljoin(base, href.strip())
    split = urlsplit(joined)
    if split.scheme not in ("http", "https"):
        return ""
    return joined.split("#", 1)[0].rstrip("/") or f"{split.scheme}://{split.netloc}/"


def disallowed_paths(robots_txt: str, *, agent: str = "*") -> list[str]:
    """``Disallow`` rules that apply to us.

    Respected even though the site belongs to the customer who connected it:
    a crawler that ignores robots.txt on its own client's site is a crawler
    nobody should trust with anything else.
    """
    rules: list[str] = []
    applies = False
    for raw in robots_txt.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        key, _, value = line.partition(":")
        key, value = key.strip().lower(), value.strip()
        if key == "user-agent":
            applies = value == "*" or value.lower() == agent.lower()
        elif key == "disallow" and applies and value:
            rules.append(value)
    return rules


def is_allowed(path: str, disallow: list[str]) -> bool:
    return not any(path.startswith(rule) for rule in disallow)
