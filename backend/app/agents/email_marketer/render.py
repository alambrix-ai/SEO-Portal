"""Email copy: strict validation, HTML/text rendering, and an HTML sanitizer.

The model writes *copy* — subject, preview text, headline, paragraphs, button —
never markup. The HTML is built here from that copy, every value escaped, in
the layout email clients render reliably: nested tables, inline CSS, a fluid
600px column. The finished document is then passed through a whitelist
sanitizer anyway, so a script, form or event handler cannot reach a platform
even if a person edits the copy into something odd.

Each email platform has its own unsubscribe merge tag, and a campaign without
one is refused (or illegal to send). The tag is chosen per platform at render
time, so the same copy is correct in Mailchimp, Brevo, Klaviyo, or a download.
"""
from __future__ import annotations

import html as html_lib
import re
from html.parser import HTMLParser
from urllib.parse import urlsplit

# ── Platforms ──────────────────────────────────────────────────────────────
#: How each platform inserts the unsubscribe link.
#: ``href``: the tag is a URL, used in our own link; ``tag``: the platform
#: renders the whole link itself; ``text``: a visible placeholder (downloads).
UNSUBSCRIBE: dict[str, tuple[str, str]] = {
    "mailchimp": ("href", "*|UNSUB|*"),
    "brevo": ("href", "{{ unsubscribe }}"),
    "klaviyo": ("tag", "{% unsubscribe %}"),
    "export": ("text", "[UNSUBSCRIBE_LINK]"),
}
#: Platforms the agent can create drafts on, in preference order.
PLATFORMS = ("mailchimp", "brevo", "klaviyo")
#: Mailchimp's merge tag for the audience's own postal address (which
#: Mailchimp requires on every audience). Filled in by Mailchimp; not invented.
MAILCHIMP_ADDRESS = "*|HTML:LIST_ADDRESS_HTML|*"

# ── Limits ─────────────────────────────────────────────────────────────────
SUBJECT_MAX = 60
PREVIEW_MAX = 110
HEADLINE_MAX = 100
CTA_LABEL_MAX = 30
SEGMENT_MAX = 200
ALT_MAX = 150
PARAGRAPHS_MAX = 8
PARAGRAPH_MAX = 700
WORDS_MIN = 30
WORDS_MAX = 450

_SPAM = re.compile(
    r"\b(free money|act now|100% free|risk[- ]free|click here|guaranteed|no obligation|cash bonus|"
    r"winner|you(?:'ve| have)? won|urgent|make money|earn money|buy now|once in a lifetime|"
    r"limited time only|double your|no catch|amazing deal)\b|\$\$+",
    re.I,
)
_OFFER_WORDS = re.compile(
    r"\b(discount|% off|percent off|coupon|promo code|voucher|free shipping|free delivery|free trial|"
    r"sale|clearance|cashback|bogo|buy one get one)\b",
    re.I,
)
_AMOUNTS = re.compile(r"\d+(?:\.\d+)?\s?%|[$€£₹]\s?\d[\d,.]*|\b(?:rs\.?|inr|usd|eur)\s?\d[\d,.]*", re.I)
_TAGS = re.compile(r"<\s*/?\s*[a-z!]", re.I)
_URLS = re.compile(r"(https?://|www\.)", re.I)
_TEMPLATE = re.compile(r"(\{\{|\{%|\*\|)")
_PLACEHOLDER = re.compile(r"\[[^\]\n]{1,40}\]|\blorem ipsum\b|\bTBD\b|\bXXX\b", re.I)


def _clean_text(value: object, limit: int = 2000) -> str:
    return " ".join(str(value or "").split())[:limit]


def normalise_url(url: str) -> str:
    parts = urlsplit((url or "").strip())
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return ""
    path = parts.path.rstrip("/")
    query = f"?{parts.query}" if parts.query else ""
    return f"{parts.scheme}://{parts.netloc.lower()}{path}{query}"


def subject_problem(subject: str) -> str | None:
    if len(subject) < 3:
        return "the subject is missing"
    if len(subject) > SUBJECT_MAX:
        return f"the subject is {len(subject)} characters (at most {SUBJECT_MAX})"
    if "!!" in subject or subject.count("!") > 1:
        return "the subject uses more than one exclamation mark"
    caps = [word for word in re.findall(r"[A-Za-z]{3,}", subject) if word.isupper()]
    if len(caps) >= 2 or any(len(word) >= 5 for word in caps):
        return "the subject uses ALL CAPS words"
    if _SPAM.search(subject):
        return "the subject uses a phrase spam filters flag"
    return None


