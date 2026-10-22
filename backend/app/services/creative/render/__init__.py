"""Turn a creative into a file: the public render API.

Callers (the creative pipeline's Celery task, a dev worker) use
:func:`render_creative`. It is blocking and must never be called from an
``async`` request handler; Chromium runs on its own threads either way.

``CREATIVE_RENDERER`` picks the renderer. Chromium is the production
primary. If it will not launch, rendering falls back to Pillow, the failure
is logged as an error and :func:`renderer_status` reports ``degraded`` so
``/health`` can show the operator.
"""
from __future__ import annotations

import logging
import threading
from typing import Literal

from app.core.config import settings
from app.core.logging import get_logger
from app.services.creative.compose import ComposeError, CreativeInputs, build_layout
from app.services.creative.render.base import (
    OutputKind,
    RenderBusy,
    RenderedFile,
    RenderError,
    Renderer,
    RendererName,
    RendererUnavailable,
    RenderInputError,
    RenderTimeout,
)
from app.services.creative.render.chromium import ChromiumRenderer
from app.services.creative.render.pillow_fallback import PillowRenderer

log = get_logger(__name__)

RendererStatus = Literal["chromium", "pillow", "degraded", "not_started"]

_lock = threading.Lock()
_chromium: ChromiumRenderer | None = None
_pillow = PillowRenderer()
#: True once Chromium was configured but could not launch.
_degraded = False


def _chromium_renderer() -> ChromiumRenderer:
    global _chromium
    with _lock:
        if _chromium is None:
            _chromium = ChromiumRenderer()
        return _chromium


def _mark_degraded(exc: RendererUnavailable) -> None:
    global _degraded
    _degraded = True
    log.error("Creative renderer: Chromium unavailable, using the Pillow fallback (%s)",
              type(exc).__name__)


def get_renderer() -> Renderer:
    """The renderer to use now: Chromium if configured and it launches,
    otherwise Pillow. Launching Chromium may block for up to the render
    timeout the first time."""
    global _degraded
    if settings.creative_renderer == "pillow":
        return _pillow
    chromium = _chromium_renderer()
    if chromium.launched:
        return chromium
    try:
        chromium.start()
    except RendererUnavailable as exc:
        _mark_degraded(exc)
        return _pillow
    _degraded = False
    return chromium


def render_creative(inputs: CreativeInputs, *, output: OutputKind | None = None) -> RenderedFile:
    """Draw one creative.

    ``output`` defaults to the format's own type: PNG for banners, social and
    email; PDF (every page, trim plus bleed) for print. ``"png"`` on a print
    format gives a 300 dpi preview of the first page.

    Raises :class:`RenderInputError` when the design itself cannot be drawn,
    :class:`RenderBusy` / :class:`RenderTimeout` when the renderer is
    overloaded, and :class:`RenderError` otherwise. Every message is safe to
    show to a user.
    """
    spec = inputs.spec
    kind: OutputKind = output or ("pdf" if spec.mime == "application/pdf" else "png")
    if kind not in ("png", "pdf"):
        raise RenderInputError()
    renderer = get_renderer()
    try:
        try:
            return renderer.render(build_layout(inputs, measure=renderer.measure), output=kind)
        except RendererUnavailable as exc:
            if renderer is _pillow:
                raise
            _mark_degraded(exc)
            return _pillow.render(build_layout(inputs, measure="pillow"), output=kind)
    except ComposeError as exc:
        raise RenderInputError(str(exc)) from exc


def renderer_status() -> RendererStatus:
    """For ``/health``: which renderer is in use, or ``degraded`` when
    Chromium is configured but fell back to Pillow."""
    if settings.creative_renderer == "pillow":
        return "pillow"
    if _degraded:
        return "degraded"
    chromium = _chromium
    if chromium is None or not chromium.launched:
        return "not_started"
    return "chromium"


def shutdown() -> None:
    """Close the browser(s). Call from Celery's ``worker_process_shutdown``."""
    chromium = _chromium
    if chromium is not None:
        try:
            chromium.shutdown()
        except RuntimeError as exc:
            log.log(logging.WARNING, "Creative renderer: shutdown failed (%s)", type(exc).__name__)


__all__ = [
    "OutputKind",
    "RenderBusy",
    "RenderError",
    "RenderInputError",
    "RenderTimeout",
    "RenderedFile",
    "Renderer",
    "RendererName",
    "RendererStatus",
    "RendererUnavailable",
    "get_renderer",
    "render_creative",
    "renderer_status",
    "shutdown",
]
