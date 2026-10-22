"""Compose a creative: lay it out, then write it as a self-contained HTML page.

Two steps, shared by both renderers:

1. :func:`build_layout` places every element on the canvas in pixels: the
   logo, the headline, body, claims, button and disclaimer. Text is fitted
   with the font the renderer will actually draw with (``measure``), so the
   size chosen here is the size that appears in the file. The layout is also
   what QA reads for its safe-zone, logo and text-fit checks.
2. :func:`render_html` writes that layout through a Jinja2 template. Only
   Chromium uses the HTML; the Pillow fallback draws the layout directly.

The HTML is built for a browser that has JavaScript off and no network
(threat model D):

* Jinja2 runs sandboxed, autoescaped, with ``StrictUndefined``, no
  extensions and a loader that can only see :data:`TEMPLATE_DIR`. Templates
  never use ``|safe`` and nothing is ever compiled from a string.
* Every value written inside CSS is one autoescaping cannot protect, so each
  is built here from a closed set: colours are validated ``#rrggbb``, sizes
  are numbers, font families are ``f_<allow-listed id>``.
* Images and fonts are inlined as ``data:`` URIs from bytes we hold, after a
  magic-byte check. No URL from a brand kit, product or model ever reaches
  the page.
* The page carries its own Content-Security-Policy, so even if a request
  slipped past the browser-level block it would still be refused.
"""
from __future__ import annotations

import base64
import io
import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Literal

from jinja2 import FileSystemLoader, StrictUndefined
from jinja2.sandbox import SandboxedEnvironment
from PIL import Image, ImageFont, UnidentifiedImageError

from app.models.creative import check_font_id, normalise_hex
from app.services.creative.fonts import FONT_DIR, FONTS, FontSpec, get_font
from app.services.creative.formats import PRINT_DPI, FormatSpec, get_format
from app.services.creative.qa import Box, Measure, fit_text, pillow_measure, safe_area
from app.services.creative.schema import ComposedCopy, Palette


TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"

#: Largest image (logo or background) the composer will inline.
MAX_INLINE_IMAGE_BYTES = 10 * 1024 * 1024
#: Largest HTML document a renderer will accept: two images, the fonts of two
#: families and the markup, base64-encoded, fit comfortably below this.
MAX_HTML_BYTES = 40 * 1024 * 1024

#: The in-document policy. Inline style is allowed because the browser runs
#: with JavaScript off and no network; everything else must be ``data:``.
DOCUMENT_CSP = "default-src 'none'; img-src data:; font-src data:; style-src 'unsafe-inline'"

Role = Literal["headline", "body", "claims", "cta", "disclaimer"]
MeasureFor = Literal["chromium", "pillow"]
ImageMime = Literal["image/png", "image/jpeg", "image/webp"]

_MM_PER_INCH = 25.4
_LINE_HEIGHT = 1.2
#: Text is fitted into a slightly narrower box than it is drawn in, so a
#: rounding difference between the measurer and the browser never wraps.
_FIT_SLACK = 0.97


class ComposeError(ValueError):
    """The inputs cannot be composed. The message is plain English."""


# ── Inputs ─────────────────────────────────────────────────────────────────
_MAGIC: tuple[tuple[bytes, ImageMime], ...] = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
)


def _sniff(data: bytes) -> ImageMime | None:
    for magic, mime in _MAGIC:
        if data.startswith(magic):
            return mime
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


@dataclass(frozen=True, slots=True)
class InlineImage:
    """An image the page embeds. Build it with :meth:`from_bytes`."""

    data: bytes
    mime: ImageMime
    width: int
    height: int

    @classmethod
    def from_bytes(cls, data: bytes) -> InlineImage:
        """Check the bytes are a PNG, JPEG or WebP we can decode, and size it.

        The type comes from the magic bytes, never from a name or header.
        """
        if not isinstance(data, bytes) or not data:
            raise ComposeError("The image is empty")
        if len(data) > MAX_INLINE_IMAGE_BYTES:
            raise ComposeError("The image is too large to use in a design")
        mime = _sniff(data)
        if mime is None:
            raise ComposeError("The image must be a PNG, JPEG or WebP file")
        try:
            with Image.open(io.BytesIO(data)) as img:
                width, height = img.size
        except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
            raise ComposeError("The image could not be read") from exc
        if width < 1 or height < 1:
            raise ComposeError("The image could not be read")
        return cls(data=data, mime=mime, width=width, height=height)

    @property
    def aspect(self) -> float:
        return self.width / self.height

    @property
    def data_uri(self) -> str:
        return _data_uri(self.mime, self.data)