def validate(
    raw: object,
    *,
    allowed_urls: list[str],
    facts: str,
    needs_alt: bool = False,
) -> tuple[dict, list[str]]:
    """Clean the model's answer and list every reason it cannot be used.

    ``allowed_urls`` are the only links the button may use (the brand's own
    pages and articles). ``facts`` is the brand profile and article text: an
    offer, percentage or price that does not appear there was invented.
    """
    data = raw if isinstance(raw, dict) else {}
    problems: list[str] = []
    out = {
        "subject": _clean_text(data.get("subject"), 200),
        "preview_text": _clean_text(data.get("preview_text"), 300),
        "headline": _clean_text(data.get("headline"), 300),
        "cta_label": _clean_text(data.get("cta_label"), 100),
        "cta_url": str(data.get("cta_url") or "").strip(),
        "segment_suggestion": _clean_text(data.get("segment_suggestion"), 400),
        "image_prompt": _clean_text(data.get("image_prompt"), 600),
        "image_alt": _clean_text(data.get("image_alt"), 300),
    }
    paragraphs = data.get("paragraphs")
    if isinstance(paragraphs, str):
        paragraphs = [p for p in paragraphs.split("\n") if p.strip()]
    out["paragraphs"] = [_clean_text(p, PARAGRAPH_MAX + 1) for p in (paragraphs or []) if _clean_text(p)]

    problem = subject_problem(out["subject"])
    if problem:
        problems.append(problem)
    if not out["preview_text"]:
        problems.append("the preview text is missing")
    elif len(out["preview_text"]) > PREVIEW_MAX:
        problems.append(f"the preview text is {len(out['preview_text'])} characters (at most {PREVIEW_MAX})")
    if _SPAM.search(out["preview_text"]):
        problems.append("the preview text uses a phrase spam filters flag")
    if not out["headline"] or len(out["headline"]) > HEADLINE_MAX:
        problems.append(f"the headline must be 1–{HEADLINE_MAX} characters")
    if not out["paragraphs"] or len(out["paragraphs"]) > PARAGRAPHS_MAX:
        problems.append(f"the body must have 1–{PARAGRAPHS_MAX} paragraphs")
    if any(len(p) > PARAGRAPH_MAX for p in out["paragraphs"]):
        problems.append(f"a paragraph is longer than {PARAGRAPH_MAX} characters")
    words = sum(len(p.split()) for p in out["paragraphs"])
    if out["paragraphs"] and not WORDS_MIN <= words <= WORDS_MAX:
        problems.append(f"the body is {words} words (between {WORDS_MIN} and {WORDS_MAX})")
    if not 2 <= len(out["cta_label"]) <= CTA_LABEL_MAX:
        problems.append(f"the button label must be 2–{CTA_LABEL_MAX} characters")
    allowed = {normalise_url(u) for u in allowed_urls if normalise_url(u)}
    if normalise_url(out["cta_url"]) not in allowed:
        problems.append("the button must link to one of the listed pages")
    if not out["segment_suggestion"] or len(out["segment_suggestion"]) > SEGMENT_MAX:
        problems.append(f"the segment suggestion must be 1–{SEGMENT_MAX} characters")
    if needs_alt and not 3 <= len(out["image_alt"]) <= ALT_MAX:
        problems.append(f"the image description (alt text) must be 3–{ALT_MAX} characters")

    copy = " ".join([out["subject"], out["preview_text"], out["headline"], out["cta_label"], *out["paragraphs"]])
    if _TAGS.search(copy):
        problems.append("the copy contains HTML")
    if _URLS.search(" ".join(out["paragraphs"])):
        problems.append("the body contains a web address (links belong on the button)")
    if _TEMPLATE.search(copy):
        problems.append("the copy contains template or merge-tag syntax")
    if _PLACEHOLDER.search(copy):
        problems.append("the copy contains a placeholder such as [First name]")
    if _SPAM.search(" ".join(out["paragraphs"])):
        problems.append("the body uses a phrase spam filters flag")
    known = facts.lower()
    for match in _OFFER_WORDS.finditer(copy):
        if match.group(0).lower() not in known:
            problems.append(f"it mentions an offer (“{match.group(0)}”) that is not in your brand profile")
            break
    for match in _AMOUNTS.finditer(copy):
        if match.group(0).lower().replace(" ", "") not in known.replace(" ", ""):
            problems.append(f"it states a figure (“{match.group(0)}”) that is not in your brand profile")
            break
    return out, problems


