"""Deterministic transforms for technical SEO fix proposals.

These are pure string / frontmatter operations. The agent builds a payload;
the applier re-reads the live page, applies the same transform, and writes
back through the page's own CMS — never inventing a destination connector.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from html import escape
from urllib.parse import urlsplit

from app.connectors.base import content_files as cf

#: Kinds the technical-fix agent will propose (v1).
AUTO_FIXABLE_KINDS: frozenset[str] = frozenset(
    {
        "title_too_long",
        "missing_meta_description",
        "broken_internal_link",
        "missing_canonical",
        "missing_h1",
    }
)

TITLE_MAX_CHARS = 60
META_TARGET_CHARS = 155
META_MIN_CHARS = 40

_META_DESC = re.compile(
    r'(<meta\b[^>]*name=["\']description["\'][^>]*content=["\'])([^"\']*)(["\'])',
    re.I,
)
_META_DESC_ANY = re.compile(
    r'<meta\b[^>]*name=["\']description["\'][^>]*>',
    re.I,
)
_CANONICAL = re.compile(r'<link\b[^>]*rel=["\']canonical["\'][^>]*>', re.I)
_HEAD_OPEN = re.compile(r"(<head\b[^>]*>)", re.I)
_H1_HTML = re.compile(r"<h1\b", re.I)
_H1_MD = re.compile(r"^#\s+\S", re.M)
_HTML_HREF = re.compile(
    r'(<a\b[^>]*href=["\'])([^"\']+)(["\'][^>]*>)(.*?)(</a>)',
    re.I | re.DOTALL,
)
_MD_LINK = re.compile(r"\[([^\]]*)\]\(([^)\s]+)\)")
_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")


@dataclass(frozen=True, slots=True)
class FixPlan:
    """What approving one issue will change."""

    fix_kind: str
    title: str | None = None
    meta_description: str | None = None
    body: str | None = None
    summary: str = ""


def is_auto_fixable(kind: str) -> bool:
    return kind in AUTO_FIXABLE_KINDS


def shorten_title(title: str, *, limit: int = TITLE_MAX_CHARS) -> str:
    """Trim a title at a word boundary without trailing punctuation debris."""
    text = _WS.sub(" ", (title or "").strip())
    if len(text) <= limit:
        return text
    cut = text[: limit + 1]
    if " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    else:
        cut = text[:limit]
    return cut.rstrip(" -–—:|,;.")


def excerpt_description(body: str, title: str = "", *, limit: int = META_TARGET_CHARS) -> str:
    """Build a meta description from visible prose (no HTML)."""
    plain = _WS.sub(" ", _TAG.sub(" ", body or "")).strip()
    if not plain:
        plain = (title or "").strip()
    if len(plain) <= limit:
        return plain
    cut = plain[: limit + 1]
    if " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    else:
        cut = plain[:limit]
    return cut.rstrip(" -–—:|,;.") + "…"


def unlink_broken(body: str, *, target: str) -> str:
    """Remove the hyperlink to ``target`` but keep the anchor text."""
    if not target or not body:
        return body
    normalised = _norm_href(target)

    def html_sub(match: re.Match[str]) -> str:  # noqa: ANN202
        href = match.group(2)
        if _norm_href(href) != normalised:
            return match.group(0)
        return match.group(4)

    def md_sub(match: re.Match[str]) -> str:  # noqa: ANN202
        href = match.group(2)
        if _norm_href(href) != normalised:
            return match.group(0)
        return match.group(1)

    out = _HTML_HREF.sub(html_sub, body)
    out = _MD_LINK.sub(md_sub, out)
    return out


def ensure_meta_description(body: str, description: str) -> str:
    """Set description in frontmatter when present, else HTML meta tag."""
    description = _WS.sub(" ", description.strip())
    if not description:
        return body
    parsed = cf.parse("__page.md", body)
    if parsed._had_block or _looks_like_frontmatter_site(body):
        cf.set_frontmatter_value(parsed, "description", description)
        return parsed.render()
    if _META_DESC.search(body):
        return _META_DESC.sub(
            lambda m: f"{m.group(1)}{escape(description, quote=True)}{m.group(3)}",
            body,
            count=1,
        )
    tag = f'<meta name="description" content="{escape(description, quote=True)}">'
    if _HEAD_OPEN.search(body):
        return _HEAD_OPEN.sub(rf"\1\n  {tag}", body, count=1)
    if _META_DESC_ANY.search(body):
        return body
    return f"{tag}\n{body}"


def ensure_canonical(body: str, url: str) -> str:
    """Declare a canonical URL in frontmatter or as a ``<link>`` tag."""
    canonical = (url or "").strip()
    if not canonical:
        return body
    parsed = cf.parse("__page.md", body)
    if parsed._had_block or _looks_like_frontmatter_site(body):
        cf.set_frontmatter_value(parsed, "canonical", canonical)
        return parsed.render()
    tag = f'<link rel="canonical" href="{escape(canonical, quote=True)}">'
    if _CANONICAL.search(body):
        return body
    if _HEAD_OPEN.search(body):
        return _HEAD_OPEN.sub(rf"\1\n  {tag}", body, count=1)
    return f"{tag}\n{body}"


def ensure_h1(body: str, title: str) -> str:
    """Prepend a single H1 derived from the page title when none exists."""
    heading = _WS.sub(" ", (title or "").strip())
    if not heading:
        return body
    if _H1_HTML.search(body) or _H1_MD.search(body):
        return body
    parsed = cf.parse("__page.md", body)
    prose = parsed.body if parsed._had_block else body
    if "<" in prose and ">" in prose:
        block = f"<h1>{escape(heading)}</h1>\n\n{prose.lstrip()}"
    else:
        block = f"# {heading}\n\n{prose.lstrip()}"
    if parsed._had_block:
        parsed.body = block
        return parsed.render()
    return block


def apply_fix(
    *,
    kind: str,
    title: str,
    body: str,
    url: str,
    target: str = "",
    proposed_title: str | None = None,
    proposed_meta: str | None = None,
) -> FixPlan:
    """Compute the write payload for one auto-fixable issue kind."""
    if kind == "title_too_long":
        new_title = proposed_title or shorten_title(title)
        if new_title == title:
            return FixPlan(fix_kind=kind, summary="Title already within limit")
        patched = body
        parsed = cf.parse("__page.md", body)
        if parsed._had_block:
            cf.set_frontmatter_value(parsed, "title", new_title)
            patched = parsed.render()
        return FixPlan(
            fix_kind=kind,
            title=new_title,
            body=patched if patched != body else None,
            summary=f"Shorten title to {len(new_title)} characters",
        )

    if kind == "missing_meta_description":
        desc = proposed_meta or excerpt_description(body, title)
        if len(desc) < META_MIN_CHARS and title:
            desc = excerpt_description(f"{title}. {body}", title)
        patched = ensure_meta_description(body, desc)
        return FixPlan(
            fix_kind=kind,
            meta_description=desc,
            body=patched if patched != body else body,
            summary="Add meta description",
        )

    if kind == "broken_internal_link":
        patched = unlink_broken(body, target=target)
        if patched == body:
            return FixPlan(fix_kind=kind, summary="Broken link already removed")
        return FixPlan(
            fix_kind=kind,
            body=patched,
            summary=f"Unlink broken target {target}",
        )

    if kind == "missing_canonical":
        patched = ensure_canonical(body, url)
        if patched == body:
            return FixPlan(fix_kind=kind, summary="Canonical already present")
        return FixPlan(
            fix_kind=kind,
            body=patched,
            summary="Declare canonical URL",
        )

    if kind == "missing_h1":
        patched = ensure_h1(body, title)
        if patched == body:
            return FixPlan(fix_kind=kind, summary="H1 already present")
        return FixPlan(
            fix_kind=kind,
            body=patched,
            summary="Add H1 from page title",
        )

    raise ValueError(f"Issue kind {kind!r} is not auto-fixable")


def validate_payload(payload: dict) -> dict:
    """Reject malformed approval payloads before any write."""
    if not isinstance(payload, dict):
        raise ValueError("Fix payload must be an object")
    fix_kind = str(payload.get("fix_kind") or "")
    if fix_kind not in AUTO_FIXABLE_KINDS:
        raise ValueError(f"Unsupported fix kind: {fix_kind}")
    page_id = str(payload.get("page_id") or "")
    issue_id = str(payload.get("issue_id") or "")
    if not page_id or not issue_id:
        raise ValueError("Fix payload needs page_id and issue_id")
    out = {
        "fix_kind": fix_kind,
        "page_id": page_id,
        "issue_id": issue_id,
        "url": str(payload.get("url") or "")[:512],
        "target": str(payload.get("target") or "")[:512],
        "title": str(payload.get("title") or "")[:512],
        "proposed_title": payload.get("proposed_title"),
        "proposed_meta": payload.get("proposed_meta"),
        "proposed_body": payload.get("proposed_body"),
    }
    if out["proposed_title"] is not None:
        out["proposed_title"] = str(out["proposed_title"])[:512]
    if out["proposed_meta"] is not None:
        out["proposed_meta"] = str(out["proposed_meta"])[:320]
    if out["proposed_body"] is not None:
        out["proposed_body"] = str(out["proposed_body"])
    return out


def _norm_href(href: str) -> str:
    raw = (href or "").strip()
    if not raw or raw.startswith(("#", "mailto:", "tel:", "javascript:")):
        return raw.lower()
    parts = urlsplit(raw if "//" in raw or raw.startswith("/") else f"/{raw}")
    path = parts.path or "/"
    return path.rstrip("/") or "/"


def _looks_like_frontmatter_site(body: str) -> bool:
    return body.lstrip().startswith("---")