def _data_uri(mime: str, data: bytes) -> str:
    if mime not in ("image/png", "image/jpeg", "image/webp", "font/ttf"):
        raise ComposeError("That file type cannot be embedded")
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


@dataclass(frozen=True, slots=True)
class CreativeInputs:
    """Everything one creative is drawn from. All of it is already checked:
    copy by :func:`schema.compose_copy`, colours by :class:`Palette`, fonts
    against the allow-list here, images by :meth:`InlineImage.from_bytes`."""

    format_id: str
    copy: ComposedCopy
    palette: Palette
    heading_font: str
    body_font: str
    logo: InlineImage | None = None
    background: InlineImage | None = None

    def __post_init__(self) -> None:
        get_format(self.format_id)
        check_font_id(self.heading_font)
        check_font_id(self.body_font)
        if not isinstance(self.palette, Palette):
            raise ComposeError("The colours are not valid")
        if not isinstance(self.copy, ComposedCopy):
            raise ComposeError("The copy has not been checked")
        for image in (self.logo, self.background):
            if image is not None and not isinstance(image, InlineImage):
                raise ComposeError("The image has not been checked")

    @property
    def spec(self) -> FormatSpec:
        return get_format(self.format_id)


# ── Layout model ───────────────────────────────────────────────────────────
@dataclass(frozen=True, slots=True)
class TextBlock:
    role: Role
    box: Box
    #: Already wrapped; each line is drawn as given.
    lines: tuple[str, ...]
    font_id: str
    weight: int
    size_px: int
    colour: str
    align: Literal["left", "center"]
    #: False when even the smallest allowed size overflowed the box.
    fits: bool
    line_height: float = _LINE_HEIGHT


@dataclass(frozen=True, slots=True)
class ImageBlock:
    role: Literal["logo", "background"]
    box: Box
    image: InlineImage
    fit: Literal["contain", "cover"]


@dataclass(frozen=True, slots=True)
class RectBlock:
    role: Literal["band", "scrim", "button"]
    box: Box
    colour: str
    opacity: float = 1.0
    radius_px: int = 0


@dataclass(frozen=True, slots=True)
class Page:
    background: ImageBlock | None = None
    rects: tuple[RectBlock, ...] = ()
    images: tuple[ImageBlock, ...] = ()
    texts: tuple[TextBlock, ...] = ()


@dataclass(frozen=True, slots=True)
class Layout:
    spec: FormatSpec
    palette: Palette
    heading_font: str
    body_font: str
    pages: tuple[Page, ...]
    measured_for: MeasureFor

    @property
    def overflowing(self) -> tuple[Role, ...]:
        """Roles whose text did not fit, for QA's text-fit check."""
        return tuple(t.role for p in self.pages for t in p.texts if not t.fits)

    @property
    def logo_boxes(self) -> tuple[Box, ...]:
        return tuple(i.box for p in self.pages for i in p.images if i.role == "logo")

    @property
    def text_boxes(self) -> tuple[Box, ...]:
        return tuple(t.box for p in self.pages for t in p.texts)


# ── Measuring ──────────────────────────────────────────────────────────────
MeasureFactory = Callable[[int], Measure]


def _font_path(spec: FontSpec, weight: int) -> tuple[Path, bool]:
    """The file Chromium draws ``spec`` at ``weight`` with, and whether it is
    variable. Built from the registry only."""
    chosen = min(spec.files, key=lambda f: abs((f.weight or weight) - weight))
    return FONT_DIR / spec.directory / chosen.filename, chosen.weight is None


@lru_cache(maxsize=512)
def _file_font(font_id: str, size_px: int, weight: int) -> ImageFont.FreeTypeFont:
    path, variable = _font_path(get_font(font_id), weight)
    if not path.is_file():
        raise ComposeError("The design fonts are not installed")
    font = ImageFont.truetype(str(path), size_px)
    if variable:
        axes = font.get_variation_axes()
        font.set_variation_by_axes([
            weight if axis.get("name") in (b"Weight", "Weight") else axis["default"]
            for axis in axes
        ])
    return font


def _measure_factory(font_id: str, weight: int, measure: MeasureFor) -> MeasureFactory:
    if measure == "pillow":
        bold = weight >= 600
        return lambda size: pillow_measure(font_id, size, bold=bold)

    def factory(size: int) -> Measure:
        font = _file_font(font_id, size, weight)
        return lambda text: float(font.getlength(text))

    return factory