# ── Rendering ──────────────────────────────────────────────────────────────
_FONT = "Arial,Helvetica,sans-serif"
_LANGS = {
    "english": "en", "hindi": "hi", "spanish": "es", "french": "fr", "german": "de",
    "portuguese": "pt", "italian": "it", "dutch": "nl", "arabic": "ar", "japanese": "ja",
}


def lang_code(language: str) -> str:
    return _LANGS.get((language or "").strip().lower(), "")


def _e(text: str) -> str:
    return html_lib.escape(text or "", quote=True)


def _footer_html(content: dict, channel: str) -> str:
    footer = content.get("footer") or {}
    business = _e(footer.get("business", ""))
    lines = [f"<strong>{business}</strong>"] if business else []
    if footer.get("address"):
        lines.append(_e(footer["address"]))
    elif channel == "mailchimp":
        lines.append(MAILCHIMP_ADDRESS)
    mode, tag = UNSUBSCRIBE.get(channel, UNSUBSCRIBE["export"])
    reason = f"You are receiving this email because you subscribed to {business or 'our emails'}."
    if mode == "href":
        link = f'<a href="{tag}" style="color:#4b5563;text-decoration:underline;">Unsubscribe</a>'
    elif mode == "tag":
        link = tag
    else:
        link = f"Unsubscribe: {tag}"
    lines.append(f"{reason} {link}")
    return "<br>".join(lines)


def render_html(content: dict, *, channel: str) -> str:
    """The send-ready HTML email for ``channel`` (a platform slug or "export")."""
    subject = _e(content.get("subject", ""))
    preview = _e(content.get("preview_text", ""))
    headline = _e(content.get("headline", ""))
    lang = lang_code((content.get("footer") or {}).get("language", ""))
    paragraphs = "".join(
        f'<p style="margin:0 0 16px 0;">{_e(p)}</p>' for p in content.get("paragraphs") or []
    )
    image = ""
    if str(content.get("image_url") or "").startswith("https://"):
        image = (
            '<tr><td style="padding:0;">'
            f'<img src="{_e(content["image_url"])}" alt="{_e(content.get("image_alt", ""))}" width="600" '
            'style="display:block;width:100%;max-width:600px;height:auto;border:0;border-radius:6px 6px 0 0;">'
            "</td></tr>"
        )
    button = ""
    if normalise_url(content.get("cta_url", "")):
        button = (
            '<tr><td align="center" style="padding:8px 32px 32px 32px;">'
            '<table role="presentation" cellpadding="0" cellspacing="0" border="0"><tr>'
            '<td align="center" bgcolor="#1f2937" style="border-radius:6px;background-color:#1f2937;">'
            f'<a href="{_e(content["cta_url"])}" target="_blank" '
            f'style="display:inline-block;padding:14px 28px;font-family:{_FONT};font-size:16px;line-height:20px;'
            'font-weight:bold;color:#ffffff;text-decoration:none;border-radius:6px;">'
            f"{_e(content.get('cta_label', ''))}</a></td></tr></table></td></tr>"
        )
    document = (
        "<!DOCTYPE html>"
        + (f'<html lang="{lang}">' if lang else "<html>")
        + '<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{subject}</title></head>"
        '<body style="margin:0;padding:0;background-color:#f3f4f6;">'
        '<div style="display:none;max-height:0;overflow:hidden;font-size:1px;line-height:1px;color:#f3f4f6;" '
        f'aria-hidden="true">{preview}</div>'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
        'style="background-color:#f3f4f6;"><tr><td align="center" style="padding:24px 12px;">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
        'style="width:100%;max-width:600px;background-color:#ffffff;border-radius:6px;">'
        f"{image}"
        f'<tr><td style="padding:32px 32px 8px 32px;font-family:{_FONT};">'
        f'<h1 style="margin:0 0 16px 0;font-size:24px;line-height:32px;color:#111827;">{headline}</h1></td></tr>'
        f'<tr><td style="padding:0 32px;font-family:{_FONT};font-size:16px;line-height:24px;color:#374151;">'
        f"{paragraphs}</td></tr>"
        f"{button}"
        "</table>"
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
        'style="width:100%;max-width:600px;"><tr>'
        f'<td align="center" style="padding:16px 32px;font-family:{_FONT};font-size:12px;line-height:18px;color:#4b5563;">'
        f"{_footer_html(content, channel)}</td></tr></table>"
        "</td></tr></table></body></html>"
    )
    return sanitize_html(document)


