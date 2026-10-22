"""What every renderer takes, returns and raises.

A renderer turns a :class:`~app.services.creative.compose.Layout` into one
file: a PNG (the first page, for multi-page formats) or a PDF of every page.
Errors carry plain-English messages a marketer can read; the cause is logged
by type and hash only, never with document text.
"""
from __future__ import annotations

import io
from dataclasses import dataclass
from typing import Literal, Protocol

from PIL import Image, UnidentifiedImageError

from app.services.creative.compose import Layout, MeasureFor

OutputKind = Literal["png", "pdf"]
RendererName = Literal["chromium", "pillow"]

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_PDF_MAGIC = b"%PDF-"


class RenderError(RuntimeError):
    """Rendering failed. ``str(exc)`` is safe to show to a user."""

    default_message = "We could not draw this design. Please try again."

    def __init__(self, message: str | None = None) -> None:
        super().__init__(message or self.default_message)


class RendererUnavailable(RenderError):
    """The renderer cannot start (for Chromium: the browser will not launch)."""

    default_message = "The design renderer is not available right now."


class RenderTimeout(RenderError):
    default_message = "Drawing this design took too long. Please try again."


class RenderBusy(RenderError):
    default_message = "The design renderer is busy. Please try again in a minute."


class RenderInputError(RenderError):
    """The design itself cannot be drawn (too large, unreadable image...)."""

    default_message = "This design cannot be drawn. Try smaller images."


@dataclass(frozen=True, slots=True)
class RenderedFile:
    data: bytes
    mime: Literal["image/png", "application/pdf"]
    #: Pixel size for a PNG; the canvas size the layout used for a PDF.
    width: int
    height: int
    pages: int
    renderer: RendererName
    #: The layout drawn, for QA (text fit, logo and safe-zone checks).
    layout: Layout


class Renderer(Protocol):
    name: RendererName
    #: Which fonts the layout must be measured with for this renderer.
    measure: MeasureFor

    def render(self, layout: Layout, *, output: OutputKind) -> RenderedFile: ...


def check_png(data: bytes, layout: Layout, renderer: RendererName) -> RenderedFile:
    """Wrap renderer output after checking it is a PNG of the format's size."""
    if not data.startswith(_PNG_MAGIC):
        raise RenderError()
    try:
        with Image.open(io.BytesIO(data)) as img:
            width, height = img.size
    except (UnidentifiedImageError, OSError) as exc:
        raise RenderError() from exc
    spec = layout.spec
    if (width, height) != (spec.width_px, spec.height_px):
        raise RenderError()
    return RenderedFile(data, "image/png", width, height, 1, renderer, layout)


def check_pdf(data: bytes, layout: Layout, renderer: RendererName) -> RenderedFile:
    if not data.startswith(_PDF_MAGIC):
        raise RenderError()
    spec = layout.spec
    return RenderedFile(data, "application/pdf", spec.width_px, spec.height_px,
                        len(layout.pages), renderer, layout)


__all__ = [
    "OutputKind",
    "RenderBusy",
    "RenderError",
    "RenderInputError",
    "RenderTimeout",
    "RenderedFile",
    "Renderer",
    "RendererName",
    "RendererUnavailable",
    "check_pdf",
    "check_png",
]