# ── Geometry helpers ───────────────────────────────────────────────────────
def _inset(box: Box, margin: float) -> Box:
    return Box(box.x + margin, box.y + margin, max(1.0, box.width - 2 * margin),
               max(1.0, box.height - 2 * margin))


def _round(box: Box) -> Box:
    return Box(round(box.x), round(box.y), max(1, round(box.width)), max(1, round(box.height)))


def _contain(image: InlineImage, box: Box, *, align: Literal["left", "center"]) -> Box:
    """The box ``image`` occupies when scaled to fit inside ``box``."""
    scale = min(box.width / image.width, box.height / image.height)
    width, height = image.width * scale, image.height * scale
    x = box.x if align == "left" else box.x + (box.width - width) / 2
    return _round(Box(x, box.y + (box.height - height) / 2, width, height))


@dataclass(slots=True)
class _Builder:
    inputs: CreativeInputs
    measure: MeasureFor
    texts: list[TextBlock] = field(default_factory=list)
    rects: list[RectBlock] = field(default_factory=list)
    images: list[ImageBlock] = field(default_factory=list)

    @property
    def short_side(self) -> int:
        spec = self.inputs.spec
        return min(spec.width_px, spec.height_px)

    def text(self, role: Role, text: str, box: Box, *, heading: bool = False,
             max_px: float, min_px: float, colour: str | None = None,
             align: Literal["left", "center"] = "left", max_lines: int | None = None,
             weight: int | None = None) -> TextBlock | None:
        if not text.strip():
            return None
        font_id = self.inputs.heading_font if heading else self.inputs.body_font
        weight = weight if weight is not None else (700 if heading else 400)
        box = _round(box)
        fit_box = Box(box.x, box.y, box.width * _FIT_SLACK, box.height)
        low = max(6, round(min_px))
        result = fit_text(
            text, fit_box, font_id=font_id, max_size_px=max(low, round(max_px)),
            min_size_px=low, line_height=_LINE_HEIGHT, max_lines=max_lines,
            bold=weight >= 600, measure_factory=_measure_factory(font_id, weight, self.measure),
        )
        lines = result.lines or tuple(text.split("\n"))
        block = TextBlock(
            role=role, box=box, lines=lines, font_id=font_id, weight=weight,
            size_px=result.size_px, colour=colour or self.inputs.palette.text,
            align=align, fits=result.fits,
        )
        self.texts.append(block)
        return block

    def button(self, box: Box, *, align: Literal["left", "center"]) -> None:
        """The call to action: a pill in the accent colour, sized to its text."""
        copy, palette = self.inputs.copy, self.inputs.palette
        box = _round(box)
        pad_y = box.height * 0.18
        inner = Box(box.x, box.y + pad_y, box.width, box.height - 2 * pad_y)
        factory = _measure_factory(self.inputs.body_font, 700, self.measure)
        size = fit_text(
            copy.cta, Box(0, 0, inner.width * 0.8 * _FIT_SLACK, inner.height),
            font_id=self.inputs.body_font, max_size_px=max(6, round(inner.height / _LINE_HEIGHT)),
            min_size_px=6, line_height=_LINE_HEIGHT, max_lines=1, bold=True,
            measure_factory=factory,
        ).size_px
        text_w = factory(size)(copy.cta)
        width = min(box.width, text_w / _FIT_SLACK + 2 * max(size * 0.9, box.height * 0.3))
        x = box.x if align == "left" else box.x + (box.width - width) / 2
        pill = _round(Box(x, box.y, width, box.height))
        self.rects.append(RectBlock("button", pill, palette.accent,
                                    radius_px=round(pill.height / 2)))
        # One pixel of headroom so rounding the box never clips the label.
        label_h = math.ceil(size * _LINE_HEIGHT) + 1
        label = Box(pill.x, pill.y + (pill.height - label_h) / 2, pill.width, label_h)
        self.text("cta", copy.cta, label, max_px=size, min_px=size, weight=700,
                  colour=palette.on_accent, align="center", max_lines=1)

    def logo(self, box: Box, *, align: Literal["left", "center"]) -> None:
        logo = self.inputs.logo
        if logo is not None:
            self.images.append(ImageBlock("logo", _contain(logo, box, align=align), logo, "contain"))

    def page(self, *, background: Box | None = None, scrim: Box | None = None) -> Page:
        bg = None
        if background is not None and self.inputs.background is not None:
            bg = ImageBlock("background", _round(background), self.inputs.background, "cover")
            if scrim is not None:
                self.rects.insert(0, RectBlock("scrim", _round(scrim),
                                               self.inputs.palette.background, opacity=0.86,
                                               radius_px=round(self.short_side * 0.02)))
        page = Page(bg, tuple(self.rects), tuple(self.images), tuple(self.texts))
        self.texts, self.rects, self.images = [], [], []
        return page


