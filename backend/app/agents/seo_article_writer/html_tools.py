"""Article HTML: an allowlist sanitizer, a Markdown converter, and exports.

Model output is never trusted as markup. Every article body goes through
:func:`sanitize` before it is stored, and again before it is sent to a CMS
(a person may have edited it in the library since). What survives is a small,
predictable vocabulary of tags:

* headings ``h2``-``h4`` (an ``h1`` becomes an ``h2``: the page title is the
  only ``h1``, and the CMS renders it);
* paragraphs, lists, quotes, tables, ``strong``/``em``/``code``/``pre``, ``br``;
* links, with only an ``href``, and only when the caller's rule allows it.

Scripts, styles, frames, forms and embedded documents are dropped *with their
content*. Any other tag (``div``, ``span``, ``section``) is unwrapped: its
text is kept, the tag is not. Attributes other than ``href`` are dropped, so
no ``style``, ``on*`` handler or ``class`` reaches a customer's site.

Because the vocabulary is closed, converting it to Markdown is a small,
exact job (:func:`to_markdown`) rather than a best effort over arbitrary HTML.
"""
from __future__ import annotations

import html
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from html.parser import HTMLParser

#: Tags kept as they are.
ALLOWED_TAGS = frozenset(
    {
        "h2", "h3", "h4", "p", "ul", "ol", "li", "strong", "em", "a", "blockquote",
        "br", "table", "thead", "tbody", "tr", "th", "td", "code", "pre",
    }
)
#: Renamed on the way in.
RENAMED = {"h1": "h2", "b": "strong", "i": "em", "h5": "h4", "h6": "h4"}
#: Dropped together with everything inside them.
DROPPED_WITH_CONTENT = frozenset(
    {
        "script", "style", "iframe", "object", "embed", "noscript", "template", "svg",
        "math", "form", "head", "title", "frame", "frameset", "canvas", "audio", "video",
        "select", "textarea", "button",
    }
)
VOID = frozenset({"br", "img", "hr", "input", "meta", "link", "source", "wbr", "area", "col", "base"})
HEADINGS = ("h2", "h3", "h4")
BLOCKS = frozenset({"p", "ul", "ol", "table", "blockquote", "pre", *HEADINGS})
#: Elements a new block start closes, as a browser would.
_AUTO_CLOSED_BY_BLOCK = frozenset({"p", *HEADINGS})

HrefRule = Callable[[str], bool]


def safe_href(href: str) -> bool:
    """The general rule: a web address or a site-relative path, nothing else.

    Blocks ``javascript:``, ``data:`` and every other scheme.
    """
    value = (href or "").strip()
    if not value:
        return False
    lowered = value.lower()
    if lowered.startswith(("https://", "http://")):
        return True
    return value.startswith("/") and not value.startswith("//")


# ── The tree ───────────────────────────────────────────────────────────────
@dataclass(slots=True)
class Node:
    tag: str  # "" for the root
    attrs: dict[str, str] = field(default_factory=dict)
    children: list[Node | str] = field(default_factory=list)

    def text(self) -> str:
        return "".join(c if isinstance(c, str) else c.text() for c in self.children)


class _Builder(HTMLParser):
    def __init__(self, allow_href: HrefRule) -> None:
        super().__init__(convert_charrefs=True)
        self.allow_href = allow_href
        self.root = Node("")
        self.stack: list[Node] = [self.root]
        self._skip = 0

    @property
    def current(self) -> Node:
        return self.stack[-1]

    def _close(self, tag: str) -> bool:
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                return True
        return False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if self._skip:
            if tag not in VOID:
                self._skip += 1
            return
        if tag in DROPPED_WITH_CONTENT:
            if tag not in VOID:
                self._skip = 1
            return
        tag = RENAMED.get(tag, tag)
        if tag not in ALLOWED_TAGS:
            return  # unwrapped: its text lands in the current element
        if tag == "br":
            self.current.children.append(Node("br"))
            return
        kept: dict[str, str] = {}
        if tag == "a":
            href = next((value or "" for name, value in attrs if name.lower() == "href"), "")
            if not (safe_href(href) and self.allow_href(href)):
                return  # the link is dropped, its anchor text kept
            kept["href"] = href.strip()
        if tag in BLOCKS or tag == "li":
            while self.current.tag in _AUTO_CLOSED_BY_BLOCK:
                self.stack.pop()
        if tag == "li" and self.current.tag == "li":
            self.stack.pop()
        if tag in ("tr",) and self.current.tag in ("td", "th"):
            self.stack.pop()
        if tag in ("td", "th") and self.current.tag in ("td", "th"):
            self.stack.pop()
        node = Node(tag, kept)
        self.current.children.append(node)
        self.stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag.lower() not in VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if self._skip:
            if tag not in VOID:
                self._skip -= 1
            return
        tag = RENAMED.get(tag, tag)
        if tag in ALLOWED_TAGS and tag != "br":
            self._close(tag)

    def handle_data(self, data: str) -> None:
        if not self._skip and data:
            self.current.children.append(data)


