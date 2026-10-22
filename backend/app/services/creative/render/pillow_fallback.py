"""The Pillow fallback: draws a layout without a browser.

Used when ``CREATIVE_RENDERER=pillow`` (the Render test environment) or when
Chromium will not launch. It draws the same layout Chromium would, measured
with the static font families (research R5: Pillow cannot pick a weight from
a variable font reliably), so designs look plainer but every element sits in
the same place and fits the same way.

It reads no HTML and makes no requests: the only inputs are the layout's
numbers, validated colours, allow-listed font files and image bytes already
checked by :class:`~app.services.creative.compose.InlineImage`.
"""
from __future__ import annotations

import io
from functools import lru_cache
from typing import Literal

from PIL import Image, ImageDraw, ImageFont, ImageOps, UnidentifiedImageError

from app.core.logging import get_logger, log_failure
from app.models.creative import normalise_hex
from app.services.creative.compose import ImageBlock, Layout, Page, TextBlock
from app.services.creative.fonts import pillow_font_path
from app.services.creative.formats import PRINT_DPI
from app.services.creative.render.base import (
    OutputKind,
    RenderedFile,
    RenderError,
    RenderInputError,
    check_pdf,
    check_png,
)

log = get_logger(__name__)


def _rgb(colour: str) -> tuple[int, int, int]:
    value = normalise_hex(colour)
    return int(value[1:3], 16), int(value[3:5], 16), int(value[5:7], 16)


@lru_cache(maxsize=256)
def _font(font_id: str, size_px: int, weight: int) -> ImageFont.FreeTypeFont:
    path = pillow_font_path(font_id, weight)
    if not path.is_file():
        raise RenderError("The design fonts are not installed.")
    return ImageFont.truetype(str(path), size_px)


def _open(block: ImageBlock) -> Image.Image:
    try:
        img = Image.open(io.BytesIO(block.image.data))
        img.load()
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise RenderInputError("An image in this design could not be read.") from exc
    return img.convert("RGBA")


def _size(block: ImageBlock) -> tuple[int, int]:
    return max(1, round(block.box.width)), max(1, round(block.box.height))


def _paste(canvas: Image.Image, block: ImageBlock) -> None:
    img = _open(block)
    size = _size(block)
    if block.fit == "cover":
        img = ImageOps.fit(img, size, method=Image.Resampling.LANCZOS)
    else:
        img = img.resize(size, Image.Resampling.LANCZOS)
    canvas.alpha_composite(img, (round(block.box.x), round(block.box.y)))


def _draw_text(canvas: Image.Image, block: TextBlock) -> None:
    font = _font(block.font_id, block.size_px, block.weight)
    line_px = block.size_px * block.line_height
    # Draw into a clip of the block's box so overflow is cut, as in CSS.
    layer = Image.new("RGBA", (max(1, round(block.box.width)), max(1, round(block.box.height))),
                      (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    fill = (*_rgb(block.colour), 255)
    for index, line in enumerate(block.lines):
        top = index * line_px + (line_px - block.size_px) / 2
        if top > layer.height:
            break
        x = 0.0
        if block.align == "center":
            x = (layer.width - font.getlength(line)) / 2
        draw.text((x, top), line, font=font, fill=fill)
    canvas.alpha_composite(layer, (round(block.box.x), round(block.box.y)))


def _draw_page(layout: Layout, page: Page) -> Image.Image:
    spec = layout.spec
    canvas = Image.new("RGBA", (spec.width_px, spec.height_px),
                       (*_rgb(layout.palette.background), 255))
    if page.background is not None:
        _paste(canvas, page.background)
    for rect in page.rects:
        layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
        alpha = max(0, min(255, round(rect.opacity * 255)))
        box = rect.box
        ImageDraw.Draw(layer).rounded_rectangle(
            (box.x, box.y, box.right - 1, box.bottom - 1),
            radius=rect.radius_px, fill=(*_rgb(rect.colour), alpha),
        )
        canvas.alpha_composite(layer)
    for image in page.images:
        _paste(canvas, image)
    for block in page.texts:
        _draw_text(canvas, block)
    return canvas.convert("RGB")


class PillowRenderer:
    name: Literal["pillow"] = "pillow"
    measure: Literal["pillow"] = "pillow"

    def render(self, layout: Layout, *, output: OutputKind) -> RenderedFile:
        if layout.measured_for != "pillow":
            raise RenderError()
        pages = layout.pages if output == "pdf" else layout.pages[:1]
        try:
            images = [_draw_page(layout, page) for page in pages]
            buffer = io.BytesIO()
            if output == "png":
                images[0].save(buffer, format="PNG", optimize=True)
                return check_png(buffer.getvalue(), layout, "pillow")
            # The page is the canvas at print resolution, so a print format's
            # PDF page is trim plus bleed exactly.
            dpi = PRINT_DPI if layout.spec.kind == "print" else 96
            images[0].save(buffer, format="PDF", save_all=True, append_images=images[1:],
                           resolution=float(dpi))
            return check_pdf(buffer.getvalue(), layout, "pillow")
        except RenderError:
            raise
        except (OSError, ValueError) as exc:
            log_failure(log, "Creative render failed (pillow)", exc, format_id=layout.spec.id)
            raise RenderError() from exc


__all__ = ["PillowRenderer"]
