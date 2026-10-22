"""The analysis report as an enterprise-style PDF (reportlab).

Layout: a full-page cover (vector score gauge, headline tiles), a contents
page with page numbers, then numbered sections with a running header and a
"Page N of M" footer. The document is built twice: the first pass records
where each section lands and how many pages there are, the second pass
prints those numbers (in the contents and the footer).

Everything under ``report["ai"]`` is model output and is read defensively.
Each section is built in isolation, so one malformed block only drops that
block; if the whole layout still fails, a plain PDF is returned instead. A
download never fails.
"""
from __future__ import annotations

import html
import io
import logging
import os
import re
from typing import Any

from reportlab.graphics.shapes import ArcPath, Drawing, Line, Rect, String
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.platypus import (
    BaseDocTemplate,
    CondPageBreak,
    Flowable,
    Frame,
    KeepTogether,
    NextPageTemplate,
    PageBreak,
    PageTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
)

from app.services.demo_analysis.export import STATUS_LABEL, _d, _date, _l, _s, _sections, host

log = logging.getLogger(__name__)

# ── Palette ─────────────────────────────────────────────────────────────────
ACCENT = "#3b5bdb"
ACCENT_BG = "#edf2ff"
ACCENT_SOFT = "#91a7ff"
NAVY = "#111a3b"
INK = "#111827"
TEXT = "#374151"
MUTED = "#6b7280"
FAINT = "#9ca3af"
LINE = "#e5e7eb"
SURFACE = "#f8fafc"
TRACK = "#eceff5"
AI_FG, AI_BG = "#6741d9", "#f3f0ff"
MEAS_FG, MEAS_BG = "#1e3a5f", "#e7eef7"
TONE = {  # name -> (foreground, background)
    "good": ("#2b8a3e", "#ebfbee"),
    "ok": ("#b4580a", "#fff4e6"),
    "poor": ("#c92a2a", "#fff0f0"),
    "none": ("#4b5563", "#f1f3f5"),
    "accent": (ACCENT, ACCENT_BG),
    "ai": (AI_FG, AI_BG),
    "measured": (MEAS_FG, MEAS_BG),
}
STATUS_TONE = {"fail": "poor", "warn": "ok", "pass": "good", "info": "none"}
STATUS_ORDER = {"fail": 0, "warn": 1, "info": 2, "pass": 3}
LEVEL_ORDER = {"high": 0, "medium": 1, "low": 2, "quick": 2}
SERP_BLUE, SERP_GREY, SERP_DARK = "#1a0dab", "#4d5156", "#202124"

PAGE_W, PAGE_H = A4
MARGIN = 18 * mm
CONTENT_W = PAGE_W - 2 * MARGIN

_COLOR_CACHE: dict[str, Any] = {}


def C(hexval: str) -> Any:
    c = _COLOR_CACHE.get(hexval)
    if c is None:
        c = _COLOR_CACHE[hexval] = colors.HexColor(hexval)
    return c


# ── Fonts and text safety ───────────────────────────────────────────────────
_FONTS: tuple[str, str, str, str] | None = None
_GLYPHS: frozenset[int] | None = None


def _fonts() -> tuple[str, str, str, str]:
    """Bitstream Vera (ships with reportlab): wider Unicode than the PDF base fonts."""
    global _FONTS, _GLYPHS
    if _FONTS is None:
        import reportlab
        from reportlab.lib.fonts import addMapping
        from reportlab.pdfbase.ttfonts import TTFont

        folder = os.path.join(os.path.dirname(reportlab.__file__), "fonts")
        try:
            faces = {}
            for name, file in (("Vera", "Vera.ttf"), ("VeraBd", "VeraBd.ttf"), ("VeraIt", "VeraIt.ttf"), ("VeraBI", "VeraBI.ttf")):
                font = TTFont(name, os.path.join(folder, file))
                pdfmetrics.registerFont(font)
                faces[name] = font
            addMapping("Vera", 0, 0, "Vera")
            addMapping("Vera", 1, 0, "VeraBd")
            addMapping("Vera", 0, 1, "VeraIt")
            addMapping("Vera", 1, 1, "VeraBI")
            glyphs = set(faces["Vera"].face.charToGlyph)
            for f in ("VeraBd", "VeraIt", "VeraBI"):
                glyphs &= set(faces[f].face.charToGlyph)
            _GLYPHS = frozenset(glyphs)
            _FONTS = ("Vera", "VeraBd", "VeraIt", "VeraBI")
        except Exception:  # noqa: BLE001 - fall back to the built-in fonts
            _GLYPHS = frozenset(range(32, 256))
            _FONTS = ("Helvetica", "Helvetica-Bold", "Helvetica-Oblique", "Helvetica-BoldOblique")
    return _FONTS


#: Characters Vera lacks, replaced with readable equivalents (others are dropped).
_SWAP = str.maketrans({
    "→": "->", "←": "<-", "⇒": "=>", "▲": "+", "▼": "-", "✓": "", "✔": "", "✗": "x", "★": "*",
    "₹": "Rs ", "●": "•", "○": "•", "■": "•", "▪": "•", "​": "", " ": " ", " ": " ",
    "−": "-", "‑": "-", "️": "", "⃣": "",
})
_SPACES = re.compile(r"[ \t]{2,}")


def _clean(text: Any, limit: int = 1500) -> str:
    """Plain text that the registered font can draw, trimmed to ``limit``."""
    _fonts()
    glyphs = _GLYPHS or frozenset()
    raw = _s(text).translate(_SWAP)
    out = "".join(ch for ch in raw if ch == "\n" or (ord(ch) >= 32 and ord(ch) in glyphs))
    out = _SPACES.sub(" ", out).strip()
    if len(out) > limit:
        out = out[: limit - 1].rstrip() + "…"
    return out


def _p(text: Any, limit: int = 1500) -> str:
    """Escaped for reportlab's mini-markup."""
    return html.escape(_clean(text, limit), quote=False).replace("\n", "<br/>")


def _num(v: Any) -> float | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(_s(v))
    except ValueError:
        return None


def _fmt(v: Any) -> str:
    n = _num(v)
    if n is None:
        return "—"
    return f"{int(round(n)):,}" if abs(n - round(n)) < 0.05 else f"{n:,.1f}"


def _soft(text: str, width: float, font: str, size: float) -> str:
    """Break words too wide for a narrow column at ``-`` ``/`` ``_`` ``.``, never mid-word."""
    out = []
    for word in text.split(" "):
        if not word or "\n" in word or _width(word, font, size) <= width:
            out.append(word)
            continue
        lines, cur = [], ""
        for part in re.split(r"(?<=[-/_.])", word):
            if cur and _width(cur + part, font, size) > width:
                lines.append(cur)
                cur = part
            else:
                cur += part
        lines.append(cur)
        out.append("\n".join(lines))
    return " ".join(out)


def _tone(score: Any) -> str:
    n = _num(score)
    if n is None:
        return "none"
    return "good" if n >= 80 else "ok" if n >= 55 else "poor"


def _width(text: str, font: str, size: float) -> float:
    return pdfmetrics.stringWidth(text, font, size)


def _fit(text: str, font: str, size: float, width: float) -> str:
    if _width(text, font, size) <= width:
        return text
    while text and _width(text + "…", font, size) > width:
        text = text[:-1]
    return text.rstrip() + "…"


def _strs(v: Any, cap: int = 40) -> list[str]:
    return [t for t in (_clean(x, 400) for x in _l(v)[:cap]) if t]


def _dicts(v: Any, cap: int = 60) -> list[dict[str, Any]]:
    return [x for x in _l(v)[:cap] if isinstance(x, dict)]


# ── Small vector components ─────────────────────────────────────────────────
class Chips(Flowable):
    """A wrapping row of rounded labels: ``[(text, tone)]``."""

    def __init__(self, chips: list[tuple[str, str]], *, size: float = 7.2, upper: bool = False, bold: bool = True, solid: bool = False, right: bool = False):
        super().__init__()
        self.right = right
        self.chips = [(_clean(t, 80).upper() if upper else _clean(t, 80), tone) for t, tone in chips if _clean(t, 80)]
        self.size, self.bold, self.solid = size, bold, solid
        self.h, self.padx, self.gap = size + 6.2, 5.5, 4.0
        self._layout: list[tuple[float, float, float, str, str]] = []

    def wrap(self, aw: float, ah: float) -> tuple[float, float]:
        font = _fonts()[1 if self.bold else 0]
        x, row, self._layout = 0.0, 0, []
        for text, tone in self.chips:
            text = _fit(text, font, self.size, max(aw - 2 * self.padx, 10))
            w = _width(text, font, self.size) + 2 * self.padx
            if x and x + w > aw:
                x, row = 0.0, row + 1
            self._layout.append((x, row, w, text, tone))
            x += w + self.gap
        if self.right:
            ends: dict[int, float] = {}
            for x, r, w, _t, _tn in self._layout:
                ends[r] = x + w
            self._layout = [(x + aw - ends[r], r, w, t, tn) for x, r, w, t, tn in self._layout]
        rows = row + 1 if self.chips else 0
        self.width = aw
        self.height = rows * self.h + max(rows - 1, 0) * self.gap
        return aw, self.height

    def draw(self) -> None:
        c = self.canv
        font = _fonts()[1 if self.bold else 0]
        for x, row, w, text, tone in self._layout:
            fg, bg = TONE.get(tone, TONE["none"])
            y = self.height - (row + 1) * self.h - row * self.gap
            c.setFillColor(C(fg if self.solid else bg))
            c.roundRect(x, y, w, self.h, self.h / 2, stroke=0, fill=1)
            c.setFillColor(colors.white if self.solid else C(fg))
            c.setFont(font, self.size)
            c.drawString(x + self.padx, y + (self.h - self.size) / 2 + self.size * 0.17, text)


class Rule(Flowable):
    def __init__(self, width: float | None = None, thickness: float = 0.6, color: str = LINE, space: float = 6):
        super().__init__()
        self.w, self.t, self.color, self.space = width, thickness, color, space

    def wrap(self, aw: float, ah: float) -> tuple[float, float]:
        self.width = self.w or aw
        return self.width, self.t + 2 * self.space

    def draw(self) -> None:
        self.canv.setStrokeColor(C(self.color))
        self.canv.setLineWidth(self.t)
        self.canv.line(0, self.space, self.width, self.space)


class Button(Flowable):
    def __init__(self, text: str, size: float = 8.5):
        super().__init__()
        self.text, self.size = _clean(text, 60), size

    def wrap(self, aw: float, ah: float) -> tuple[float, float]:
        self.text = _fit(self.text, _fonts()[1], self.size, aw - 24)
        self.width = min(aw, _width(self.text, _fonts()[1], self.size) + 24)
        return self.width, self.size + 12

    def draw(self) -> None:
        c = self.canv
        c.setFillColor(C(ACCENT))
        c.roundRect(0, 0, self.width, self.size + 12, 4, stroke=0, fill=1)
        c.setFillColor(colors.white)
        c.setFont(_fonts()[1], self.size)
        c.drawCentredString(self.width / 2, 6 + self.size * 0.15, self.text)


class SectionHead(Flowable):
    """Kicker, title, short accent rule and lede. Registers itself for the contents."""

    def __init__(self, num: int, title: str, tag: str, lede: str, styles: dict[str, ParagraphStyle]):
        super().__init__()
        self.num, self.title, self.tag = num, title, tag
        self.toc = (num, title, tag)
        self._title = Paragraph(_p(title), styles["sec_title"])
        self._lede = Paragraph(_p(lede), styles["lede"]) if lede else None


    def wrap(self, aw: float, ah: float) -> tuple[float, float]:
        self.width = aw
        self._th = self._title.wrap(aw, ah)[1]
        self._lh = self._lede.wrap(aw * 0.9, ah)[1] if self._lede else 0
        self.height = 16 + self._th + 12 + 3 + (12 + self._lh if self._lede else 0) + 14
        return aw, self.height

    def draw(self) -> None:
        c = self.canv
        bold = _fonts()[1]
        top = self.height
        kicker = f"SECTION {self.num:02d}"
        t = c.beginText(0, top - 8)
        t.setFont(bold, 7.5)
        t.setCharSpace(1.4)
        t.setFillColor(C(ACCENT))
        t.textOut(kicker)
        t.setCharSpace(0)
        c.drawText(t)
        kw = _width(kicker, bold, 7.5) + 1.4 * len(kicker)
        fg, bg = TONE["ai" if self.tag.startswith("AI") else "measured"]
        label = self.tag.upper()
        lw = _width(label, bold, 6.3) + 10
        c.setFillColor(C(bg))
        c.roundRect(kw + 8, top - 11, lw, 11, 5.5, stroke=0, fill=1)
        c.setFillColor(C(fg))
        c.setFont(bold, 6.3)
        c.drawString(kw + 13, top - 7.8, label)
        y = top - 16 - self._th
        self._title.drawOn(c, 0, y)
        y -= 12
        c.setFillColor(C(ACCENT))
        c.rect(0, y, 34, 3, stroke=0, fill=1)
        if self._lede:
            y -= 12 + self._lh
            self._lede.drawOn(c, 0, y)


