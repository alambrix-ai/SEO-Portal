"""Deterministic QA run on every creative before it reaches a reviewer.

Two groups of checks, all pure functions with no model call:

* **What the copy says.** The ad-platform rules shared with the Dynamic
  Creative Optimizer (``specs.check_text``: unprovable promises, an all-caps
  headline, exclamation marks), the brand kit's banned phrases, and number
  grounding: every number in the copy must appear in the product's own data,
  so a model cannot invent "50% off" or "10,000 customers" (LLM09).
* **How it looks.** WCAG contrast, whether text fits its box (measured with
  Pillow's font metrics), logo size and clear space, safe zones, and the
  format's file-size cap.

Results go into ``CreativeJob.outputs``, which holds ids and numbers only, so
an issue carries a fixed code and a fixed plain-English message and never
quotes the copy it was raised about.
"""
from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from functools import lru_cache
from typing import Any, Literal

from PIL import ImageFont

from app.agents.dynamic_creative_optimizer.specs import check_text
from app.models.creative import normalise_hex
from app.services.creative.fonts import get_font, pillow_font_path
from app.services.creative.formats import BANNER_HEADLINE_WORDS, PRINT_DPI, FormatSpec
from app.services.creative.schema import CreativeCopy, word_count

Severity = Literal["error", "warning"]


@dataclass(frozen=True, slots=True)
class QAIssue:
    code: str
    #: Fixed wording for a marketer; never contains the copy itself.
    message: str
    severity: Severity = "error"


@dataclass(frozen=True, slots=True)
class QAReport:
    issues: tuple[QAIssue, ...] = ()

    @property
    def passed(self) -> bool:
        return not any(i.severity == "error" for i in self.issues)

    def to_dict(self) -> dict[str, Any]:
        """The shape stored in ``CreativeJob.outputs``."""
        return {
            "passed": self.passed,
            "issues": [
                {"code": i.code, "severity": i.severity, "message": i.message} for i in self.issues
            ],
        }


def build_report(issues: Iterable[QAIssue | None]) -> QAReport:
    return QAReport(tuple(i for i in issues if i is not None))


# ── What the copy says ─────────────────────────────────────────────────────
@dataclass(frozen=True, slots=True)
class ProductFacts:
    """The product data copy is grounded against."""

    name: str = ""
    description: str = ""
    price_text: str = ""
    approved_claims: tuple[str, ...] = ()
    disclaimer: str = ""

    @classmethod
    def from_product(cls, product: Any) -> ProductFacts:  # noqa: ANN401 - ORM row or stand-in
        claims = getattr(product, "approved_claims", None) or []
        return cls(
            name=str(getattr(product, "name", "") or ""),
            description=str(getattr(product, "description", "") or ""),
            price_text=str(getattr(product, "price_text", "") or ""),
            approved_claims=tuple(c for c in claims if isinstance(c, str)),
            disclaimer=str(getattr(product, "disclaimer", "") or ""),
        )

    def text(self) -> str:
        return " ".join(
            (self.name, self.description, self.price_text, *self.approved_claims, self.disclaimer)
        )


#: Bounds on brand-kit input so matching stays cheap whatever was saved.
MAX_BANNED_PHRASES = 200
MAX_BANNED_PHRASE_CHARS = 100

_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")
_SPACES = re.compile(r"\s+")
_APOSTROPHES = str.maketrans({"‘": "'", "’": "'", "ʼ": "'"})

_CHECK_TEXT_CODES = (
    ("contains an unsupportable claim", "unsupportable_claim",
     "Makes a promise that cannot be proven, such as \"guaranteed\" or \"best price\"."),
    ("headline is all caps", "all_caps_headline",
     "The headline is in capital letters only, which most platforms refuse."),
    ("excessive exclamation marks", "too_many_exclamations",
     "Uses more than one exclamation mark."),
)


def _normalise_text(value: str) -> str:
    folded = unicodedata.normalize("NFKC", value).translate(_APOSTROPHES).casefold()
    return _SPACES.sub(" ", folded).strip()