def _claims_text(claims: Sequence[str]) -> str:
    return "\n".join(f"• {claim}" for claim in claims)


# ── Arrangements ───────────────────────────────────────────────────────────
#: Share of the stack's height each part asks for, before gaps.
_STACK_SHARES: dict[str, float] = {
    "logo": 0.10, "headline": 0.30, "body": 0.24, "claims": 0.14, "cta": 0.10, "disclaimer": 0.07,
}


def _stack(b: _Builder, region: Box, parts: Sequence[str], *, align: Literal["left", "center"],
           body_text: str | None = None) -> None:
    """Lay ``parts`` top to bottom in ``region``; the disclaimer sits last."""
    copy = b.inputs.copy
    present = [p for p in parts if _has(b, p)]
    if not present:
        return
    gap = region.height * 0.03
    total = sum(_STACK_SHARES[p] for p in present)
    usable = region.height - gap * (len(present) - 1)
    short = b.short_side
    y = region.y
    for part in present:
        height = usable * _STACK_SHARES[part] / total
        box = Box(region.x, y, region.width, height)
        if part == "logo":
            b.logo(box, align=align)
        elif part == "headline":
            b.text("headline", copy.headline, box, heading=True, max_px=min(height, short * 0.12),
                   min_px=short * 0.02, align=align)
        elif part == "body":
            b.text("body", body_text if body_text is not None else copy.body, box,
                   max_px=short * 0.045, min_px=short * 0.012, align=align)
        elif part == "claims":
            b.text("claims", _claims_text(copy.claims), box, max_px=short * 0.04,
                   min_px=short * 0.012, align=align)
        elif part == "cta":
            pill_h = min(height, short * 0.09)
            b.button(Box(box.x, box.y + (height - pill_h) / 2, box.width, pill_h), align=align)
        elif part == "disclaimer":
            b.text("disclaimer", copy.disclaimer, box, max_px=short * 0.022,
                   min_px=short * 0.009, align=align)
        y += height + gap


def _has(b: _Builder, part: str) -> bool:
    copy = b.inputs.copy
    return {
        "logo": b.inputs.logo is not None,
        "headline": bool(copy.headline),
        "body": bool(copy.body) and b.inputs.spec.limits.body_chars > 0,
        "claims": bool(copy.claims),
        "cta": bool(copy.cta),
        "disclaimer": bool(copy.disclaimer),
    }[part]


def _row(b: _Builder, region: Box) -> None:
    """Wide formats (leaderboards, billboards, email headers): logo, text and
    button side by side, with the disclaimer as a strip along the bottom."""
    copy = b.inputs.copy
    gap = region.width * 0.03
    if copy.disclaimer:
        strip_h = region.height * 0.2
        b.text("disclaimer", copy.disclaimer,
               Box(region.x, region.bottom - strip_h, region.width, strip_h),
               max_px=strip_h / _LINE_HEIGHT, min_px=6, max_lines=1)
        region = Box(region.x, region.y, region.width, region.height - strip_h - region.height * 0.04)
    x = region.x
    if b.inputs.logo is not None:
        logo_w = min(region.height * 1.6, region.width * 0.18)
        b.logo(Box(x, region.y, logo_w, region.height), align="left")
        x += logo_w + gap
    cta_w = region.width * 0.24
    pill_h = min(region.height, max(region.height * 0.5, 18))
    b.button(Box(region.right - cta_w, region.y + (region.height - pill_h) / 2, cta_w, pill_h),
             align="center")
    text_box = Box(x, region.y, max(1.0, region.right - cta_w - gap - x), region.height)
    if _has(b, "body"):
        head_h = text_box.height * 0.58
        b.text("headline", copy.headline, Box(text_box.x, text_box.y, text_box.width, head_h),
               heading=True, max_px=head_h / _LINE_HEIGHT, min_px=8)
        body_box = Box(text_box.x, text_box.y + head_h, text_box.width, text_box.height - head_h)
        b.text("body", copy.body, body_box, max_px=body_box.height / _LINE_HEIGHT * 0.6, min_px=7)
    else:
        b.text("headline", copy.headline, text_box, heading=True,
               max_px=text_box.height / _LINE_HEIGHT, min_px=8)