def render_text(content: dict, *, channel: str) -> str:
    """The plain-text part of the same email."""
    footer = content.get("footer") or {}
    _mode, tag = UNSUBSCRIBE.get(channel, UNSUBSCRIBE["export"])
    lines = [content.get("headline", ""), ""]
    for paragraph in content.get("paragraphs") or []:
        lines += [paragraph, ""]
    if content.get("cta_url"):
        lines += [f"{content.get('cta_label', '')}: {content['cta_url']}", ""]
    lines.append("—")
    if footer.get("business"):
        lines.append(footer["business"])
    if footer.get("address"):
        lines.append(footer["address"])
    elif channel == "mailchimp":
        lines.append("*|LIST:ADDRESSLINE|*")
    lines.append(f"Unsubscribe: {tag}")
    return "\n".join(lines).strip() + "\n"


# ── Sanitizer ──────────────────────────────────────────────────────────────
_ALLOWED_TAGS = {
    "html", "head", "body", "meta", "title", "table", "tbody", "thead", "tfoot", "tr", "td", "th",
    "div", "span", "p", "a", "img", "h1", "h2", "h3", "h4", "strong", "em", "b", "i", "u", "br",
    "hr", "ul", "ol", "li", "center", "small", "sup", "sub", "blockquote",
}
_VOID = {"meta", "img", "br", "hr"}
#: Elements removed together with everything inside them.
_DROP_WITH_CONTENT = {
    "script", "style", "form", "iframe", "object", "embed", "noscript", "template", "svg",
    "math", "select", "textarea", "button", "frameset", "frame", "applet",
}
_ALLOWED_ATTRS = {
    "style", "href", "src", "alt", "width", "height", "align", "valign", "role", "border",
    "cellpadding", "cellspacing", "bgcolor", "lang", "dir", "charset", "name", "content",
    "aria-hidden", "aria-label", "target", "title", "colspan", "rowspan",
}
_SAFE_LINK = re.compile(r"^(https?://|mailto:|\*\|[A-Z0-9_:]+\|\*$|\{\{\s*[a-z_]+\s*\}\}$)", re.I)
_BAD_STYLE = re.compile(r"(expression\s*\(|javascript:|url\s*\(|@import|behavior\s*:)", re.I)


class _Sanitizer(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.dropping = 0

    def handle_decl(self, decl: str) -> None:
        if decl.lower().startswith("doctype") and not self.dropping:
            self.out.append("<!DOCTYPE html>")

    def _attrs(self, tag: str, attrs: list[tuple[str, str | None]]) -> str:
        kept = []
        for name, value in attrs:
            name = name.lower()
            if name not in _ALLOWED_ATTRS or name.startswith("on"):
                continue
            value = value or ""
            if name in ("href", "src") and not _SAFE_LINK.match(value.strip()):
                continue
            if name == "style" and _BAD_STYLE.search(value):
                continue
            if tag == "meta" and name == "content" and "url=" in value.lower():
                continue  # no meta refresh redirects
            kept.append(f' {name}="{html_lib.escape(value, quote=True)}"')
        return "".join(kept)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in _DROP_WITH_CONTENT:
            if tag not in _VOID:
                self.dropping += 1
            return
        if self.dropping or tag not in _ALLOWED_TAGS:
            return
        self.out.append(f"<{tag}{self._attrs(tag, attrs)}>")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if self.dropping or tag in _DROP_WITH_CONTENT or tag not in _ALLOWED_TAGS:
            return
        self.out.append(f"<{tag}{self._attrs(tag, attrs)}>")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in _DROP_WITH_CONTENT:
            self.dropping = max(0, self.dropping - 1)
            return
        if self.dropping or tag not in _ALLOWED_TAGS or tag in _VOID:
            return
        self.out.append(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        if not self.dropping:
            self.out.append(html_lib.escape(data, quote=False))


def sanitize_html(markup: str) -> str:
    """Keep only email-safe markup: no scripts, forms, frames, handlers or script links."""
    parser = _Sanitizer()
    parser.feed(markup or "")
    parser.close()
    return "".join(parser.out)