class TocRow(Flowable):
    def __init__(self, num: int, title: str, tag: str, page: int | None, link: str | None):
        super().__init__()
        self.num, self.title, self.tag, self.page, self.link = num, title, tag, page, link

    def wrap(self, aw: float, ah: float) -> tuple[float, float]:
        self.width, self.height = aw, getattr(self, "row_h", 31)
        return aw, self.height

    def draw(self) -> None:
        c = self.canv
        reg, bold = _fonts()[:2]
        c.setFillColor(C(ACCENT))
        c.setFont(bold, 11)
        c.drawString(0, 15, f"{self.num:02d}")
        c.setFillColor(C(INK))
        c.setFont(reg, 11)
        title = _fit(_clean(self.title), reg, 11, self.width - 120)
        c.drawString(30, 15, title)
        c.setFillColor(C(AI_FG if self.tag.startswith("AI") else MEAS_FG))
        c.setFont(bold, 6.2)
        c.drawString(30, 5, self.tag.upper())
        page = str(self.page) if self.page else ""
        c.setFillColor(C(INK))
        c.setFont(bold, 11)
        c.drawRightString(self.width, 15, page)
        start = 30 + _width(title, reg, 11) + 8
        end = self.width - _width(page or "00", bold, 11) - 8
        if end > start:
            c.setStrokeColor(C(FAINT))
            c.setLineWidth(0.9)
            c.setLineCap(1)
            c.setDash([0.1, 3.2])
            c.line(start, 16, end, 16)
            c.setDash([])
        c.setStrokeColor(C(LINE))
        c.setLineWidth(0.5)
        c.line(0, 0, self.width, 0)
        if self.link:
            c.linkRect("", self.link, (0, 0, self.width, self.height), relative=1, thickness=0)


# ── Charts (reportlab.graphics) ─────────────────────────────────────────────
def _gauge(score: Any, size: float, *, thickness: float | None = None, label: str = "", grade: str = "",
           sub: str = "out of 100") -> Drawing:
    """A 270-degree ring gauge with the score in the middle."""
    reg, bold = _fonts()[:2]
    th = thickness or size * 0.085
    label_h = 14 if label else 0
    d = Drawing(size, size + label_h)
    cx, cy, r = size / 2, size / 2 + label_h, size / 2 - th / 2 - 1
    track = ArcPath(strokeColor=C(TRACK), strokeWidth=th, fillColor=None, strokeLineCap=1)
    track.addArc(cx, cy, r, -45, 225, moveTo=True)
    d.add(track)
    n = _num(score)
    fg = TONE[_tone(score)][0]
    if n is not None and n > 0:
        frac = max(0.0, min(n, 100.0)) / 100
        arc = ArcPath(strokeColor=C(fg), strokeWidth=th, fillColor=None, strokeLineCap=1)
        arc.addArc(cx, cy, r, 225 - 270 * frac, 225, moveTo=True)
        d.add(arc)
    big = (size * (0.27 if len(_fmt(score)) < 3 else 0.22)) if n is not None else size * 0.13
    lift = size * 0.08 if grade else 0
    d.add(String(cx, cy - big * 0.28 + (size * 0.05 if sub else 0) + lift, _fmt(score) if n is not None else "Not measured",
                 fontName=bold, fontSize=big, fillColor=C(INK if n is not None else MUTED), textAnchor="middle"))
    if sub and n is not None:
        d.add(String(cx, cy - big * 0.28 - size * 0.09 + lift, sub, fontName=reg, fontSize=max(size * 0.058, 6),
                     fillColor=C(MUTED), textAnchor="middle"))
    if grade:
        gs = size * 0.07
        text = f"GRADE {grade}"
        w = _width(text, bold, gs) + gs * 2.2
        d.add(Rect(cx - w / 2, cy - r * 0.6 - gs * 0.9, w, gs * 2.1, rx=gs * 1.05, ry=gs * 1.05,
                   fillColor=C(TONE[_tone(score)][1]), strokeColor=None))
        d.add(String(cx, cy - r * 0.6 - gs * 0.9 + gs * 0.68, text, fontName=bold, fontSize=gs,
                     fillColor=C(fg), textAnchor="middle"))
    if label:
        d.add(String(cx, 2, label, fontName=bold, fontSize=7.5, fillColor=C(TEXT), textAnchor="middle"))
    return d


def _bar_chart(rows: list[tuple[str, Any, bool]], width: float) -> Drawing:
    """Horizontal bars, 0-100, coloured by threshold. ``rows``: (label, score, emphasise)."""
    reg, bold, ital = _fonts()[:3]
    row_h, top, bottom, label_w, value_w = 24, 6, 30, 138, 46
    height = top + bottom + row_h * len(rows)
    d = Drawing(width, height)
    x0, bar_w = label_w, width - label_w - value_w
    for mark in (55, 80):
        x = x0 + bar_w * mark / 100
        d.add(Line(x, bottom - 4, x, height - top + 2, strokeColor=C("#cbd2dc"), strokeWidth=0.6, strokeDashArray=[2, 2]))
        d.add(String(x, bottom - 14, str(mark), fontName=reg, fontSize=6.8, fillColor=C(FAINT), textAnchor="middle"))
    for x, text in ((x0, "0"), (x0 + bar_w, "100")):
        d.add(String(x, bottom - 14, text, fontName=reg, fontSize=6.8, fillColor=C(FAINT), textAnchor="middle"))
    for i, (label, score, strong) in enumerate(rows):
        y = height - top - (i + 1) * row_h + 7
        d.add(String(0, y + 2, _fit(_clean(label, 60), bold if strong else reg, 8.6, label_w - 10),
                     fontName=bold if strong else reg, fontSize=8.6, fillColor=C(INK)))
        bh = 11 if strong else 9
        by = y + (9 - bh) / 2 + 1
        n = _num(score)
        if n is None:
            d.add(Rect(x0, by, bar_w, bh, rx=bh / 2, ry=bh / 2, fillColor=colors.white, strokeColor=C("#c5ccd6"),
                       strokeWidth=0.6, strokeDashArray=[2, 2]))
            d.add(String(x0 + 8, by + bh / 2 - 2.6, "Not measured", fontName=ital, fontSize=7.2, fillColor=C(MUTED)))
            d.add(String(width, y + 2, "—", fontName=bold, fontSize=9, fillColor=C(MUTED), textAnchor="end"))
            continue
        d.add(Rect(x0, by, bar_w, bh, rx=bh / 2, ry=bh / 2, fillColor=C(TRACK), strokeColor=None))
        fill = max(bar_w * max(0.0, min(n, 100.0)) / 100, bh)
        d.add(Rect(x0, by, fill, bh, rx=bh / 2, ry=bh / 2, fillColor=C(ACCENT if strong else TONE[_tone(n)][0]), strokeColor=None))
        d.add(String(width, y + 1.5, _fmt(n), fontName=bold, fontSize=10 if strong else 9,
                     fillColor=C(INK if strong else TONE[_tone(n)][0]), textAnchor="end"))
        if strong and i < len(rows) - 1:
            d.add(Line(0, y - 7.5, width, y - 7.5, strokeColor=C(LINE), strokeWidth=0.6))
    lx = 0.0
    for tone, text in (("good", "80+ strong"), ("ok", "55-79 needs work"), ("poor", "below 55 weak")):
        d.add(Rect(lx, 1, 7, 7, rx=1.5, ry=1.5, fillColor=C(TONE[tone][0]), strokeColor=None))
        d.add(String(lx + 11, 1.6, text, fontName=reg, fontSize=7, fillColor=C(MUTED)))
        lx += 22 + _width(text, reg, 7)
    return d


def _meter(score: Any, out_of: int = 10) -> Drawing:
    n = _num(score) or 0
    seg, gap = 9, 2
    d = Drawing(out_of * (seg + gap), 7)
    fg = TONE[_tone(n * 100 / out_of)][0]
    for i in range(out_of):
        d.add(Rect(i * (seg + gap), 0, seg, 7, rx=1.5, ry=1.5, fillColor=C(fg if i < round(n) else TRACK), strokeColor=None))
    return d


def _timeline(labels: list[tuple[str, str]], width: float, col_w: float, gap: float) -> Drawing:
    reg, bold = _fonts()[:2]
    d = Drawing(width, 44)
    d.add(Line(col_w / 2, 30, width - col_w / 2, 30, strokeColor=C("#c5d0f5"), strokeWidth=2))
    shades = ("#3b5bdb", "#5c7cfa", "#91a7ff")
    for i, (title, sub) in enumerate(labels):
        cx = i * (col_w + gap) + col_w / 2
        d.add(Rect(cx - 9, 21, 18, 18, rx=9, ry=9, fillColor=C(shades[i % 3]), strokeColor=colors.white, strokeWidth=2.5))
        d.add(String(cx, 26.6, str(i + 1), fontName=bold, fontSize=8.5, fillColor=colors.white, textAnchor="middle"))
        d.add(String(cx, 9, title, fontName=bold, fontSize=9.5, fillColor=C(INK), textAnchor="middle"))
        d.add(String(cx, 0, sub, fontName=reg, fontSize=7, fillColor=C(MUTED), textAnchor="middle"))
    return d


# ── Styles ──────────────────────────────────────────────────────────────────
def _styles() -> dict[str, ParagraphStyle]:
    reg, bold, ital, _bi = _fonts()
    family = "Vera" if reg == "Vera" else reg

    def ps(name: str, **kw: Any) -> ParagraphStyle:
        base: dict[str, Any] = {
            "fontName": family, "fontSize": 9.4, "leading": 13.8, "textColor": C(TEXT),
            "uriWasteReduce": 0.3, "embeddedHyphenation": 1, "allowWidows": 0, "allowOrphans": 0,
        }
        base.update(kw)
        return ParagraphStyle(name, **base)

    return {
        "body": ps("body", spaceAfter=4),
        "lead": ps("lead", fontSize=10.6, leading=16, textColor=C(INK)),
        "lede": ps("lede", fontSize=10, leading=14.5, textColor=C(MUTED)),
        "small": ps("small", fontSize=7.8, leading=10.8, textColor=C(MUTED)),
        "note": ps("note", fontSize=7.8, leading=10.8, textColor=C(MUTED), fontName=ital, spaceBefore=4),
        "cell": ps("cell", fontSize=8.2, leading=11.2),
        "cellb": ps("cellb", fontSize=8.2, leading=11.2, fontName=bold, textColor=C(INK)),
        "cellm": ps("cellm", fontSize=8, leading=11, textColor=C(MUTED)),
        "cellr": ps("cellr", fontSize=8.2, leading=11.2, alignment=TA_RIGHT),
        "th": ps("th", fontSize=6.8, leading=9, fontName=bold, textColor=C(MUTED)),
        "thr": ps("thr", fontSize=6.8, leading=9, fontName=bold, textColor=C(MUTED), alignment=TA_RIGHT),
        "label": ps("label", fontSize=6.6, leading=9, fontName=bold, textColor=C(MUTED), spaceAfter=1.5),
        "h3": ps("h3", fontSize=12, leading=15.5, fontName=bold, textColor=C(INK), spaceBefore=14, spaceAfter=7),
        "h4": ps("h4", fontSize=10.2, leading=13.4, fontName=bold, textColor=C(INK), spaceAfter=3),
        "sec_title": ps("sec_title", fontSize=23, leading=27, fontName=bold, textColor=C(INK)),
        "bullet": ps("bullet", leftIndent=12, bulletIndent=0, spaceAfter=3.5),
        "tile_v": ps("tile_v", fontSize=19, leading=22, fontName=bold, textColor=C(INK)),
        "tile_l": ps("tile_l", fontSize=6.8, leading=9, fontName=bold, textColor=C(MUTED)),
        "tile_s": ps("tile_s", fontSize=7.4, leading=10, textColor=C(MUTED)),
        "serp_url": ps("serp_url", fontSize=7.8, leading=10.5, textColor=C(SERP_DARK)),
        "serp_title": ps("serp_title", fontSize=12.4, leading=15.5, textColor=C(SERP_BLUE)),
        "serp_desc": ps("serp_desc", fontSize=8.6, leading=12.4, textColor=C(SERP_GREY)),
        "hero": ps("hero", fontSize=13.5, leading=17.5, fontName=bold, textColor=C(INK), spaceAfter=5),
        "center_small": ps("center_small", fontSize=7.4, leading=10, textColor=C(MUTED), alignment=TA_CENTER),
    }