def _prune(node: Node) -> None:
    """Drop elements with no text (empty paragraphs, empty list items)."""
    kept: list[Node | str] = []
    for child in node.children:
        if isinstance(child, str):
            kept.append(child)
            continue
        _prune(child)
        if child.tag == "br" or child.text().strip():
            kept.append(child)
    node.children = kept


def _wrap_loose_text(root: Node) -> None:
    """Text sitting directly in the body becomes a paragraph."""
    out: list[Node | str] = []
    run: list[Node | str] = []

    def flush() -> None:
        if any((c.strip() if isinstance(c, str) else c.text().strip()) for c in run):
            out.append(Node("p", children=list(run)))
        run.clear()

    for child in root.children:
        if isinstance(child, Node) and (child.tag in BLOCKS or child.tag == "li"):
            flush()
            if child.tag == "li":  # a stray item gets a list
                child = Node("ul", children=[child])
            out.append(child)
        else:
            run.append(child)
    flush()
    root.children = out


def parse(raw: str, *, allow_href: HrefRule = safe_href) -> Node:
    builder = _Builder(allow_href)
    builder.feed(raw or "")
    builder.close()
    root = builder.root
    _prune(root)
    _wrap_loose_text(root)
    return root


# ── HTML out ───────────────────────────────────────────────────────────────
def _serialize(node: Node | str) -> str:
    if isinstance(node, str):
        return html.escape(node, quote=False)
    if node.tag == "br":
        return "<br>"
    inner = "".join(_serialize(child) for child in node.children)
    if not node.tag:
        return inner
    attrs = "".join(f' {key}="{html.escape(value, quote=True)}"' for key, value in node.attrs.items())
    out = f"<{node.tag}{attrs}>{inner}</{node.tag}>"
    return out + "\n" if node.tag in BLOCKS or node.tag in ("li", "tr", "thead", "tbody") else out


def sanitize(raw: str, *, allow_href: HrefRule = safe_href) -> str:
    """The allowlisted form of ``raw``."""
    return "\n".join(line.rstrip() for line in _serialize(parse(raw, allow_href=allow_href)).splitlines()).strip()


def text_of(raw: str) -> str:
    return re.sub(r"\s+", " ", parse(raw).text()).strip()


def word_count(raw: str) -> int:
    return len(text_of(raw).split())


def links_in(raw: str) -> list[str]:
    found: list[str] = []

    def walk(node: Node) -> None:
        for child in node.children:
            if isinstance(child, Node):
                if child.tag == "a" and child.attrs.get("href"):
                    found.append(child.attrs["href"])
                walk(child)

    walk(parse(raw))
    return found


def empty_sections(raw: str) -> list[str]:
    """Headings with nothing under them.

    A heading is empty when the next thing after it is a heading of the same
    or a higher level, or the end of the body. An ``h2`` followed straight by
    an ``h3`` is not empty: the subsection is its content.
    """
    root = parse(raw)
    empty: list[str] = []
    open_headings: list[tuple[int, str]] = []  # headings with no content yet
    for child in root.children:
        if isinstance(child, Node) and child.tag in HEADINGS:
            level = int(child.tag[1])
            while open_headings and open_headings[-1][0] >= level:
                empty.append(open_headings.pop()[1])
            open_headings.append((level, child.text().strip()))
        elif child.strip() if isinstance(child, str) else child.text().strip():
            open_headings.clear()
    empty.extend(title for _, title in open_headings)
    return empty