def _content(spec: FormatSpec, region: Box | None = None) -> Box:
    area = region or safe_area(spec)
    return _inset(area, min(area.width, area.height) * 0.07)


def _single(b: _Builder) -> tuple[Page, ...]:
    spec = b.inputs.spec
    full = Box(0, 0, spec.width_px, spec.height_px)
    region = _content(spec)
    if spec.kind == "print":
        bleed = round(spec.bleed_mm / _MM_PER_INCH * PRINT_DPI)
        # An accent band across the top, running out into the bleed.
        b.rects.append(RectBlock("band", _round(Box(0, 0, spec.width_px, bleed + spec.height_px * 0.02)),
                                 b.inputs.palette.accent))
    if spec.width_px / spec.height_px >= 2.4:
        _row(b, region)
    else:
        _stack(b, region, ("logo", "headline", "body", "claims", "cta", "disclaimer"),
               align="center" if spec.kind == "social" else "left")
    scrim = _inset(region, -min(region.width, region.height) * 0.04)
    return (b.page(background=full, scrim=scrim),)


def _split_body(b: _Builder, text: str, boxes: Sequence[Box]) -> None:
    """Flow body text through several equal panels at one size."""
    first = boxes[0]
    tall = Box(first.x, first.y, first.width, first.height * len(boxes))
    short = b.short_side
    probe = _Builder(b.inputs, b.measure)
    fitted = probe.text("body", text, tall, max_px=short * 0.02, min_px=short * 0.011)
    if fitted is None:
        return
    per_panel = max(1, int(first.height // (fitted.size_px * _LINE_HEIGHT)))
    lines = list(fitted.lines)
    for index, box in enumerate(boxes):
        chunk = tuple(lines[index * per_panel:(index + 1) * per_panel])
        if not chunk:
            break
        is_last = index == len(boxes) - 1
        overflow = is_last and len(lines) > per_panel * len(boxes)
        b.texts.append(TextBlock(
            role="body", box=_round(box), lines=chunk, font_id=fitted.font_id,
            weight=fitted.weight, size_px=fitted.size_px, colour=fitted.colour,
            align="left", fits=fitted.fits and not overflow,
        ))


def _brochure(b: _Builder) -> tuple[Page, ...]:
    """Two pages: the outside (back, flap and cover) and the inside."""
    spec = b.inputs.spec
    copy = b.inputs.copy
    bleed = round(spec.bleed_mm / _MM_PER_INCH * PRINT_DPI)
    trim_w, trim_h = spec.width_px - 2 * bleed, spec.height_px - 2 * bleed
    panel_w = trim_w / spec.panels
    panels = [Box(bleed + i * panel_w, bleed, panel_w, trim_h) for i in range(spec.panels)]
    inner = [_inset(p, min(p.width, p.height) * 0.08) for p in panels]

    # Outside. The cover is the right-hand panel; it gets the background.
    cover = panels[-1]
    cover_bg = Box(cover.x, 0, spec.width_px - cover.x, spec.height_px)
    _stack(b, inner[-1], ("logo", "headline", "cta"), align="left")
    if spec.panels == 3:
        _stack(b, inner[0], ("claims",), align="left")
    back = inner[-2]
    back_parts = ("logo", "disclaimer") if spec.panels == 2 else ("logo", "cta", "disclaimer")
    _stack(b, back, back_parts, align="left")
    outside = b.page(background=cover_bg, scrim=_inset(inner[-1], -panel_w * 0.03))

    # Inside: the body flows through every panel but the last, which closes
    # with the claims and the button.
    if copy.body:
        _split_body(b, copy.body, inner[:-1])
    _stack(b, inner[-1], ("claims", "cta"), align="left")
    b.rects.insert(0, RectBlock("band", _round(Box(0, 0, spec.width_px, bleed + trim_h * 0.02)),
                                b.inputs.palette.accent))
    inside = b.page()
    return outside, inside


def build_layout(inputs: CreativeInputs, *, measure: MeasureFor) -> Layout:
    """Place every element of ``inputs`` on its format's canvas.

    ``measure`` names the renderer: Chromium draws each family from its own
    file, the Pillow fallback from the nearest static family.
    """
    builder = _Builder(inputs, measure)
    spec = inputs.spec
    pages = _brochure(builder) if spec.kind == "print" and spec.panels > 1 else _single(builder)
    return Layout(spec=spec, palette=inputs.palette, heading_font=inputs.heading_font,
                  body_font=inputs.body_font, pages=pages, measured_for=measure)


# ── HTML ───────────────────────────────────────────────────────────────────
_env = SandboxedEnvironment(
    loader=FileSystemLoader(str(TEMPLATE_DIR)),
    autoescape=True,
    undefined=StrictUndefined,
    extensions=[],
    trim_blocks=True,
    lstrip_blocks=True,
    auto_reload=False,
)

_TEMPLATES: dict[str, str] = {
    "banner": "banner.html.j2",
    "social": "social.html.j2",
    "email": "email_header.html.j2",
    "flyer": "flyer.html.j2",
    "brochure": "brochure.html.j2",
}


@dataclass(frozen=True, slots=True)
class Geometry:
    """How canvas pixels become CSS lengths: pixels for a raster, millimetres
    for a PDF (so the page matches ``@page`` exactly)."""

    factor: float
    unit: Literal["px", "mm"]

    def __call__(self, value: float) -> str:
        return f"{float(value) * self.factor:.3f}{self.unit}"


def _css_colour(value: str) -> str:
    return normalise_hex(value)


_env.filters["colour"] = _css_colour


@dataclass(frozen=True, slots=True)
class FontFace:
    family: str
    weight: str
    data_uri: str


@lru_cache(maxsize=16)
def _font_faces(font_id: str) -> tuple[FontFace, ...]:
    spec = FONTS[check_font_id(font_id)]
    faces: list[FontFace] = []
    for item in spec.files:
        path = FONT_DIR / spec.directory / item.filename
        if not path.is_file():
            raise ComposeError("The design fonts are not installed")
        weight = "100 900" if item.weight is None else str(item.weight)
        faces.append(FontFace(f"f_{font_id}", weight, _data_uri("font/ttf", path.read_bytes())))
    return tuple(faces)


def template_for(spec: FormatSpec) -> str:
    if spec.kind == "print":
        return _TEMPLATES["brochure" if spec.panels > 1 else "flyer"]
    return _TEMPLATES[spec.kind]


def render_html(layout: Layout, *, output: Literal["png", "pdf"]) -> str:
    """The page for Chromium. ``pdf`` sizes it in millimetres with an
    ``@page`` of trim plus bleed; ``png`` in canvas pixels."""
    spec = layout.spec
    # Font ids become CSS family names; re-check them even though the inputs
    # were checked, because a Layout can be built by hand.
    for page in layout.pages:
        for block in page.texts:
            check_font_id(block.font_id)
    page_mm = spec.page_mm if output == "pdf" else None
    if output == "pdf" and page_mm is None:
        # A raster format written as PDF: one canvas pixel per CSS pixel.
        page_mm = (spec.width_px * _MM_PER_INCH / 96, spec.height_px * _MM_PER_INCH / 96)
    if page_mm is not None:
        geo = Geometry(page_mm[0] / spec.width_px, "mm")
        page_w, page_h = f"{page_mm[0]:.3f}mm", f"{page_mm[1]:.3f}mm"
    else:
        geo = Geometry(1.0, "px")
        page_w, page_h = f"{spec.width_px}px", f"{spec.height_px}px"
    faces = _font_faces(layout.heading_font)
    if layout.body_font != layout.heading_font:
        faces = faces + _font_faces(layout.body_font)
    html = _env.get_template(template_for(spec)).render(
        csp=DOCUMENT_CSP,
        layout=layout,
        pages=layout.pages,
        palette=layout.palette,
        faces=faces,
        geo=geo,
        page_w=page_w,
        page_h=page_h,
        print_page=page_mm is not None,
        line_height=_LINE_HEIGHT,
    )
    if len(html.encode("utf-8")) > MAX_HTML_BYTES:
        raise ComposeError("The design is too large to render. Try smaller images.")
    return html


__all__ = [
    "DOCUMENT_CSP",
    "MAX_HTML_BYTES",
    "MAX_INLINE_IMAGE_BYTES",
    "TEMPLATE_DIR",
    "ComposeError",
    "CreativeInputs",
    "ImageBlock",
    "InlineImage",
    "Layout",
    "Page",
    "RectBlock",
    "TextBlock",
    "build_layout",
    "render_html",
    "template_for",
]