# ── The report ──────────────────────────────────────────────────────────────
class _Report:
    """Builds the story (once per pass) from the stored report."""

    def __init__(self, report: dict[str, Any], extra: dict[str, Any]):
        from app.core.config import settings

        self.r, self.x = report, extra
        self.brand = _clean(getattr(settings, "app_name", "") or "Synaprank.ai", 60) or "Synaprank.ai"
        self.sec = _sections(report)
        self.st = _styles()
        self.fonts = _fonts()
        self.site = host(_s(report.get("website")))
        self.biz_name = _clean(self.sec["business"].get("name"), 80) or _clean(self.site, 80) or "Your website"
        self.when = _date(_s(report.get("generated_at")))
        self.workspace = _clean(extra.get("workspace"), 80)
        self.agent_names = {_s(a.get("agent")): _s(a.get("name")) for a in _dicts(report.get("automation"))}

    # helpers ---------------------------------------------------------------
    def para(self, text: Any, style: str = "body", limit: int = 1500, fit: float | None = None) -> Paragraph:
        """``fit``: the column's inner width; over-long words are broken at hyphens or slashes."""
        if not fit:
            return Paragraph(_p(text, limit), self.st[style])
        st = self.st[style]
        font = self.fonts[1] if style in ("cellb", "h4") else self.fonts[0]
        soft = _soft(_clean(text, limit), fit, font, st.fontSize)
        return Paragraph(html.escape(soft, quote=False).replace("\n", "<br/>"), st)

    def raw(self, markup: str, style: str = "body") -> Paragraph:
        return Paragraph(markup, self.st[style])

    def bullets(self, items: list[str], color: str = ACCENT, style: str = "bullet") -> list[Any]:
        st = self.st[style] if style == "bullet" else ParagraphStyle(f"{style}_b", parent=self.st[style], leftIndent=12, spaceAfter=3)
        return [Paragraph(f"<bullet><font color='{color}' size='7'>•</font></bullet>{_p(i, 600)}", st) for i in items if i]

    def numbered(self, items: list[str], style: str = "bullet") -> list[Any]:
        st = self.st[style] if style == "bullet" else ParagraphStyle(f"{style}_n", parent=self.st[style], leftIndent=14, spaceAfter=2.5)
        return [Paragraph(f"<bullet><font color='{ACCENT}'><b>{n}.</b></font></bullet>{_p(i, 600)}", st)
                for n, i in enumerate(items, 1)]

    def h3(self, text: str, right: str = "") -> Any:
        """A sub-heading; :meth:`story` keeps it off the bottom of a page."""
        if not right:
            p = Paragraph(_p(text), self.st["h3"])
            p._head = True
            return p
        t = Table([[Paragraph(_p(text), self.st["h4"]), Paragraph(right, self.st["cellr"])]],
                  colWidths=[CONTENT_W * 0.62, CONTENT_W * 0.38])
        t.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "BOTTOM"), ("LEFTPADDING", (0, 0), (-1, -1), 0),
                               ("RIGHTPADDING", (0, 0), (-1, -1), 0), ("TOPPADDING", (0, 0), (-1, -1), 14),
                               ("BOTTOMPADDING", (0, 0), (-1, -1), 5), ("LINEBELOW", (0, 0), (-1, -1), 0.8, C(INK))]))
        t._head = True
        return t

    def grid(self, header: list[str], rows: list[list[Any]], widths: list[float], *, right: tuple[int, ...] = ()) -> Table:
        head = [Paragraph(_p(h.upper()), self.st["thr" if i in right else "th"]) for i, h in enumerate(header)]
        t = Table([head, *rows], colWidths=widths, repeatRows=1)
        cmds: list[Any] = [
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LINEBELOW", (0, 0), (-1, 0), 0.9, C(INK)),
            ("LINEBELOW", (0, 1), (-1, -1), 0.4, C(LINE)),
            ("TOPPADDING", (0, 0), (-1, -1), 5.5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 5.5),
            ("LEFTPADDING", (0, 0), (-1, -1), 5),
            ("RIGHTPADDING", (0, 0), (-1, -1), 5),
            ("TOPPADDING", (0, 0), (-1, 0), 2),
        ]
        for i in range(2, len(rows) + 1, 2):
            cmds.append(("BACKGROUND", (0, i), (-1, i), C(SURFACE)))
        t.setStyle(TableStyle(cmds))
        return t

    def card(self, rows: list[list[Any]], widths: list[float] | None = None, *, bar: str | None = None, bg: str = "#ffffff",
             border: str = LINE, pad: float = 10, spans: list[Any] | None = None, keep: bool = True) -> Any:
        t = Table(rows, colWidths=widths or [CONTENT_W], splitInRow=1)
        cmds: list[Any] = [
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("BACKGROUND", (0, 0), (-1, -1), C(bg)),
            ("BOX", (0, 0), (-1, -1), 0.6, C(border)),
            ("LEFTPADDING", (0, 0), (-1, -1), pad + (3 if bar else 0)),
            ("RIGHTPADDING", (0, 0), (-1, -1), pad),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ("TOPPADDING", (0, 0), (-1, 0), pad),
            ("BOTTOMPADDING", (0, -1), (-1, -1), pad),
        ]
        if bar:
            cmds.append(("LINEBEFORE", (0, 0), (0, -1), 3.2, C(bar)))
        cmds += spans or []
        t.setStyle(TableStyle(cmds))
        return KeepTogether([t, Spacer(1, 8)]) if keep else t

    def tiles(self, items: list[tuple[str, str, str, str]], cols: int = 4, width: float = CONTENT_W) -> Table:
        """``(value, label, tone, sub)`` tiles in a grid."""
        gap = 8
        tw = (width - gap * (cols - 1)) / cols
        rows: list[list[Any]] = []
        cmds: list[Any] = [("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 9),
                           ("RIGHTPADDING", (0, 0), (-1, -1), 9), ("TOPPADDING", (0, 0), (-1, -1), 8),
                           ("BOTTOMPADDING", (0, 0), (-1, -1), 9)]
        for r in range(0, len(items), cols):
            chunk = items[r:r + cols]
            row: list[Any] = []
            ri = len(rows)
            for ci, (value, label, tone, sub) in enumerate(chunk):
                fg = TONE.get(tone, TONE["none"])[0] if tone != "ink" else INK
                cell = [Paragraph(_p(label.upper()), self.st["tile_l"]),
                        Paragraph(f"<font color='{fg}'>{_p(value)}</font>", self.st["tile_v"])]
                if sub:
                    cell.append(Paragraph(_p(sub), self.st["tile_s"]))
                row.append(cell)
                col = ci * 2
                cmds += [("BACKGROUND", (col, ri), (col, ri), C(SURFACE)),
                         ("BOX", (col, ri), (col, ri), 0.4, C(LINE)),
                         ("LINEABOVE", (col, ri), (col, ri), 2.4, C(fg))]
                if ci < cols - 1:
                    row.append("")
            while len(row) < cols * 2 - 1:
                row.append("")
            rows.append(row)
            rows.append([""] * (cols * 2 - 1))
        rows.pop()
        widths = []
        for i in range(cols):
            widths.append(tw)
            if i < cols - 1:
                widths.append(gap)
        t = Table(rows, colWidths=widths, rowHeights=[None if i % 2 == 0 else gap for i in range(len(rows))])
        cmds += [("TOPPADDING", (0, i), (-1, i), 0) for i in range(1, len(rows), 2)]
        cmds += [("BOTTOMPADDING", (0, i), (-1, i), 0) for i in range(1, len(rows), 2)]
        t.setStyle(TableStyle(cmds))
        return t

    def pill(self, status: str) -> Chips:
        return Chips([(STATUS_LABEL.get(status, status or "Note"), STATUS_TONE.get(status, "none"))], upper=True, size=6.6)

    def two_col(self, left: list[Any], right: list[Any], ratio: float = 0.5, gap: float = 14) -> Table:
        lw = (CONTENT_W - gap) * ratio
        t = Table([[left, "", right]], colWidths=[lw, gap, CONTENT_W - gap - lw], splitInRow=1)
        t.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0),
                               ("RIGHTPADDING", (0, 0), (-1, -1), 0), ("TOPPADDING", (0, 0), (-1, -1), 0),
                               ("BOTTOMPADDING", (0, 0), (-1, -1), 0)]))
        return t

    def callout(self, flow: list[Any], tone: str = "accent") -> Any:
        fg, bg = TONE[tone]
        return self.card([[flow]], bar=fg, bg=bg, border=bg, pad=12)

    def empty(self, text: str) -> Paragraph:
        return self.para(text, "note")

    # sections --------------------------------------------------------------
    def sections(self) -> list[tuple[str, str, str, list[Any]]]:
        """``(title, tag, lede, flowables)`` for every section that has something to show."""
        s = self.sec
        plan = [
            ("Executive summary", "AI summary + measured", "The headline view: where the site stands, what is working and where to start.", self.s_summary),
            ("Scorecard", "Measured", "Scores by area, from reading the site today. Higher is better; 80 or more is strong.", self.s_scorecard),
            ("What changed", "Measured", "", self.s_changes),
            ("Top priorities", "AI suggestion", "The few changes most likely to move results, in the order we would tackle them.", self.s_priorities),
            ("Detailed findings", "Measured", "Every check we ran, grouped by area. Issues come first; passing checks are listed compactly.", self.s_findings),
            ("Messaging review", "AI suggestion", "Does the home page say clearly what the business does, for whom, and what to do next?", self.s_messaging),
            ("Keyword opportunities", "AI suggestion", "Searches the business should aim to win, and the page that should win each one.", self.s_keywords),
            ("Ready-to-paste titles and descriptions", "AI suggestion", "Suggested search snippets, previewed the way they would appear in Google results.", self.s_page_fixes),
            ("Content plan", "AI suggestion", "Articles and pages to write next, each aimed at a specific search.", self.s_content),
            ("Search campaign", "AI suggestion", "A ready-to-build Google Ads search campaign with ad previews, keywords and exclusions.", self.s_campaign),
            ("Social posts and emails", "AI suggestion", "Draft posts, email campaigns and ad angles in the brand's voice.", self.s_social),
            ("AI search and local", "AI suggestion", "Questions to answer on the site so AI assistants can quote it, competitors to study and local actions.", self.s_aisearch),
            ("90-day roadmap", "AI suggestion", "The recommendations sequenced into three 30-day phases.", self.s_roadmap),
            ("Automation", "Measured", "Which Synaprank.ai agents work on which findings.", self.s_automation),
            ("Connected at the time", "Measured", "Integrations and agents in the workspace when this analysis ran.", self.s_context),
            ("Methodology and notes", "Measured", "How this report was produced, and which parts are measured facts versus AI suggestions.", self.s_method),
        ]
        out = []
        for title, tag, lede, fn in plan:
            try:
                flow = fn()
            except Exception:  # noqa: BLE001 - one malformed block must not break the download
                log.warning("analysis pdf: section %r skipped", title, exc_info=True)
                continue
            if flow:
                if title == "What changed":
                    lede = f"Compared with the previous analysis on {_date(_s(_d(self.x.get('comparison')).get('previous_at'))) or 'an earlier date'}."
                if title == "Automation":
                    lede = f"Which {self.brand} agents work on which findings."
                out.append((title, tag, lede, flow))
        _ = s
        return out

    def s_summary(self) -> list[Any]:
        st = self.sec["strategy"]
        overall, summary = _d(self.r.get("overall")), _d(self.r.get("summary"))
        out: list[Any] = []
        text = _clean(st.get("executive_summary"), 2500)
        if text:
            out.append(self.callout([self.para("SUMMARY", "label"), Spacer(1, 3), self.para(text, "lead", 2500)]))
        score = overall.get("score")
        grade = _clean(overall.get("grade"), 4)
        out += [self.h3("Key metrics"), self.tiles([
            (f"{_fmt(score)}", "Overall score", _tone(score), f"out of 100{' · grade ' + grade if grade else ''}"),
            (_fmt(summary.get("checks", len(_l(self.r.get("checks"))))), "Checks run", "ink", "automated checks"),
            (_fmt(summary.get("failed", 0)), "To fix", "poor", "failing checks"),
            (_fmt(summary.get("warnings", 0)), "To improve", "ok", "warnings"),
            (_fmt(summary.get("passed", 0)), "Passed", "good", "checks in good shape"),
            (_fmt(summary.get("pages_read", 0)), "Pages read", "accent", "crawled today"),
            (_fmt(summary.get("links_checked", 0)), "Links checked", "accent", "internal and external"),
            (_fmt(len(_l(_d(self.r.get("measured")).get("broken_links")))), "Broken links", "poor" if _l(_d(self.r.get("measured")).get("broken_links")) else "good", "found on pages read"),
        ])]
        biz = self.sec["business"]
        facts = [
            ("What it does", _clean(biz.get("what_it_does"), 500)),
            ("Industry", _clean(biz.get("industry"), 200)),
            ("Business model", _clean(biz.get("business_model"), 100)),
            ("Audience", " · ".join(_strs(biz.get("audience"), 8))),
            ("Offer", " · ".join(_strs(biz.get("offer"), 10))),
            ("Locations", " · ".join(_strs(biz.get("locations"), 10))),
            ("Tone of voice", _clean(biz.get("tone"), 300)),
        ]
        facts = [(k, v) for k, v in facts if v]
        if facts:
            rows = [[self.para(k, "cellb"), self.para(v, "cell")] for k, v in facts]
            out += [self.h3("Business profile"), self.grid(["Attribute", "As understood from the site"], rows, [36 * mm, CONTENT_W - 36 * mm]),
                    self.para("Inferred by AI from the site's own text.", "note")]
        strengths = _strs(st.get("strengths"), 12)
        priorities = [_clean(_d(p).get("title") if isinstance(p, dict) else p, 200) for p in _l(st.get("top_priorities"))[:3]]
        priorities = [p for p in priorities if p]
        if strengths or priorities:
            left = [self.para("WHAT IS WORKING", "label"), Spacer(1, 3), *self.bullets(strengths, TONE["good"][0])] if strengths else []
            right = [self.para("WHERE TO START", "label"), Spacer(1, 3), *self.numbered(priorities),
                     self.para("Details in Top priorities.", "note")] if priorities else []
            if left and right:
                out += [Spacer(1, 10), self.two_col(left, right, 0.56, 20)]
            else:
                out += [Spacer(1, 10), *(left or right)]
        return out if (text or facts or strengths) else out

    def s_scorecard(self) -> list[Any]:
        cats = {k: _d(v) for k, v in _d(self.r.get("categories")).items()}
        overall = _d(self.r.get("overall"))
        out: list[Any] = []
        if cats:
            rows = [("Overall", overall.get("score"), True)] + [(_s(c.get("label")) or k, c.get("score"), False) for k, c in cats.items()]
            out += [_bar_chart(rows, CONTENT_W), Spacer(1, 6), self.h3("By area")]
            trs = []
            for k, c in cats.items():
                sc = c.get("score")
                fg = TONE[_tone(sc)][0]
                passed, total = _fmt(c.get("passed", 0)), _fmt(c.get("total", 0))
                trs.append([
                    self.para(c.get("label") or k, "cellb"),
                    self.raw(f"<font color='{fg}'><b>{_fmt(sc) if _num(sc) is not None else 'Not measured'}</b></font>", "cell"),
                    self.para(f"{passed} of {total}" if _num(c.get("total")) else "—", "cell"),
                    self.para(c.get("about"), "cellm"),
                ])
            out.append(self.grid(["Area", "Score", "Checks passed", "What it covers"], trs, [42 * mm, 27 * mm, 24 * mm, CONTENT_W - 93 * mm]))
        out += self.lighthouse()
        facts = self.site_facts()
        if facts:
            out += [self.h3("Site facts"), self.grid(["Measured", "Result"], facts, [50 * mm, CONTENT_W - 50 * mm])]
        return out if (cats or facts) else []

    def lighthouse(self) -> list[Any]:
        speed = _d(_d(self.r.get("measured")).get("pagespeed"))
        if not speed:
            return []
        out: list[Any] = [self.h3("Google Lighthouse · mobile, home page")]
        if not speed.get("measured"):
            reason = _clean(speed.get("reason"), 300) or "Google PageSpeed did not return results for this run."
            out.append(self.callout([self.raw("<b>Not measured on this run.</b>", "cell"), self.para(reason, "cellm"),
                                     self.para("The Speed & experience area is left unscored rather than estimated.", "cellm")], "none"))
            return out
        scores = _d(speed.get("scores"))
        names = (("performance", "Performance"), ("seo", "SEO"), ("accessibility", "Accessibility"), ("best_practices", "Best practices"))
        gw = CONTENT_W / 4
        cells = [_gauge(scores.get(k), 78, label=label, sub="") for k, label in names]
        t = Table([cells], colWidths=[gw] * 4)
        t.setStyle(TableStyle([("ALIGN", (0, 0), (-1, -1), "CENTER"), ("BACKGROUND", (0, 0), (-1, -1), C(SURFACE)),
                               ("BOX", (0, 0), (-1, -1), 0.4, C(LINE)), ("TOPPADDING", (0, 0), (-1, -1), 10),
                               ("BOTTOMPADDING", (0, 0), (-1, -1), 8)]))
        out.append(t)
        lab = _d(speed.get("lab"))
        names2 = {"lcp": "Largest Contentful Paint", "cls": "Cumulative Layout Shift", "tbt": "Total Blocking Time",
                  "fcp": "First Contentful Paint", "si": "Speed Index", "speed_index": "Speed Index", "tti": "Time to Interactive",
                  "ttfb": "Server response (TTFB)", "inp": "Interaction to Next Paint"}
        rows = []
        for k, v in list(lab.items())[:12]:
            if isinstance(v, dict):
                v = v.get("display") or v.get("value")
            if _s(v):
                rows.append([self.para(names2.get(k, k.replace("_", " ").title()), "cellb"), self.para(v, "cell")])
        if rows:
            out += [Spacer(1, 8), self.grid(["Lab metric", "Value"], rows, [70 * mm, CONTENT_W - 70 * mm])]
        opps = []
        for o in _l(speed.get("opportunities"))[:10]:
            if isinstance(o, dict):
                title = _clean(o.get("title") or o.get("id"), 200)
                saving = _clean(o.get("savings") or o.get("display") or o.get("savings_ms"), 60)
                if title:
                    opps.append(f"{title}{' — ' + saving + (' ms' if saving.isdigit() else '') if saving else ''}")
            elif _s(o):
                opps.append(_clean(o, 300))
        if opps:
            out += [Spacer(1, 6), self.para("SPEED OPPORTUNITIES", "label"), *self.bullets(opps)]
        return out

    def site_facts(self) -> list[list[Any]]:
        m = _d(self.r.get("measured"))
        if not m:
            return []
        yes = lambda b: "Yes" if b else "No"  # noqa: E731
        rows: list[tuple[str, str]] = []
        if _s(m.get("platform")):
            rows.append(("Platform", _s(m["platform"])))
        if "https" in m:
            rows.append(("Secure (HTTPS)", yes(m.get("https"))))
        if "sitemap_pages" in m or "sitemap_url" in m:
            rows.append(("XML sitemap", f"{_fmt(m.get('sitemap_pages'))} pages listed" if _num(m.get("sitemap_pages")) is not None else "Not found"))
        if "robots_txt" in m:
            rows.append(("robots.txt", yes(m.get("robots_txt"))))
        if "llms_txt" in m:
            rows.append(("llms.txt (AI crawler guide)", yes(m.get("llms_txt"))))
        if "trackers" in m:
            rows.append(("Tracking tags", ", ".join(_strs(m.get("trackers"))) or "None detected"))
        if "social_links" in m:
            social = m.get("social_links")
            names = list(social.keys()) if isinstance(social, dict) else _strs(social)
            rows.append(("Social profiles linked", ", ".join(_clean(n, 40).title() for n in names[:12]) or "None found"))
        if "phones" in m or "emails" in m:
            contact = _strs(m.get("phones"), 4) + _strs(m.get("emails"), 4)
            rows.append(("Contact details on pages", ", ".join(contact) or "None found"))
        lbs = _d(m.get("local_business_schema"))
        if lbs:
            rows.append(("Business schema", f"{_clean(lbs.get('type'), 60) or 'Present'}"
                         f"{'; no address' if not _s(lbs.get('address')) else ''}{'; no phone' if not _s(lbs.get('telephone')) else ''}"))
        if "broken_links" in m:
            broken = _l(m.get("broken_links"))
            rows.append(("Broken links", f"{len(broken)} found" if broken else "None found"))
        return [[self.para(k, "cellb"), self.para(v, "cell")] for k, v in rows]

    def s_changes(self) -> list[Any]:
        comp = _d(self.x.get("comparison"))
        if not comp:
            return []
        o = _d(comp.get("overall"))
        before, after = _num(o.get("before")), _num(o.get("after"))
        change = _num(o.get("change"))
        if change is None and before is not None and after is not None:
            change = after - before
        out: list[Any] = [self.tiles([
            (_fmt(before), "Previous score", _tone(before), _date(_s(comp.get("previous_at")))),
            (_fmt(after), "Score now", _tone(after), self.when),
            (self.delta(change), "Change", "good" if (change or 0) > 0 else "poor" if (change or 0) < 0 else "none", "points"),
        ], cols=3)]
        cats = _d(comp.get("categories"))
        if cats:
            rows = []
            for k, c in list(cats.items())[:20]:
                c = _d(c)
                b, a, ch = _num(c.get("before")), _num(c.get("after")), _num(c.get("change"))
                if ch is None and a is not None and b is not None:
                    ch = a - b
                fg = TONE["good" if (ch or 0) > 0 else "poor" if (ch or 0) < 0 else "none"][0]
                rows.append([self.para(c.get("label") or k, "cellb"), self.para(_fmt(b), "cellr"), self.para(_fmt(a), "cellr"),
                             self.raw(f"<font color='{fg}'><b>{self.delta(ch)}</b></font>", "cellr")])
            out += [self.h3("By area"), self.grid(["Area", "Before", "After", "Change"], rows,
                                                    [CONTENT_W - 90 * mm, 30 * mm, 30 * mm, 30 * mm], right=(1, 2, 3))]
        fixed = [f if isinstance(f, dict) else {"title": f} for f in _l(comp.get("fixed"))[:40]]
        if fixed:
            def was(f: dict[str, Any]) -> str:
                before, after = (STATUS_LABEL.get(_s(f.get(k)), _s(f.get(k))) for k in ("before", "after"))
                return " · ".join(x for x in (before and f"Was {before}", after and f"now {after}") if x)

            rows = [[Chips([("Fixed", "good")], upper=True, size=6.6), self.para(_d(f).get("title"), "cellb"), self.para(was(_d(f)), "cellm")]
                    for f in fixed]
            out += [self.h3(f"Fixed since last time ({len(fixed)})"), self.grid(["", "Check", "Status"], rows, [20 * mm, CONTENT_W - 60 * mm, 40 * mm])]
        new = [f if isinstance(f, dict) else {"title": f} for f in _l(comp.get("new_issues"))[:40]]
        if new:
            rows = [[self.pill(_s(_d(f).get("status")) or "warn"), self.para(_d(f).get("title"), "cellb"), self.para(_d(f).get("evidence"), "cell")] for f in new]
            out += [self.h3(f"New issues ({len(new)})"), self.grid(["", "Check", "What we found"], rows, [20 * mm, 50 * mm, CONTENT_W - 70 * mm])]
        still = [_clean(_d(f).get("title") if isinstance(f, dict) else f, 200) for f in _l(comp.get("still_open"))[:40]]
        still = [x for x in still if x]
        if still:
            out += [self.h3(f"Still open ({len(still)})"), Chips([(t, "ok") for t in still], bold=False, size=7.4)]
        added, removed = _strs(comp.get("connectors_added")), _strs(comp.get("connectors_removed"))
        if added or removed:
            out.append(self.h3("Integrations"))
            if added:
                out += [self.para("CONNECTED SINCE", "label"), Chips([(a, "good") for a in added], size=7.4), Spacer(1, 6)]
            if removed:
                out += [self.para("DISCONNECTED SINCE", "label"), Chips([(a, "poor") for a in removed], size=7.4)]
        return out

    @staticmethod
    def delta(v: float | None) -> str:
        if v is None:
            return "—"
        return f"+{_fmt(v)}" if v > 0 else _fmt(v) if v < 0 else "0"

    def s_priorities(self) -> list[Any]:
        out: list[Any] = []
        n = 0
        for p in _l(self.sec["strategy"].get("top_priorities"))[:15]:
            p = p if isinstance(p, dict) else {"title": p}
            title = _clean(p.get("title"), 200)
            if not title:
                continue
            n += 1
            impact, effort, when = _clean(p.get("impact"), 30).lower(), _clean(p.get("effort"), 30).lower(), _clean(p.get("timeframe"), 40)
            chips = []
            if impact:
                chips.append((f"{impact} impact", {"high": "accent", "medium": "accent"}.get(impact, "none")))
            if effort:
                chips.append((f"{effort} effort", {"low": "good", "quick": "good", "medium": "ok", "high": "poor"}.get(effort, "none")))
            if when:
                chips.append((when, "none"))
            body: list[Any] = [self.para(title, "h4")]
            if chips:
                body += [Spacer(1, 2), Chips(chips, upper=True, size=6.4), Spacer(1, 6)]
            if _s(p.get("why")):
                body += [self.para("WHY IT MATTERS", "label"), self.para(p.get("why"), "cell"), Spacer(1, 4)]
            if _s(p.get("how")):
                body += [self.para("HOW TO DO IT", "label"), self.para(p.get("how"), "cell")]
            num = Paragraph(f"<font color='{ACCENT}'>{n:02d}</font>", ParagraphStyle("n", fontName=self.fonts[1], fontSize=16, leading=19))
            out.append(self.card([[num, body]], [17 * mm, CONTENT_W - 17 * mm], bar=ACCENT if impact == "high" else "#a5b4fc",
                                 spans=[("LEFTPADDING", (1, 0), (1, -1), 2), ("RIGHTPADDING", (0, 0), (0, -1), 0)]))
        return out

    def s_findings(self) -> list[Any]:
        checks = _dicts(self.r.get("checks"), 300)
        if not checks:
            return []
        cats = {k: _d(v) for k, v in _d(self.r.get("categories")).items()}
        groups: dict[str, list[dict[str, Any]]] = {k: [] for k in cats}
        for c in checks:
            key = _s(c.get("category"))
            groups.setdefault(key if key in cats else "_other", []).append(c)
        counts = {s: sum(1 for c in checks if _s(c.get("status")) == s) for s in ("fail", "warn", "info", "pass")}
        out: list[Any] = [
            self.raw(f"{len(checks)} checks: <font color='{TONE['poor'][0]}'><b>{counts['fail']} to fix</b></font>, "
                     f"<font color='{TONE['ok'][0]}'><b>{counts['warn']} to improve</b></font>, "
                     f"{counts['info']} notes and <font color='{TONE['good'][0]}'><b>{counts['pass']} passing</b></font>.", "body"),
        ]
        for key, items in groups.items():
            if not items:
                continue
            cat = cats.get(key, {})
            label = _s(cat.get("label")) or ("Other checks" if key == "_other" else key.replace("_", " ").title())
            sc = cat.get("score")
            right = ""
            if cat:
                fg = TONE[_tone(sc)][0]
                right = (f"<font color='{fg}'><b>{_fmt(sc)}</b></font><font color='{MUTED}'> / 100</font>" if _num(sc) is not None
                         else f"<font color='{MUTED}'>Not measured</font>") + (f"<font color='{MUTED}'> · {_fmt(cat.get('passed', 0))} of {_fmt(cat.get('total', 0))} passed</font>" if _num(cat.get('total')) else '')
            items.sort(key=lambda c: (STATUS_ORDER.get(_s(c.get("status")), 2), LEVEL_ORDER.get(_s(c.get("priority")), 3)))
            issues = [c for c in items if _s(c.get("status")) != "pass"]
            passing = [c for c in items if _s(c.get("status")) == "pass"]
            head = self.h3(label, right)
            head._head = False  # kept with the area's first block below
            block: list[Any] = [head]
            if _s(cat.get("about")):
                block += [Spacer(1, 3), self.para(cat.get("about"), "small")]
            block.append(Spacer(1, 6))
            body: list[Any] = [self.issue_card(c) for c in issues]
            if passing:
                rows = [[self.pill("pass"), self.para(c.get("title"), "cellb", 200, fit=52 * mm - 10),
                         self.para(c.get("evidence"), "cellm", 400)] for c in passing]
                body += [self.grid(["", "Passing check", "What we found"], rows, [17 * mm, 52 * mm, CONTENT_W - 69 * mm]), Spacer(1, 4)]
            # the heading travels with the first card; with a table, it needs room for a few rows
            if body and not isinstance(body[0], Table):
                out += [CondPageBreak(120), _together([*block, body.pop(0)])]
            else:
                out += [CondPageBreak(170), *block]
            out += body
        return out

    def issue_card(self, c: dict[str, Any]) -> Any:
        status = _s(c.get("status")) or "info"
        fg = TONE[STATUS_TONE.get(status, "none")][0]
        meta = []
        if _s(c.get("priority")):
            meta.append((f"{_clean(c['priority'], 20)} priority", "accent" if _s(c.get("priority")) == "high" else "none"))
        if _s(c.get("effort")):
            meta.append((f"{_clean(c['effort'], 20)} effort", "none"))
        head = Table([[self.pill(status), self.para(c.get("title"), "h4", 200), Chips(meta, upper=True, size=6, right=True) if meta else ""]],
                     colWidths=[19 * mm, CONTENT_W - 19 * mm - 62 * mm - 26, 62 * mm])
        head.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("LEFTPADDING", (0, 0), (-1, -1), 0),
                                  ("RIGHTPADDING", (0, 0), (-1, -1), 0), ("TOPPADDING", (0, 0), (-1, -1), 0),
                                  ("BOTTOMPADDING", (0, 0), (-1, -1), 0)]))
        rows: list[list[Any]] = [[head]]
        if _s(c.get("evidence")):
            rows.append([[self.para("WHAT WE FOUND", "label"), self.para(c.get("evidence"), "cell")]])
        why, fix = _s(c.get("why")), _s(c.get("fix"))
        if why or fix:
            half = (CONTENT_W - 26 - 14) / 2
            t = Table([[[self.para("WHY IT MATTERS", "label"), self.para(why or "—", "cell")], "",
                        [self.para("HOW TO FIX", "label"), self.para(fix or "—", "cell")]]], colWidths=[half, 14, half])
            t.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0),
                                   ("RIGHTPADDING", (0, 0), (-1, -1), 0), ("TOPPADDING", (0, 0), (-1, -1), 0),
                                   ("BOTTOMPADDING", (0, 0), (-1, -1), 0)]))
            rows.append([t])
        foot = []
        pages = [host(p) for p in _strs(c.get("pages"), 6)]
        if pages:
            foot.append("Pages: " + ", ".join(pages))
        agent = self.agent_names.get(_s(c.get("agent")))
        if agent:
            foot.append(f"Handled by: {agent} agent")
        if foot:
            rows.append([self.para("  ·  ".join(foot), "cellm", 500)])
        return self.card(rows, bar=fg, pad=10, spans=[("TOPPADDING", (0, 1), (-1, -1), 5)])

    def s_messaging(self) -> list[Any]:
        m = self.sec["messaging"]
        if not (_s(m.get("suggested_headline")) or _s(m.get("current_headline")) or _s(m.get("clarity_verdict"))):
            return []
        out: list[Any] = []
        score = _num(m.get("clarity_score"))
        if score is not None or _s(m.get("clarity_verdict")):
            left = [self.para("CLARITY", "label"), self.raw(f"<b><font size='20' color='{TONE[_tone((score or 0) * 10)][0]}'>{_fmt(score)}</font></b>"
                                                            f"<font color='{MUTED}'> / 10</font>", "body"), Spacer(1, 6)]
            if score is not None:
                left.append(_meter(score))
            out.append(self.card([[left, [self.para("FIVE-SECOND TEST", "label"), self.para(m.get("clarity_verdict") or "—", "cell")]]],
                                 [40 * mm, CONTENT_W - 40 * mm], bg=SURFACE, border=LINE))
        today = [self.para("ON THE SITE TODAY", "label"), Spacer(1, 3), self.para(m.get("current_headline") or "No clear headline found", "hero", 300)]
        sugg = [self.para("SUGGESTED", "label"), Spacer(1, 3), self.para(m.get("suggested_headline") or "—", "hero", 300)]
        if _s(m.get("suggested_subheadline")):
            sugg.append(self.para(m.get("suggested_subheadline"), "cell", 500))
        if _s(m.get("suggested_cta")):
            sugg += [Spacer(1, 7), Button(_s(m.get("suggested_cta")))]
        half = (CONTENT_W - 12) / 2
        t = Table([[today, "", sugg]], colWidths=[half, 12, half])
        t.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("BACKGROUND", (0, 0), (0, 0), C(SURFACE)),
                               ("BACKGROUND", (2, 0), (2, 0), C(ACCENT_BG)), ("BOX", (0, 0), (0, 0), 0.5, C(LINE)),
                               ("BOX", (2, 0), (2, 0), 0.5, C("#c5d0f5")), ("LINEABOVE", (2, 0), (2, 0), 2.2, C(ACCENT)),
                               ("LINEABOVE", (0, 0), (0, 0), 2.2, C(FAINT)), ("LEFTPADDING", (0, 0), (-1, -1), 12),
                               ("RIGHTPADDING", (0, 0), (-1, -1), 12), ("TOPPADDING", (0, 0), (-1, -1), 11),
                               ("BOTTOMPADDING", (0, 0), (-1, -1), 13), ("LEFTPADDING", (1, 0), (1, 0), 0),
                               ("RIGHTPADDING", (1, 0), (1, 0), 0)]))
        out += [self.h3("Home page hero: today vs suggested"), KeepTogether(t)]
        gaps = _strs(m.get("trust_gaps"), 15)
        if gaps:
            out += [self.h3("Missing proof"), self.para("Evidence a first-time visitor looks for before trusting the business:", "small"),
                    Spacer(1, 4), *self.bullets(gaps, TONE["ok"][0])]
        return out

    def s_keywords(self) -> list[Any]:
        kws = [k for k in _dicts(self.sec["strategy"].get("keyword_opportunities"), 60) if _s(k.get("keyword"))]
        if not kws:
            return []
        tones = {"buy": "good", "transactional": "good", "local": "accent", "compare": "ok", "commercial": "ok", "research": "none", "informational": "none"}
        rows = [[self.para(k.get("keyword"), "cellb", 120, fit=42 * mm - 10), Chips([(_s(k.get("intent")) or "—", tones.get(_s(k.get("intent")).lower(), "none"))], upper=True, size=6.2),
                 self.para(k.get("why"), "cell", 600), self.para(host(_s(k.get("target_page"))), "cellm", 160, fit=42 * mm - 10)] for k in kws]
        return [self.grid(["Search", "Intent", "Why it fits", "Page to rank"], rows, [42 * mm, 23 * mm, CONTENT_W - 107 * mm, 42 * mm]),
                self.para("Search volumes and difficulty need Search Console or a keyword tool connected; they are not estimated here.", "note")]

    def s_page_fixes(self) -> list[Any]:
        fixes = [f for f in _dicts(self.sec["strategy"].get("page_fixes"), 40) if _s(f.get("suggested_title")) or _s(f.get("suggested_description"))]
        out: list[Any] = []
        for f in fixes:
            url = _s(f.get("url"))
            h = host(url)
            domain, _, path = h.partition("/")
            crumbs = " › ".join([domain] + [x for x in path.split("/") if x]) if domain else "page"
            title, desc = _clean(f.get("suggested_title"), 200), _clean(f.get("suggested_description"), 400)
            tl, dl = len(title), len(desc)
            tone_t = "good" if 30 <= tl <= 60 else "ok"
            tone_d = "good" if 70 <= dl <= 160 else "ok"
            rows: list[list[Any]] = [[self.site_line(crumbs)], [self.para(title or "—", "serp_title", 200)], [self.para(desc or "—", "serp_desc", 400)],
                                     [Chips([(f"Title {tl} chars", tone_t), (f"Description {dl} chars", tone_d)], size=6.2)]]
            if _s(f.get("note")):
                rows.append([self.raw(f"<b>Why change it:</b> {_p(f.get('note'), 500)}", "cellm")])
            out.append(self.card(rows, pad=12, spans=[("TOPPADDING", (0, 1), (-1, 1), 6), ("TOPPADDING", (0, 3), (-1, -1), 6),
                                                      ("LINEABOVE", (0, 4), (-1, 4), 0.4, C(LINE)) if len(rows) > 4 else ("TOPPADDING", (0, 0), (0, 0), 12)]))
        if out:
            out.append(self.para("Google shows about 60 characters of a title and 155-160 of a description; longer text is cut off.", "note"))
        return out

    def s_content(self) -> list[Any]:
        plan = [a for a in _dicts(self.sec["creative"].get("content_plan"), 30) if _s(a.get("title"))]
        out: list[Any] = []
        for i, a in enumerate(plan, 1):
            chips = [(f"Piece {i:02d}", "accent")]
            if _s(a.get("type")):
                chips.append((_s(a.get("type")), "none"))
            if _s(a.get("target_keyword")):
                chips.append((f"Target: {_s(a.get('target_keyword'))}", "good"))
            body: list[Any] = [Chips(chips, upper=True, size=6.2), Spacer(1, 6), self.para(a.get("title"), "h4", 250)]
            if _s(a.get("angle")):
                body.append(self.para(a.get("angle"), "cell", 700))
            outline = _strs(a.get("outline"), 12)
            if outline:
                body += [Spacer(1, 4), self.para("OUTLINE", "label"), *self.numbered(outline, "cell")]
            out.append(self.card([[body]], pad=12))
        return out

    def s_campaign(self) -> list[Any]:
        camp = self.sec["campaign"]
        groups = [g for g in _dicts(camp.get("ad_groups"), 15)]
        if not groups:
            return []
        rows = [[self.para(k, "cellb"), self.para(v, "cell")] for k, v in (
            ("Campaign", _clean(camp.get("campaign_name"), 200) or "Google search campaign"),
            ("Goal", _clean(camp.get("goal"), 100).capitalize()),
            ("Budget guidance", _clean(camp.get("budget_note"), 500)),
            ("Ad groups", str(len(groups))),
        ) if v]
        out: list[Any] = [self.grid(["Campaign setting", "Value"], rows, [40 * mm, CONTENT_W - 40 * mm])]
        site = self.site.split("/")[0] or "example.com"
        for gi, g in enumerate(groups, 1):
            heads = _strs(g.get("headlines"), 15)
            descs = _strs(g.get("descriptions"), 4)
            kws = _strs(g.get("keywords"), 30)
            landing = _s(g.get("landing_page"))
            path = "/".join(x for x in host(landing).split("/")[1:3] if x)
            head = self.h3(f"Ad group {gi} · {_clean(g.get('name'), 120) or 'Untitled'}")
            head._head = False  # kept with its previews below
            previews: list[Any] = []
            for pi in range(min(2, max(1, len(descs)))):
                hs = heads[pi * 3: pi * 3 + 3] or heads[:3]
                if not hs and not descs:
                    break
                previews += [self.ad_preview(site, path, hs, descs[pi] if pi < len(descs) else ""), Spacer(1, 6)]
            block: list[Any] = [CondPageBreak(200), _together([head, self.para("AD PREVIEWS", "label"), Spacer(1, 3), *previews])
                                if previews else head]
            if kws:
                block += [Spacer(1, 4), self.para("KEYWORDS", "label"), Spacer(1, 2), Chips([(k, "accent") for k in kws], bold=False, size=7.2), Spacer(1, 6)]
            assets = [[self.para(h, "cell", 120), self.para("Headline", "cellm"), self.limit(len(h), 30)] for h in heads]
            assets += [[self.para(d, "cell", 300), self.para("Description", "cellm"), self.limit(len(d), 90)] for d in descs]
            tail: list[Any] = []
            if assets:
                tail.append(self.grid(["Ad text", "Type", "Length"], assets, [CONTENT_W - 50 * mm, 26 * mm, 24 * mm], right=(2,)))
            if landing:
                tail.append(self.para(f"Landing page: {host(landing)}", "note"))
            # a short inventory stays in one piece; a long one splits with its header repeated
            out += [*block, KeepTogether(tail) if tail and len(assets) <= 12 else Spacer(1, 0), *([] if len(assets) <= 12 else tail)]
        negs = _strs(camp.get("negative_keywords"), 60)
        if negs:
            out += [self.h3("Negative keywords"), self.para("Searches to exclude so the budget is not spent on people who will not buy:", "small"),
                    Spacer(1, 5), Chips([(n, "poor") for n in negs], bold=False, size=7.2)]
        return out

    def limit(self, n: int, cap: int) -> Paragraph:
        fg = TONE["good" if n <= cap else "poor"][0]
        return self.raw(f"<font color='{fg}'><b>{n}</b></font><font color='{MUTED}'> / {cap}</font>", "cellr")

    def site_line(self, crumbs: str) -> Table:
        """Favicon, site name and breadcrumb URL, as in a Google result."""
        letter = (self.biz_name[:1] or "W").upper()
        fav = Paragraph(f"<font color='#ffffff'><b>{_p(letter)}</b></font>",
                        ParagraphStyle("fav", fontName=self.fonts[1], fontSize=8, leading=10, alignment=TA_CENTER))
        fav_t = Table([[fav]], colWidths=[16], rowHeights=[16])
        fav_t.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), C(ACCENT)), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                                   ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                                   ("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
                                   ("ROUNDEDCORNERS", [8, 8, 8, 8])]))
        t = Table([[fav_t, [self.para(self.biz_name, "serp_url", 80), self.para(crumbs, "small", 200)]]], colWidths=[24, CONTENT_W - 60])
        t.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("LEFTPADDING", (0, 0), (-1, -1), 0),
                               ("RIGHTPADDING", (0, 0), (-1, -1), 0), ("TOPPADDING", (0, 0), (-1, -1), 0),
                               ("BOTTOMPADDING", (0, 0), (-1, -1), 0)]))
        return t

    def ad_preview(self, site: str, path: str, heads: list[str], desc: str) -> Any:
        crumbs = "https://" + site + ("".join(" › " + x for x in path.split("/") if x) if path else "")
        rows = [[self.raw(f"<font color='{SERP_DARK}'><b>Sponsored</b></font>", "serp_url")], [self.site_line(crumbs)],
                [self.para(" | ".join(heads) or "—", "serp_title", 200)]]
        if desc:
            rows.append([self.para(desc, "serp_desc", 300)])
        return self.card(rows, pad=12, border="#dadce0", spans=[("TOPPADDING", (0, 1), (-1, -1), 5)], keep=False)

    def s_social(self) -> list[Any]:
        cr = self.sec["creative"]
        posts = [p for p in _dicts(cr.get("social_posts"), 20) if _s(p.get("text"))]
        emails = [e for e in _dicts(cr.get("email_campaigns"), 12) if _s(e.get("subject")) or _s(e.get("name"))]
        angles = [a for a in _dicts(cr.get("ad_creative_angles"), 15) if _s(a.get("angle")) or _s(a.get("hook"))]
        out: list[Any] = []
        networks = {"linkedin": ("in", "#0a66c2"), "facebook": ("f", "#1877f2"), "instagram": ("IG", "#c13584"),
                    "x": ("X", "#111111"), "twitter": ("X", "#111111"), "tiktok": ("TT", "#111111"), "youtube": ("YT", "#ff0000"),
                    "google business profile": ("G", "#34a853"), "pinterest": ("P", "#e60023"), "threads": ("@", "#111111")}
        if posts:
            out.append(self.h3(f"Social posts ({len(posts)})"))
            for p in posts:
                net = _clean(p.get("network"), 40) or "Social"
                mark, color = networks.get(net.lower(), (net[:1].upper(), ACCENT))
                badge = Table([[Paragraph(f"<font color='#ffffff'><b>{_p(mark)}</b></font>",
                                          ParagraphStyle("b", fontName=self.fonts[1], fontSize=8, leading=10, alignment=TA_CENTER))]],
                              colWidths=[20], rowHeights=[20])
                badge.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), C(color)), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                                           ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                                           ("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
                                           ("ROUNDEDCORNERS", [4, 4, 4, 4])]))
                head = Table([[badge, [self.para(net, "h4", 40), self.para(f"Draft post for {self.biz_name}", "small")]]], colWidths=[28, CONTENT_W - 60])
                head.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("LEFTPADDING", (0, 0), (-1, -1), 0),
                                          ("RIGHTPADDING", (0, 0), (-1, -1), 0), ("TOPPADDING", (0, 0), (-1, -1), 0),
                                          ("BOTTOMPADDING", (0, 0), (-1, -1), 0)]))
                rows: list[list[Any]] = [[head], [self.para(p.get("text"), "cell", 1400)]]
                tags = " ".join("#" + t.lstrip("#") for t in _strs(p.get("hashtags"), 12))
                if tags:
                    rows.append([self.raw(f"<font color='{ACCENT}'>{_p(tags)}</font>", "cell")])
                if _s(p.get("visual_idea")):
                    rows.append([self.raw(f"<b>Visual idea:</b> {_p(p.get('visual_idea'), 400)}", "cellm")])
                out.append(self.card(rows, pad=12, spans=[("TOPPADDING", (0, 1), (-1, -1), 6)]))
        if emails:
            out.append(self.h3(f"Email campaigns ({len(emails)})"))
            for e in emails:
                name = _clean(e.get("name"), 60)
                meta = Table([[self.para("From", "cellm"), self.para(self.biz_name, "cellb")],
                              [self.para("Subject", "cellm"), self.para(e.get("subject") or "—", "cellb", 200)],
                              [self.para("Preview", "cellm"), self.para(e.get("preview_text") or "—", "cellm", 250)]],
                             colWidths=[18 * mm, CONTENT_W - 18 * mm - 26])
                meta.setStyle(TableStyle([("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                                          ("TOPPADDING", (0, 0), (-1, -1), 1.5), ("BOTTOMPADDING", (0, 0), (-1, -1), 1.5)]))
                rows = [[Chips([(f"Email · {name}" if name else "Email", "accent")], upper=True, size=6.2)], [meta]]
                outline = _strs(e.get("body_outline"), 12)
                if outline:
                    rows.append([[self.para("BODY OUTLINE", "label"), *self.numbered(outline, "cell")]])
                if _s(e.get("cta")):
                    rows.append([Button(_s(e.get("cta")), 8)])
                out.append(self.card(rows, pad=12, spans=[("TOPPADDING", (0, 1), (-1, -1), 7),
                                                          ("LINEBELOW", (0, 1), (-1, 1), 0.4, C(LINE)),
                                                          ("BOTTOMPADDING", (0, 1), (-1, 1), 8)]))
        if angles:
            rows = [[self.para(a.get("angle"), "cellb", 150), self.para(a.get("hook"), "cell", 400), self.para(a.get("audience"), "cellm", 300)] for a in angles]
            out += [self.h3("Ad creative angles"), self.grid(["Angle", "Hook", "Audience"], rows, [42 * mm, CONTENT_W - 94 * mm, 52 * mm])]
        return out

    def s_aisearch(self) -> list[Any]:
        st = self.sec["strategy"]
        faq = [f for f in _dicts(st.get("faq_suggestions"), 20) if _s(f.get("q"))]
        comps = [c for c in _dicts(st.get("competitors_to_research"), 15) if _s(c.get("name"))]
        local = _strs(st.get("local_actions"), 20)
        if not (faq or comps or local or st):
            return []
        out: list[Any] = []
        if faq:
            out += [self.h3("Questions to answer on the site"),
                    self.para("Answering these plainly (ideally with FAQ schema) helps ChatGPT, Gemini and Perplexity quote the site.", "small"), Spacer(1, 6)]
            qs = ParagraphStyle("q", fontName=self.fonts[1], fontSize=10, leading=12, textColor=C(ACCENT))
            for f in faq:
                t = Table([[Paragraph("Q", qs), self.para(f.get("q"), "h4", 300)],
                           [Paragraph(f"<font color='{FAINT}'>A</font>", qs), self.para(f.get("a"), "cell", 1000)]],
                          colWidths=[16, CONTENT_W - 16])
                t.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0),
                                       ("RIGHTPADDING", (0, 0), (-1, -1), 0), ("TOPPADDING", (0, 0), (-1, -1), 1),
                                       ("BOTTOMPADDING", (0, 0), (-1, -1), 2), ("LINEBELOW", (0, -1), (-1, -1), 0.4, C(LINE)),
                                       ("BOTTOMPADDING", (0, -1), (-1, -1), 9)]))
                out += [KeepTogether([t]), Spacer(1, 7)]
        if comps:
            rows = [[self.para(c.get("name"), "cellb", 100, fit=45 * mm - 10), self.para(c.get("why"), "cell", 600)] for c in comps]
            out += [self.h3("Competitors to research"), Chips([("Suggested, not measured", "ai")], upper=True, size=6.4), Spacer(1, 6),
                    self.grid(["Competitor", "Why study them"], rows, [45 * mm, CONTENT_W - 45 * mm]),
                    self.para("Named by AI from the site's content and market; rankings and traffic were not measured.", "note")]
        out.append(self.h3("Local search actions"))
        if local:
            out += self.bullets(local)
        else:
            out.append(self.empty("None suggested: the business does not appear to serve a specific local area."))
        return out

    def s_roadmap(self) -> list[Any]:
        road = self.sec["roadmap"]
        phases = [("Days 1-30", _strs(road.get("first_30_days"), 15)), ("Days 31-60", _strs(road.get("days_31_60"), 15)),
                  ("Days 61-90", _strs(road.get("days_61_90"), 15))]
        if not any(items for _, items in phases):
            return []
        gap = 10
        cw = (CONTENT_W - 2 * gap) / 3
        subs = ("Foundations", "Build", "Scale")
        out: list[Any] = [_timeline([(t, f"{subs[i]} · {len(items)} actions") for i, (t, items) in enumerate(phases)], CONTENT_W, cw, gap), Spacer(1, 8)]
        n = max(len(items) for _, items in phases)
        num = ParagraphStyle("rn", fontName=self.fonts[1], fontSize=7.4, leading=11.2, textColor=C(ACCENT))
        rows = []
        for i in range(n):
            row: list[Any] = []
            for pi, (_, items) in enumerate(phases):
                if i < len(items):
                    cell = Table([[Paragraph(f"{i + 1:02d}", num), self.para(items[i], "cell", 500, fit=cw - 32)]], colWidths=[16, cw - 16 - 16])
                    cell.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0),
                                              ("RIGHTPADDING", (0, 0), (-1, -1), 0), ("TOPPADDING", (0, 0), (-1, -1), 0),
                                              ("BOTTOMPADDING", (0, 0), (-1, -1), 0)]))
                    row.append(cell)
                else:
                    row.append("")
                if pi < 2:
                    row.append("")
            rows.append(row)
        t = Table(rows, colWidths=[cw, gap, cw, gap, cw])
        cmds: list[Any] = [("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 8),
                           ("RIGHTPADDING", (0, 0), (-1, -1), 8), ("TOPPADDING", (0, 0), (-1, -1), 7),
                           ("BOTTOMPADDING", (0, 0), (-1, -1), 7)]
        for ci, (_, items) in zip((0, 2, 4), phases):
            if items:
                cmds += [("BACKGROUND", (ci, 0), (ci, len(items) - 1), C(SURFACE)),
                         ("LINEBELOW", (ci, 0), (ci, len(items) - 2), 0.4, C(LINE)) if len(items) > 1 else ("LEFTPADDING", (ci, 0), (ci, 0), 8),
                         ("LINEABOVE", (ci, 0), (ci, 0), 2.2, C(("#3b5bdb", "#5c7cfa", "#91a7ff")[ci // 2]))]
        for ci in (1, 3):
            cmds += [("LEFTPADDING", (ci, 0), (ci, -1), 0), ("RIGHTPADDING", (ci, 0), (ci, -1), 0)]
        t.setStyle(TableStyle(cmds))
        out.append(t)
        return out

    def s_automation(self) -> list[Any]:
        auto = [a for a in _dicts(self.r.get("automation"), 30) if _s(a.get("name"))]
        if not auto:
            return []
        rows = []
        for a in auto:
            addr = _strs(a.get("addresses"), 12)
            rows.append([self.para(a.get("name"), "cellb", 80),
                         [self.para(a.get("what_it_does"), "cell", 500), Spacer(1, 4),
                          Chips([(x, "accent") for x in addr], bold=False, size=6.6) if addr else Spacer(1, 0)]])
        return [self.grid(["Agent", "What it does · findings it works on"], rows, [48 * mm, CONTENT_W - 48 * mm])]

    def s_context(self) -> list[Any]:
        ctx = _d(self.x.get("context"))
        if not ctx:
            return []
        out: list[Any] = []
        conns = _dicts(ctx.get("connectors"), 40)
        health = {"healthy": ("Healthy", "good"), "ok": ("Healthy", "good"), "failing": ("Problem", "poor"), "error": ("Problem", "poor"),
                  "degraded": ("Degraded", "ok"), "unknown": ("Unknown", "none")}
        out.append(self.h3(f"Integrations ({len(conns)})"))
        if conns:
            rows = []
            for c in conns:
                label, tone = health.get(_s(c.get("health")).lower(), (_s(c.get("health")).title() or "Connected", "none"))
                rows.append([self.para(c.get("name"), "cellb", 80), self.para(_s(c.get("category")).replace("_", " ").capitalize() or "—", "cell", 60),
                             Chips([(label, tone)], upper=True, size=6.4), self.para(c.get("last_error") or "—", "cellm", 300)])
            out.append(self.grid(["Integration", "Category", "Health", "Last error"], rows, [44 * mm, 32 * mm, 26 * mm, CONTENT_W - 102 * mm]))
        else:
            out.append(self.empty("Nothing was connected."))
        agents = _dicts(ctx.get("agents"), 40)
        if agents:
            state = {"running": ("On", "good"), "paused": ("Paused", "ok"), "error": ("Problem", "poor")}
            rows = []
            for a in agents:
                label, tone = state.get(_s(a.get("status")), (_s(a.get("status")).title() or "—", "none"))
                conf = a.get("configured")
                rows.append([self.para(a.get("name"), "cellb", 80), Chips([(label, tone)], upper=True, size=6.4),
                             self.para("Yes" if conf is True else "No" if conf is False else "—", "cell"),
                             self.para(_date(_s(a.get("last_run_at"))) or "Not yet", "cell")])
            out += [self.h3(f"Agents ({len(agents)})"), self.grid(["Agent", "Status", "Set up", "Last run"], rows,
                                                                 [CONTENT_W - 90 * mm, 26 * mm, 24 * mm, 40 * mm])]
        llm = _d(ctx.get("llm"))
        ps = ctx.get("pagespeed")
        facts = []
        if _s(llm.get("name")):
            facts.append(("AI model", _s(llm.get("name"))))
        if ps is not None:
            facts.append(("Google PageSpeed", ("Connected" if ps else "Not connected") if isinstance(ps, bool) else
                          _s(_d(ps).get("status") or _d(ps).get("name")) or _s(ps) or "—"))
        if facts:
            out += [self.h3("Services"), self.grid(["Service", "State"], [[self.para(k, "cellb"), self.para(v, "cell")] for k, v in facts],
                                                   [50 * mm, CONTENT_W - 50 * mm])]
        return out

    def s_method(self) -> list[Any]:
        summary, ai = _d(self.r.get("summary")), self.sec["ai"]
        speed = _d(_d(self.r.get("measured")).get("pagespeed"))
        measured = [
            f"{_fmt(summary.get('pages_read', 0))} pages read and {_fmt(summary.get('links_checked', 0))} links checked on {self.when or 'the run date'}.",
            f"{_fmt(summary.get('checks', len(_l(self.r.get('checks')))))} automated checks; every score in the Scorecard and Detailed findings comes from them.",
            "Google Lighthouse (PageSpeed) measured on mobile." if speed.get("measured") else "Google Lighthouse was not available for this run, so speed is left unscored.",
        ]
        model = _clean(ai.get("model"), 80)
        usage = _d(ai.get("usage"))
        written = [
            f"Written by {model}." if model else "No AI model output was available for this run.",
            "Business profile, executive summary, priorities, messaging, keywords, titles, content, ads, posts, emails, competitors and roadmap.",
            "Grounded in the site's own text; drafts to review before publishing, not measurements.",
        ]
        if _num(usage.get("input_tokens")) is not None:
            written.append(f"{_fmt(usage.get('input_tokens'))} input and {_fmt(usage.get('output_tokens'))} output tokens.")
        left = [Chips([("Measured", "measured")], upper=True, size=6.6), Spacer(1, 7), *self.bullets(measured, MEAS_FG, "cell")]
        right = [Chips([("AI suggestion", "ai")], upper=True, size=6.6), Spacer(1, 7), *self.bullets(written, AI_FG, "cell")]
        half = (CONTENT_W - 12) / 2
        t = Table([[left, "", right]], colWidths=[half, 12, half])
        t.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("BACKGROUND", (0, 0), (0, 0), C(MEAS_BG)),
                               ("BACKGROUND", (2, 0), (2, 0), C(AI_BG)), ("LEFTPADDING", (0, 0), (-1, -1), 12),
                               ("RIGHTPADDING", (0, 0), (-1, -1), 12), ("TOPPADDING", (0, 0), (-1, -1), 12),
                               ("BOTTOMPADDING", (0, 0), (-1, -1), 10), ("LEFTPADDING", (1, 0), (1, 0), 0),
                               ("RIGHTPADDING", (1, 0), (1, 0), 0)]))
        out: list[Any] = [KeepTogether(t)]
        run = [("Website", self.site), ("Analysed", _clean(_s(self.r.get("generated_at"))[:19].replace("T", " "), 40) + (" UTC" if _s(self.r.get("generated_at")) else "")),
               ("Analysis time", f"{_fmt(self.r.get('duration_s'))} seconds" if _num(self.r.get("duration_s")) is not None else ""),
               ("Prepared for", self.workspace), ("Prepared by", self.brand)]
        out += [self.h3("Run details"), self.grid(["Item", "Value"], [[self.para(k, "cellb"), self.para(v, "cell")] for k, v in run if v],
                                                  [40 * mm, CONTENT_W - 40 * mm])]
        notes = _strs(self.r.get("notes"), 20)
        if notes:
            out += [self.h3("Notes"), *self.bullets(notes, FAINT)]
        out += [Spacer(1, 10), self.para(
            "This report reflects the pages that could be read on the run date. Search rankings, traffic, backlinks and map positions "
            "are not measured unless the matching account is connected. AI-written content should be reviewed before it is published.", "note")]
        return out

    # contents ----------------------------------------------------------------
    def contents(self, sections: list[tuple[str, str, str, list[Any]]], marks: dict[int, int]) -> list[Any]:
        out: list[Any] = [Paragraph("REPORT CONTENTS", ParagraphStyle("k", fontName=self.fonts[1], fontSize=7.5, leading=10, textColor=C(ACCENT))),
                          Spacer(1, 6), Paragraph("Contents", self.st["sec_title"]), Spacer(1, 10), Rule(34, 3, ACCENT, 0), Spacer(1, 18)]
        for i, (title, tag, _lede, _f) in enumerate(sections, 1):
            row = TocRow(i, title, tag, marks.get(i), f"sec{i}" if i in marks else None)
            row.row_h = 31 if len(sections) <= 14 else 26.5
            out.append(row)
        legend = [
            [self.para("STATUS", "label"), Chips([("Fix", "poor"), ("Improve", "ok"), ("Note", "none"), ("Good", "good")], upper=True, size=6.4),
             self.para("Fix: failing and worth doing now. Improve: works, but below best practice. Note: for information. Good: passing.", "cellm")],
            [self.para("SCORES", "label"), Chips([("80+ strong", "good"), ("55-79 needs work", "ok"), ("Below 55 weak", "poor")], upper=True, size=6.4),
             self.para("Scores are out of 100. An area with nothing measurable is shown as Not measured, never estimated.", "cellm")],
            [self.para("SOURCE", "label"), Chips([("Measured", "measured"), ("AI suggestion", "ai")], upper=True, size=6.4),
             self.para("Measured sections come from reading the live site. AI suggestions are drafts written from the site's text.", "cellm")],
        ]
        t = Table(legend, colWidths=[18 * mm, 72 * mm, CONTENT_W - 90 * mm - 24])
        t.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("LEFTPADDING", (0, 0), (-1, -1), 0),
                               ("RIGHTPADDING", (0, 0), (-1, -1), 8), ("TOPPADDING", (0, 0), (-1, -1), 5),
                               ("BOTTOMPADDING", (0, 0), (-1, -1), 5)]))
        out += [Spacer(1, 16), CondPageBreak(130), self.card([[self.para("How to read this report", "h4")], [t]], bg=SURFACE, border=SURFACE, pad=12)]
        return out

    def story(self, marks: dict[int, int]) -> list[Any]:
        sections = self.sections()
        out: list[Any] = [NextPageTemplate("body"), PageBreak(), *self.contents(sections, marks)]
        for i, (title, tag, lede, flow) in enumerate(sections, 1):
            out += [NextPageTemplate("body"), PageBreak(), SectionHead(i, title, tag, lede, self.st), *_keep_heads(flow)]
        return out

    # page furniture --------------------------------------------------------
    def draw_cover(self, c: Any, doc: Any) -> None:
        from reportlab.graphics import renderPDF

        reg, bold, ital = self.fonts[:3]
        W, H = PAGE_W, PAGE_H
        band = 352
        c.saveState()
        c.setFillColor(C(NAVY))
        c.rect(0, H - band, W, band, stroke=0, fill=1)
        c.setStrokeColor(colors.white)
        c.setStrokeAlpha(0.06)
        c.setLineWidth(0.8)
        for r in range(90, 470, 46):
            c.circle(W - 30, H - 70, r, stroke=1, fill=0)
        c.setStrokeAlpha(1)
        c.setFillColor(C(ACCENT))
        c.rect(0, H - band - 5, W, 5, stroke=0, fill=1)
        # brand
        x0 = MARGIN + 4
        c.setFillColor(C(ACCENT))
        c.roundRect(x0, H - 66, 22, 22, 5, stroke=0, fill=1)
        c.setStrokeColor(colors.white)
        c.setLineWidth(1.8)
        c.setLineCap(1)
        c.setLineJoin(1)
        p = c.beginPath()
        p.moveTo(x0 + 5, H - 60)
        p.lineTo(x0 + 10, H - 54)
        p.lineTo(x0 + 13, H - 57)
        p.lineTo(x0 + 17.5, H - 50)
        c.drawPath(p, stroke=1, fill=0)
        c.setFillColor(colors.white)
        c.setFont(bold, 12.5)
        c.drawString(x0 + 30, H - 59.5, self.brand)
        c.setFillColor(C("#aab4d4"))
        c.setFont(reg, 8.5)
        c.drawRightString(W - MARGIN - 4, H - 59, self.when)
        # title block
        t = c.beginText(x0, H - 150)
        t.setFont(bold, 9)
        t.setCharSpace(2.2)
        t.setFillColor(C(ACCENT_SOFT))
        t.textOut("WEBSITE GROWTH ANALYSIS")
        t.setCharSpace(0)
        c.drawText(t)
        title = Paragraph(_p(self.biz_name, 80), ParagraphStyle("ct", fontName=bold, fontSize=36, leading=41, textColor=colors.white))
        _, th = title.wrap(W - 2 * MARGIN - 40, 200)
        title.drawOn(c, x0, H - 168 - th)
        y = H - 168 - th - 26
        c.setFillColor(C("#d5dbf0"))
        c.setFont(reg, 13)
        c.drawString(x0, y, _fit(_clean(self.site, 120), reg, 13, W - 2 * MARGIN - 20))
        industry = _clean(self.sec["business"].get("industry"), 80)
        if industry:
            y -= 20
            c.setFillColor(C("#aab4d4"))
            c.setFont(reg, 9.5)
            c.drawString(x0, y, _fit(industry, reg, 9.5, W - 2 * MARGIN - 20))
        if self.workspace:
            label = f"Prepared for {self.workspace}"
            lw = _width(label, bold, 8.5) + 20
            c.setFillColor(colors.white)
            c.setFillAlpha(0.1)
            c.roundRect(x0, H - band + 34, lw, 20, 10, stroke=0, fill=1)
            c.setFillAlpha(1)
            c.setFillColor(colors.white)
            c.setFont(bold, 8.5)
            c.drawString(x0 + 10, H - band + 40.5, label)
        # score panel
        overall, summary = _d(self.r.get("overall")), _d(self.r.get("summary"))
        top = H - band - 5 - 44
        c.setFillColor(C(INK))
        c.setFont(bold, 11)
        c.drawString(x0, top, "Overall score")
        c.setFillColor(C(MUTED))
        c.setFont(reg, 8.5)
        c.drawString(x0, top - 14, "Measured across every area of the site")
        g = _gauge(overall.get("score"), 178, grade=_clean(overall.get("grade"), 4), thickness=15)
        renderPDF.draw(g, c, x0 - 4, top - 30 - 178)
        tiles = [
            (summary.get("failed", 0), "To fix", "poor", "failing checks"),
            (summary.get("warnings", 0), "To improve", "ok", "below best practice"),
            (summary.get("passed", 0), "Passed", "good", "in good shape"),
            (summary.get("pages_read", 0), "Pages read", "accent", f"{_fmt(summary.get('links_checked', 0))} links checked"),
        ]
        gx = x0 + 205
        tw = (W - MARGIN - 4 - gx - 12) / 2
        tile_h = 82
        for i, (val, label, tone, sub) in enumerate(tiles):
            tx = gx + (i % 2) * (tw + 12)
            ty = top - 30 - (i // 2 + 1) * tile_h - (i // 2) * 12 + 4
            c.setFillColor(C(SURFACE))
            c.setStrokeColor(C(LINE))
            c.setLineWidth(0.6)
            c.roundRect(tx, ty, tw, tile_h, 6, stroke=1, fill=1)
            c.setFillColor(C(TONE[tone][0]))
            c.roundRect(tx, ty + tile_h - 3, tw, 3, 1.5, stroke=0, fill=1)
            c.setFillColor(C(MUTED))
            c.setFont(bold, 7)
            c.drawString(tx + 12, ty + tile_h - 20, label.upper())
            c.setFillColor(C(TONE[tone][0]))
            c.setFont(bold, 26)
            c.drawString(tx + 12, ty + 26, _fmt(val))
            c.setFillColor(C(MUTED))
            c.setFont(reg, 7.5)
            c.drawString(tx + 12, ty + 12, _fit(sub, reg, 7.5, tw - 20))
        # key facts strip
        m = _d(self.r.get("measured"))
        facts = [(_fmt(summary.get("checks", len(_l(self.r.get("checks"))))), "checks run"),
                 (_fmt(len(_d(self.r.get("categories")))), "areas scored"),
                 (_clean(m.get("platform"), 30) or "—", "platform"),
                 (f"{_fmt(self.r.get('duration_s'))} s" if _num(self.r.get("duration_s")) is not None else "—", "analysis time")]
        fy = 140
        c.setStrokeColor(C(LINE))
        c.setLineWidth(0.6)
        c.line(x0, fy + 38, W - MARGIN - 4, fy + 38)
        fw = (W - MARGIN - 4 - x0) / 4
        for i, (v, label) in enumerate(facts):
            fx = x0 + i * fw
            c.setFillColor(C(INK))
            c.setFont(bold, 13)
            c.drawString(fx, fy + 14, _fit(v, bold, 13, fw - 10))
            c.setFillColor(C(MUTED))
            c.setFont(reg, 7.5)
            c.drawString(fx, fy, label.upper())
            if i:
                c.line(fx - 10, fy - 2, fx - 10, fy + 28)
        # footer
        c.setStrokeColor(C(LINE))
        c.line(x0, 70, W - MARGIN - 4, 70)
        c.setFillColor(C(MUTED))
        c.setFont(reg, 7.5)
        left = f"Confidential — prepared for {self.workspace}" if self.workspace else f"Prepared by {self.brand}"
        c.drawString(x0, 56, left)
        c.drawRightString(W - MARGIN - 4, 56, f"Generated {self.when}" if self.when else "")
        c.setFont(ital, 7)
        c.setFillColor(C(FAINT))
        c.drawString(x0, 44, "Scores are measured from the live site. Sections marked AI suggestion are drafts written by AI from the site's text.")
        c.restoreState()

    def draw_chrome(self, c: Any, doc: Any, total: int) -> None:
        reg, bold = self.fonts[:2]
        W, H = PAGE_W, PAGE_H
        c.saveState()
        c.setFillColor(C(ACCENT))
        c.roundRect(MARGIN, H - 13.6 * mm, 7, 7, 1.6, stroke=0, fill=1)
        c.setFillColor(C(INK))
        c.setFont(bold, 7.8)
        c.drawString(MARGIN + 11, H - 13.6 * mm + 1, self.brand)
        bw = _width(self.brand, bold, 7.8)
        c.setFillColor(C(MUTED))
        c.setFont(reg, 7.8)
        c.drawString(MARGIN + 11 + bw + 6, H - 13.6 * mm + 1, "|   Website Growth Analysis")
        c.drawRightString(W - MARGIN, H - 13.6 * mm + 1, _fit(_clean(f"{self.biz_name}  ·  {self.site}" if self.biz_name != self.site else self.site, 120), reg, 7.8, 230))
        c.setStrokeColor(C(LINE))
        c.setLineWidth(0.6)
        c.line(MARGIN, H - 16 * mm, W - MARGIN, H - 16 * mm)
        c.line(MARGIN, 13 * mm, W - MARGIN, 13 * mm)
        left = f"Confidential — prepared for {self.workspace}" if self.workspace else f"Generated {self.when}" if self.when else self.brand
        c.drawString(MARGIN, 8.8 * mm, _fit(left, reg, 7.8, 300))
        c.setFillColor(C(INK))
        c.setFont(bold, 7.8)
        c.drawRightString(W - MARGIN, 8.8 * mm, f"Page {doc.page} of {total}" if total else f"Page {doc.page}")
        c.restoreState()


def _together(items: list[Any]) -> KeepTogether:
    """KeepTogether without nesting (a nested one reports a huge height and forces a page break)."""
    flat: list[Any] = []
    for f in items:
        flat += f._content if isinstance(f, KeepTogether) else [f]
    return KeepTogether(flat)


def _keep_heads(flow: list[Any]) -> list[Any]:
    """No heading stranded at a page bottom.

    Every sub-heading needs room for some content below it; one followed by a
    block that cannot split (a card) travels with that block. A following
    table may split, and repeats its header row on the next page.
    """
    out: list[Any] = []
    i = 0
    while i < len(flow):
        f = flow[i]
        if getattr(f, "_head", False):
            out.append(CondPageBreak(150))
            nxt = flow[i + 1] if i + 1 < len(flow) else None
            short_table = isinstance(nxt, Table) and len(getattr(nxt, "_cellvalues", ())) <= 9
            if nxt is not None and (short_table or not isinstance(nxt, (Table, CondPageBreak))) and not getattr(nxt, "_head", False):
                out.append(_together([f, nxt]))
                i += 2
                continue
        out.append(f)
        i += 1
    return out


class _Doc(BaseDocTemplate):
    def afterFlowable(self, flowable: Any) -> None:
        toc = getattr(flowable, "toc", None)
        if toc:
            num, title, _tag = toc
            self.marks[num] = self.page
            key = f"sec{num}"
            self.canv.bookmarkPage(key)
            self.canv.addOutlineEntry(f"{num}. {_clean(title)}", key, level=0)


def _build(rep: _Report, marks_in: dict[int, int], total: int) -> tuple[bytes, dict[int, int], int]:
    buf = io.BytesIO()
    doc = _Doc(buf, pagesize=A4, leftMargin=MARGIN, rightMargin=MARGIN, topMargin=24 * mm, bottomMargin=18 * mm,
               title=f"Website Growth Analysis - {rep.biz_name}", author=rep.brand, subject=rep.site, creator=rep.brand)
    doc.marks = {}
    cover = Frame(0, 0, PAGE_W, PAGE_H, id="cover", leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0)
    body = Frame(MARGIN, 18 * mm, CONTENT_W, PAGE_H - 42 * mm, id="body", leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0)
    doc.addPageTemplates([
        PageTemplate("cover", [cover], onPage=rep.draw_cover),
        PageTemplate("body", [body], onPage=lambda c, d: rep.draw_chrome(c, d, total)),
    ])
    doc.build(rep.story(marks_in))
    return buf.getvalue(), dict(doc.marks), doc.page


def _fancy(report: dict[str, Any], extra: dict[str, Any]) -> bytes:
    rep = _Report(report, extra)
    marks: dict[int, int] = {}
    total = 0
    pdf = b""
    for _ in range(3):  # pass 1 learns page numbers; pass 2 prints them; a 3rd only if they moved
        pdf, new_marks, pages = _build(rep, marks, total)
        if new_marks == marks and pages == total:
            break
        marks, total = new_marks, pages
    return pdf


def _plain(report: dict[str, Any]) -> bytes:
    """Last-resort layout: plain paragraphs, no tables or charts."""
    from reportlab.platypus import SimpleDocTemplate

    reg, bold = _fonts()[:2]
    body = ParagraphStyle("b", fontName=reg, fontSize=9.5, leading=13)
    head = ParagraphStyle("h", fontName=bold, fontSize=14, leading=18, spaceBefore=10, spaceAfter=4)
    story: list[Any] = [Paragraph(_p(f"Website analysis - {host(_s(report.get('website')))}"), head)]
    overall = _d(report.get("overall"))
    story.append(Paragraph(_p(f"Overall score: {_fmt(overall.get('score'))} ({_clean(overall.get('grade'), 4) or '-'})"), body))
    for c in _l(report.get("checks"))[:300]:
        c = _d(c)
        story.append(Paragraph(_p(f"[{STATUS_LABEL.get(_s(c.get('status')), _s(c.get('status')))}] {_s(c.get('title'))}: {_s(c.get('evidence'))}", 800), body))
    buf = io.BytesIO()
    SimpleDocTemplate(buf, pagesize=A4).build(story)
    return buf.getvalue()


def to_pdf(report: dict[str, Any], *, extra: dict[str, Any] | None = None) -> bytes:
    report = report if isinstance(report, dict) else {}
    extra = extra if isinstance(extra, dict) else {}
    try:
        return _fancy(report, extra)
    except Exception:  # noqa: BLE001 - a download must never fail
        log.exception("analysis pdf: full layout failed, falling back to the plain layout")
    try:
        return _plain(report)
    except Exception:  # noqa: BLE001
        log.exception("analysis pdf: plain layout failed too")
        from reportlab.pdfgen.canvas import Canvas

        buf = io.BytesIO()
        c = Canvas(buf, pagesize=A4)
        c.drawString(72, PAGE_H - 72, "Website analysis: the report could not be laid out. Please download the HTML version.")
        c.save()
        return buf.getvalue()
