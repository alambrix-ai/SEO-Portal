"""Lexical internal-link suggestions and safe body insertion."""
from __future__ import annotations

import re
from dataclasses import dataclass
from html import escape
from urllib.parse import urlsplit

from app.agents.seo_article_writer.html_tools import safe_href
from app.agents.technical_seo_auditor.checks import links_in, normalise_url

#: Ignore common English filler when scoring page similarity.
_STOP = frozenset(
    "a an and are as at be by for from has have if in into is it its of on or "
    "our the their this to vs was what when where which who why will with you "
    "your best top guide page home about".split()
)

#: Max internal links per ~100 words of body (density guard).
MAX_LINKS_PER_100_WORDS = 3
#: Minimum token Jaccard overlap to suggest a link.
MIN_SCORE = 0.12
#: Prefer inserting into the first suitable paragraph.
_PARA_HTML = re.compile(r"(<p\b[^>]*>)(.*?)(</p>)", re.I | re.DOTALL)
_PARA_MD = re.compile(r"(?m)^(?!\#)(?!\s*[-*+]\s)(.+\S.+)$")
_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")
_WORD = re.compile(r"[a-z0-9]+", re.I)


@dataclass(frozen=True, slots=True)
class LinkCandidate:
    source_page_id: str
    target_page_id: str
    source_url: str
    target_url: str
    anchor: str
    score: float


def tokens(text: str) -> set[str]:
    return {
        w
        for w in _WORD.findall((text or "").lower())
        if len(w) > 1 and w not in _STOP
    }


def jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def word_count(body: str) -> int:
    plain = _WS.sub(" ", _TAG.sub(" ", body or "")).strip()
    return len(plain.split()) if plain else 0


def internal_link_count(body: str, *, domain: str = "") -> int:
    return len(links_in(body or "", domain=domain))


def density_ok(body: str, *, domain: str = "") -> bool:
    """Whether the page can accept one more internal link."""
    words = max(word_count(body), 1)
    links = internal_link_count(body, domain=domain)
    # Allow at least one link on short pages; then enforce the ratio.
    if links == 0:
        return True
    return (links + 1) / (words / 100.0) <= MAX_LINKS_PER_100_WORDS


def already_links_to(body: str, target_url: str, *, domain: str = "") -> bool:
    target = _path_key(target_url)
    return any(_path_key(href) == target for href in links_in(body, domain=domain))


def pick_anchor(source_title: str, target_title: str, target_keywords: list[str]) -> str:
    """Choose a short, human-looking anchor from the target page."""
    for keyword in target_keywords or []:
        phrase = _WS.sub(" ", str(keyword).strip())
        if 2 <= len(phrase) <= 60 and safe_href("/") and phrase.lower() not in {
            "click here",
            "here",
            "read more",
            "learn more",
        }:
            return phrase
    title = _WS.sub(" ", (target_title or "").strip())
    if title:
        return title[:60]
    return _WS.sub(" ", (source_title or "related page").strip())[:40] or "related page"


def validate_anchor(anchor: str) -> str:
    text = _WS.sub(" ", (anchor or "").strip())
    if not text or len(text) > 80:
        raise ValueError("Anchor text must be 1–80 characters")
    lowered = text.lower()
    if "javascript:" in lowered or "data:" in lowered or "<" in text or ">" in text:
        raise ValueError("Anchor text is not allowed")
    return text


def validate_target_href(url: str, *, domain: str = "") -> str:
    """Return a site-relative or absolute http(s) href safe to insert."""
    raw = (url or "").strip()
    if not raw:
        raise ValueError("Target URL is required")
    if raw.startswith("/"):
        if not safe_href(raw):
            raise ValueError("Unsafe target URL")
        return normalise_url(raw) or "/"
    if not safe_href(raw):
        raise ValueError("Unsafe target URL")
    parts = urlsplit(raw)
    host = (parts.netloc or "").lower().removeprefix("www.")
    bare = (domain or "").lower().removeprefix("www.")
    if bare and host and host != bare:
        # Cross-host absolute URLs are fine if http(s); keep absolute form.
        return raw
    path = parts.path or "/"
    return path.rstrip("/") or "/"