def headings(raw: str, *, level: str = "h2") -> list[str]:
    return [
        child.text().strip()
        for child in parse(raw).children
        if isinstance(child, Node) and child.tag == level
    ]


def faq_html(heading: str, faq: list[dict]) -> str:
    """The FAQ as a section to append to the body, in the allowlisted tags."""
    pairs = [(str(i.get("q") or "").strip(), str(i.get("a") or "").strip()) for i in faq or []]
    pairs = [(q, a) for q, a in pairs if q and a]
    if not pairs:
        return ""
    parts = [f"<h2>{html.escape(heading or 'Frequently asked questions', quote=False)}</h2>"]
    for question, answer in pairs:
        parts.append(f"<h3>{html.escape(question, quote=False)}</h3>")
        parts.append(f"<p>{html.escape(answer, quote=False)}</p>")
    return "\n".join(parts)


# ── Markdown out ───────────────────────────────────────────────────────────
#: Stands in for a hard line break until whitespace has been collapsed.
_BREAK = "\ue000"
_MD_SPECIAL = re.compile(r"([\\`*_\[\]])")
_MD_LINE_START = re.compile(r"^(\s*)([#>+]|-(?=\s)|\d+\.(?=\s))", re.MULTILINE)


def _md_escape(text: str) -> str:
    return _MD_SPECIAL.sub(r"\\\1", text)


def _inline(node: Node | str) -> str:
    if isinstance(node, str):
        return _md_escape(re.sub(r"\s+", " ", node))
    if node.tag == "br":
        return _BREAK
    inner = "".join(_inline(child) for child in node.children)
    if node.tag == "strong":
        return f"**{inner.strip()}**" if inner.strip() else ""
    if node.tag == "em":
        return f"*{inner.strip()}*" if inner.strip() else ""
    if node.tag == "code":
        code = node.text().replace("`", "'")
        return f"`{code}`"
    if node.tag == "a":
        href = node.attrs.get("href", "").replace(")", "%29").replace(" ", "%20")
        return f"[{inner.strip()}]({href})"
    if node.tag in BLOCKS or node.tag in ("li", "table"):
        return _block(node).strip()
    return inner


def _inline_line(node: Node) -> str:
    text = "".join(_inline(child) for child in node.children)
    text = re.sub(r"[ \t]+", " ", text).strip()
    text = re.sub(rf"\s*{_BREAK}\s*", "  \n", text)
    # A line that would read as a heading, quote or list marker is escaped.
    return _MD_LINE_START.sub(lambda m: m.group(1) + "\\" + m.group(2), text)


def _list(node: Node, depth: int) -> str:
    lines: list[str] = []
    number = 0
    for child in node.children:
        if not isinstance(child, Node) or child.tag != "li":
            continue
        number += 1
        marker = f"{number}." if node.tag == "ol" else "-"
        pad = "   " * depth
        own = Node("li", children=[c for c in child.children if not (isinstance(c, Node) and c.tag in ("ul", "ol"))])
        lines.append(f"{pad}{marker} {_inline_line(own)}")
        for sub in child.children:
            if isinstance(sub, Node) and sub.tag in ("ul", "ol"):
                lines.append(_list(sub, depth + 1))
    return "\n".join(lines)


def _table(node: Node) -> str:
    rows: list[list[str]] = []
    header_rows = 0

    def collect(parent: Node, in_head: bool) -> None:
        nonlocal header_rows
        for child in parent.children:
            if not isinstance(child, Node):
                continue
            if child.tag in ("thead", "tbody"):
                collect(child, child.tag == "thead")
            elif child.tag == "tr":
                cells = [
                    _inline_line(cell).replace("|", "\\|").replace("\n", " ")
                    for cell in child.children
                    if isinstance(cell, Node) and cell.tag in ("td", "th")
                ]
                if in_head or (not rows and all(
                    isinstance(c, Node) and c.tag == "th" for c in child.children if isinstance(c, Node)
                )):
                    header_rows += 1
                rows.append(cells)

    collect(node, False)
    if not rows:
        return ""
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    head, body = (rows[0], rows[1:]) if header_rows else ([""] * width, rows)
    out = ["| " + " | ".join(head) + " |", "|" + "|".join(" --- " for _ in range(width)) + "|"]
    out.extend("| " + " | ".join(r) + " |" for r in body)
    return "\n".join(out)