def _numbers(text: str) -> set[Decimal]:
    found: set[Decimal] = set()
    for raw in _NUMBER.findall(text):
        # Thousands separators in any grouping ("1,000", "1,00,000") are
        # dropped, and "49.00" and "49" are the same number.
        found.add(Decimal(raw.replace(",", "")).normalize())
    return found


def _copy_text(copy: CreativeCopy) -> str:
    return f"{copy.headline} {copy.body} {copy.cta}"


def check_platform_rules(copy: CreativeCopy) -> QAIssue | None:
    """The DCO's platform-independent rules, applied to creative copy."""
    reason = check_text(headline=copy.headline, body_copy=f"{copy.body} {copy.cta}")
    if reason is None:
        return None
    for prefix, code, message in _CHECK_TEXT_CODES:
        if reason.startswith(prefix):
            return QAIssue(code, message)
    return QAIssue("advertising_rule", "The wording breaks an advertising rule.")


def check_banned_phrases(copy: CreativeCopy, banned_phrases: Sequence[Any]) -> QAIssue | None:
    """The brand kit's "don'ts", matched as whole words, ignoring case."""
    text = _normalise_text(_copy_text(copy))
    for phrase in list(banned_phrases)[:MAX_BANNED_PHRASES]:
        if not isinstance(phrase, str):
            continue
        needle = _normalise_text(phrase[:MAX_BANNED_PHRASE_CHARS])
        if needle and re.search(rf"(?<!\w){re.escape(needle)}(?!\w)", text):
            return QAIssue("banned_phrase", "Uses a word or phrase your brand kit does not allow.")
    return None


def check_numbers_grounded(copy: CreativeCopy, facts: ProductFacts) -> QAIssue | None:
    """Every number in the copy must appear somewhere in the product data."""
    if _numbers(_copy_text(copy)) - _numbers(facts.text()):
        return QAIssue(
            "ungrounded_number",
            "Mentions a number that is not in the product details. Add it to the "
            "product first if it is true.",
        )
    return None


def check_headline_words(copy: CreativeCopy, spec: FormatSpec) -> QAIssue | None:
    """Banner headlines read best at five to eight words (the schema refuses
    more than eight; fewer is allowed but flagged)."""
    if spec.kind == "banner" and word_count(copy.headline) < BANNER_HEADLINE_WORDS[0]:
        return QAIssue(
            "short_headline", "Banner headlines usually work best at five to eight words.", "warning"
        )
    return None


def check_copy_content(
    copy: CreativeCopy,
    *,
    spec: FormatSpec,
    facts: ProductFacts,
    banned_phrases: Sequence[Any] = (),
) -> QAReport:
    return build_report(
        (
            check_platform_rules(copy),
            check_banned_phrases(copy, banned_phrases),
            check_numbers_grounded(copy, facts),
            check_headline_words(copy, spec),
        )
    )


# ── Geometry ───────────────────────────────────────────────────────────────
@dataclass(frozen=True, slots=True)
class Box:
    x: float
    y: float
    width: float
    height: float

    @property
    def right(self) -> float:
        return self.x + self.width

    @property
    def bottom(self) -> float:
        return self.y + self.height

    def expanded(self, margin: float) -> Box:
        return Box(self.x - margin, self.y - margin, self.width + 2 * margin, self.height + 2 * margin)

    def intersects(self, other: Box) -> bool:
        return (
            self.x < other.right and other.x < self.right
            and self.y < other.bottom and other.y < self.bottom
        )

    def within(self, other: Box) -> bool:
        return (
            self.x >= other.x and self.y >= other.y
            and self.right <= other.right and self.bottom <= other.bottom
        )


def safe_area(spec: FormatSpec) -> Box:
    """The part of the canvas content may use: inside the bleed for print,
    and clear of the platform's own interface for formats with a safe zone."""
    bleed_px = round(spec.bleed_mm / 25.4 * PRINT_DPI)
    zone = spec.safe_zone
    left = bleed_px + zone.left
    top = bleed_px + zone.top
    return Box(
        left,
        top,
        spec.width_px - left - bleed_px - zone.right,
        spec.height_px - top - bleed_px - zone.bottom,
    )