def insert_link(body: str, *, href: str, anchor: str) -> str:
    """Insert one link into the first suitable paragraph (HTML or Markdown)."""
    href = validate_target_href(href)
    anchor = validate_anchor(anchor)
    if not body.strip():
        return f'<p><a href="{escape(href, quote=True)}">{escape(anchor)}</a></p>\n'

    def html_repl(match: re.Match[str]) -> str:
        inner = match.group(2)
        if _TAG.search(inner) and "href=" in inner.lower():
            # Prefer a paragraph without an existing link when possible; still OK.
            pass
        if anchor.lower() in _TAG.sub("", inner).lower():
            # Wrap the first plain occurrence of the anchor words.
            pattern = re.compile(re.escape(anchor), re.I)
            if pattern.search(inner):
                return (
                    match.group(1)
                    + pattern.sub(
                        f'<a href="{escape(href, quote=True)}">{escape(anchor)}</a>',
                        inner,
                        count=1,
                    )
                    + match.group(3)
                )
        return (
            f"{match.group(1)}{inner.rstrip()} "
            f'<a href="{escape(href, quote=True)}">{escape(anchor)}</a>'
            f"{match.group(3)}"
        )

    if _PARA_HTML.search(body):
        return _PARA_HTML.sub(html_repl, body, count=1)

    # Markdown: append a linked phrase to the first prose paragraph.
    def md_repl(match: re.Match[str]) -> str:
        line = match.group(1)
        if "](" in line:
            return line
        return f"{line.rstrip()} [{anchor}]({href})"

    if _PARA_MD.search(body):
        return _PARA_MD.sub(md_repl, body, count=1)
    return f"{body.rstrip()}\n\n[{anchor}]({href})\n"


def score_pair(
    *,
    source_title: str,
    source_url: str,
    source_body: str,
    target_title: str,
    target_url: str,
    target_keywords: list[str],
) -> float:
    source_tokens = tokens(f"{source_title} {source_url}") | tokens(source_body[:2000])
    target_tokens = tokens(f"{target_title} {target_url}") | tokens(
        " ".join(target_keywords or [])
    )
    return jaccard(source_tokens, target_tokens)


def rank_candidates(
    pages: list[dict],
    *,
    domain: str = "",
    limit: int = 20,
) -> list[LinkCandidate]:
    """Score every ordered pair of pages and return the best unused links.

    Each ``pages`` item needs: id, url, title, body, target_keywords.
    """
    scored: list[LinkCandidate] = []
    for source in pages:
        body = source.get("body") or ""
        if not density_ok(body, domain=domain):
            continue
        for target in pages:
            if source["id"] == target["id"]:
                continue
            if already_links_to(body, target["url"], domain=domain):
                continue
            try:
                href = validate_target_href(target["url"], domain=domain)
            except ValueError:
                continue
            score = score_pair(
                source_title=source.get("title") or "",
                source_url=source.get("url") or "",
                source_body=body,
                target_title=target.get("title") or "",
                target_url=target.get("url") or "",
                target_keywords=list(target.get("target_keywords") or []),
            )
            if score < MIN_SCORE:
                continue
            anchor = pick_anchor(
                source.get("title") or "",
                target.get("title") or "",
                list(target.get("target_keywords") or []),
            )
            try:
                anchor = validate_anchor(anchor)
            except ValueError:
                continue
            scored.append(
                LinkCandidate(
                    source_page_id=source["id"],
                    target_page_id=target["id"],
                    source_url=source["url"],
                    target_url=href if href.startswith("/") else target["url"],
                    anchor=anchor,
                    score=round(score, 4),
                )
            )
    scored.sort(key=lambda c: c.score, reverse=True)
    # One suggestion per source page per run batch.
    seen_sources: set[str] = set()
    out: list[LinkCandidate] = []
    for candidate in scored:
        if candidate.source_page_id in seen_sources:
            continue
        seen_sources.add(candidate.source_page_id)
        out.append(candidate)
        if len(out) >= limit:
            break
    return out


def _path_key(url: str) -> str:
    raw = (url or "").strip()
    if raw.startswith("/"):
        return normalise_url(raw)
    parts = urlsplit(raw if "//" in raw else f"https://{raw}")
    return normalise_url(parts.path or "/")