def _block(node: Node) -> str:
    if node.tag in HEADINGS:
        return "#" * int(node.tag[1]) + " " + _inline_line(node)
    if node.tag == "p":
        return _inline_line(node)
    if node.tag in ("ul", "ol"):
        return _list(node, 0)
    if node.tag == "pre":
        code = node.text().strip("\n")
        return f"```\n{code}\n```"
    if node.tag == "blockquote":
        inner = _blocks(node)
        return "\n".join(f"> {line}" if line else ">" for line in inner.splitlines())
    if node.tag == "table":
        return _table(node)
    return _inline_line(node)


def _blocks(parent: Node) -> str:
    parts: list[str] = []
    loose: list[Node | str] = []

    def flush() -> None:
        line = _inline_line(Node("p", children=list(loose))) if loose else ""
        if line:
            parts.append(line)
        loose.clear()

    for child in parent.children:
        if isinstance(child, Node) and (child.tag in BLOCKS):
            flush()
            rendered = _block(child)
            if rendered.strip():
                parts.append(rendered)
        else:
            loose.append(child)
    flush()
    return "\n\n".join(parts)


def to_markdown(raw: str) -> str:
    """Markdown for the allowlisted HTML (anything else is sanitized first)."""
    return _blocks(parse(raw)).strip() + "\n"


def yaml_string(value: str) -> str:
    """A YAML double-quoted scalar (JSON strings are valid YAML)."""
    return json.dumps(str(value or ""), ensure_ascii=False)


# ── Standalone HTML document ───────────────────────────────────────────────
def document(content: dict) -> str:
    """A complete HTML page for the article, ready to upload anywhere."""
    title = str(content.get("title") or "")
    meta_title = str(content.get("meta_title") or title)
    description = str(content.get("meta_description") or "")
    body = sanitize(str(content.get("body_html") or ""))
    faq = [i for i in content.get("faq") or [] if isinstance(i, dict) and i.get("q") and i.get("a")]
    faq_section = faq_html(str(content.get("faq_heading") or ""), faq)
    graph = ""
    if faq:
        data = {
            "@context": "https://schema.org",
            "@type": "FAQPage",
            "mainEntity": [
                {
                    "@type": "Question",
                    "name": str(item["q"]),
                    "acceptedAnswer": {"@type": "Answer", "text": str(item["a"])},
                }
                for item in faq
            ],
        }
        encoded = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
        graph = f'\n<script type="application/ld+json">{encoded}</script>'
    esc = html.escape
    return (
        "<!doctype html>\n"
        "<html>\n<head>\n"
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{esc(meta_title, quote=False)}</title>\n"
        f'<meta name="description" content="{esc(description, quote=True)}">'
        f"{graph}\n"
        "</head>\n<body>\n<article>\n"
        f"<h1>{esc(title, quote=False)}</h1>\n"
        f"{body}\n"
        + (f'<section class="faq">\n{faq_section}\n</section>\n' if faq_section else "")
        + "</article>\n</body>\n</html>\n"
    )


def markdown_document(content: dict, *, date: str) -> str:
    """Front matter plus the body and FAQ as Markdown."""
    keywords = [str(k) for k in [content.get("target_keyword"), *(content.get("secondary_keywords") or [])] if k]
    front = [
        "---",
        f"title: {yaml_string(content.get('title') or '')}",
        f"description: {yaml_string(content.get('meta_description') or '')}",
        f"slug: {yaml_string(content.get('slug') or '')}",
        f"date: {date}",
    ]
    if content.get("meta_title"):
        front.append(f"seo_title: {yaml_string(content['meta_title'])}")
    if keywords:
        front.append("keywords: [" + ", ".join(yaml_string(k) for k in keywords) + "]")
    front.append("---")
    faq = faq_html(str(content.get("faq_heading") or ""), content.get("faq") or [])
    body = sanitize(str(content.get("body_html") or "")) + ("\n" + faq if faq else "")
    return "\n".join(front) + "\n\n" + to_markdown(body)