def check_safe_zone(box: Box, spec: FormatSpec) -> QAIssue | None:
    if not box.within(safe_area(spec)):
        return QAIssue(
            "outside_safe_area",
            "Something sits too close to the edge, where it may be cut off or covered.",
        )
    return None


# ── Contrast (WCAG 2.x) ────────────────────────────────────────────────────
#: Normal text needs 4.5:1, large text 3:1 (WCAG 2.x success criterion 1.4.3).
CONTRAST_NORMAL = 4.5
CONTRAST_LARGE = 3.0
#: Large text is at least 18pt, or 14pt bold (24px and about 18.66px).
LARGE_TEXT_PX = 24.0
LARGE_BOLD_TEXT_PX = 18.66


def _channel(value: int) -> float:
    c = value / 255
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def relative_luminance(colour: str) -> float:
    hex_value = normalise_hex(colour)
    r, g, b = (int(hex_value[i:i + 2], 16) for i in (1, 3, 5))
    return 0.2126 * _channel(r) + 0.7152 * _channel(g) + 0.0722 * _channel(b)


def contrast_ratio(foreground: str, background: str) -> float:
    a, b = relative_luminance(foreground), relative_luminance(background)
    lighter, darker = max(a, b), min(a, b)
    return (lighter + 0.05) / (darker + 0.05)


def is_large_text(size_px: float, *, bold: bool = False) -> bool:
    return size_px >= LARGE_TEXT_PX or (bold and size_px >= LARGE_BOLD_TEXT_PX)


def check_contrast(
    foreground: str, background: str, *, size_px: float, bold: bool = False
) -> QAIssue | None:
    """Fail below 4.5:1 for body text, or 3:1 for large text. The ratio is
    compared unrounded, as WCAG requires."""
    needed = CONTRAST_LARGE if is_large_text(size_px, bold=bold) else CONTRAST_NORMAL
    if contrast_ratio(foreground, background) < needed:
        return QAIssue(
            "low_contrast",
            "Some text is hard to read against its background. Pick a darker or lighter colour.",
        )
    return None


# ── Text fit ───────────────────────────────────────────────────────────────
#: Width of a string in pixels at a given font and size.
Measure = Callable[[str], float]
_MAX_FONT_PX = 400


@dataclass(frozen=True, slots=True)
class TextFit:
    fits: bool
    size_px: int
    lines: tuple[str, ...] = field(default=())


@lru_cache(maxsize=256)
def _pillow_font(font_id: str, size_px: int, bold: bool) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """The font Pillow measures with. Uses the shipped static file when it is
    present (step 3.2 fetches them) and Pillow's bundled font otherwise, so
    the check works before the fonts are installed."""
    path = pillow_font_path(font_id, 700 if bold else 400)
    if path.is_file():
        return ImageFont.truetype(str(path), size_px)
    return ImageFont.load_default(size=size_px)


def pillow_measure(font_id: str, size_px: int, *, bold: bool = False) -> Measure:
    get_font(font_id)  # refuse an unknown id before touching any file
    font = _pillow_font(font_id, size_px, bold)
    return lambda text: float(font.getlength(text))


def wrap_lines(text: str, max_width: float, measure: Measure) -> tuple[str, ...] | None:
    """Greedy word wrap; ``None`` if a single word is wider than the box."""
    lines: list[str] = []
    for paragraph in text.split("\n"):
        current = ""
        for word in paragraph.split():
            if measure(word) > max_width:
                return None
            candidate = f"{current} {word}" if current else word
            if measure(candidate) <= max_width:
                current = candidate
            else:
                lines.append(current)
                current = word
        lines.append(current)
    return tuple(lines)


def _fit_at(text: str, box: Box, size_px: int, line_height: float, max_lines: int | None,
            measure: Measure) -> tuple[str, ...] | None:
    lines = wrap_lines(text, box.width, measure)
    if lines is None:
        return None
    if max_lines is not None and len(lines) > max_lines:
        return None
    if len(lines) * size_px * line_height > box.height:
        return None
    return lines


def fit_text(
    text: str,
    box: Box,
    *,
    font_id: str,
    max_size_px: int,
    min_size_px: int,
    line_height: float = 1.2,
    max_lines: int | None = None,
    bold: bool = False,
    measure_factory: Callable[[int], Measure] | None = None,
) -> TextFit:
    """The largest size in ``[min_size_px, max_size_px]`` at which ``text``
    wraps into ``box``. ``fits`` is false if even the minimum does not."""
    low = max(1, min(min_size_px, _MAX_FONT_PX))
    high = max(low, min(max_size_px, _MAX_FONT_PX))
    factory = measure_factory or (lambda size: pillow_measure(font_id, size, bold=bold))

    best: TextFit | None = None
    while low <= high:  # wrapped height grows with size, so bisect
        mid = (low + high) // 2
        lines = _fit_at(text, box, mid, line_height, max_lines, factory(mid))
        if lines is None:
            high = mid - 1
        else:
            best = TextFit(True, mid, lines)
            low = mid + 1
    return best or TextFit(False, max(1, min_size_px))


def check_text_fit(
    text: str,
    box: Box,
    *,
    font_id: str,
    max_size_px: int,
    min_size_px: int,
    line_height: float = 1.2,
    max_lines: int | None = None,
    bold: bool = False,
    measure_factory: Callable[[int], Measure] | None = None,
) -> QAIssue | None:
    result = fit_text(
        text, box, font_id=font_id, max_size_px=max_size_px, min_size_px=min_size_px,
        line_height=line_height, max_lines=max_lines, bold=bold, measure_factory=measure_factory,
    )
    if not result.fits:
        return QAIssue("text_does_not_fit", "The text does not fit this size. Try shorter wording.")
    return None


# ── Logo ───────────────────────────────────────────────────────────────────
#: The logo's shorter side must be at least this many pixels.
LOGO_MIN_PX = 24
#: Clear space on every side, as a share of the logo's shorter side.
LOGO_CLEAR_SPACE = 0.25


def check_logo(logo: Box, spec: FormatSpec, *, others: Sequence[Box] = ()) -> list[QAIssue]:
    issues: list[QAIssue] = []
    shorter = min(logo.width, logo.height)
    if shorter < LOGO_MIN_PX:
        issues.append(QAIssue("logo_too_small", "The logo is too small to read at this size."))
    zone = logo.expanded(shorter * LOGO_CLEAR_SPACE)
    if not zone.within(safe_area(spec)) or any(zone.intersects(o) for o in others):
        issues.append(QAIssue("logo_crowded", "The logo needs more space around it."))
    return issues


# ── Output file ────────────────────────────────────────────────────────────
def check_file_size(size_bytes: int, spec: FormatSpec) -> QAIssue | None:
    cap = spec.max_bytes
    if cap is not None and size_bytes > cap:
        return QAIssue(
            "file_too_large",
            f"The file is larger than {cap // 1024} KB, the most this format allows.",
        )
    return None


def check_dimensions(width: int, height: int, spec: FormatSpec) -> QAIssue | None:
    if (width, height) != (spec.width_px, spec.height_px):
        return QAIssue("wrong_size", "The finished file is not the size this format needs.")
    return None


__all__ = [
    "CONTRAST_LARGE",
    "CONTRAST_NORMAL",
    "LOGO_CLEAR_SPACE",
    "LOGO_MIN_PX",
    "Box",
    "ProductFacts",
    "QAIssue",
    "QAReport",
    "TextFit",
    "build_report",
    "check_banned_phrases",
    "check_contrast",
    "check_copy_content",
    "check_dimensions",
    "check_file_size",
    "check_headline_words",
    "check_logo",
    "check_numbers_grounded",
    "check_platform_rules",
    "check_safe_zone",
    "check_text_fit",
    "contrast_ratio",
    "fit_text",
    "is_large_text",
    "pillow_measure",
    "relative_luminance",
    "safe_area",
    "wrap_lines",
]
