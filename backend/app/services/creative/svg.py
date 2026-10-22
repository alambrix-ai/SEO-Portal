"""Uploaded SVG logos: sanitise, then rasterise to PNG in a child process.

An SVG is a program as much as a picture: it can carry scripts, event
handlers, links to local files (``<image href="/etc/passwd">``), links to the
network, entity expansions (billion laughs) and self-referencing ``<use>``
loops. resvg's default resource resolver reads local files, so nothing
reaches it until the sanitiser has rebuilt the document from an allow-list
(research R2):

1. Size cap, and the bytes must start with ``<?xml`` or ``<svg``.
2. Parse with defusedxml: any DOCTYPE, entity or external reference fails.
3. Structural caps on the raw tree: element count and depth.
4. Rebuild from an element and attribute allow-list. Active or external
   content (``<script>``, ``<foreignObject>``, ``on*`` handlers, any href that
   is not ``#id`` or an inline PNG/JPEG, CSS ``@import`` or ``url()`` that
   leaves the document) rejects the file outright; anything merely unknown
   (metadata, editor namespaces, filters) is dropped.
5. ``<use>`` count cap, no reference cycles, and a cap on the expanded size.
6. Reserialise with our own serialiser, so only what survived is written.

The rasteriser runs resvg in a separate Python process with a timeout, an
address-space limit where the OS has one (not Windows), an empty working
directory and an environment carrying no secrets. It is given the markup
only: never ``svg_path`` or ``resources_dir``. The PNG magic bytes are
checked on the way back. Only the PNG is ever stored; the SVG is discarded.
"""
from __future__ import annotations

import json
import math
import os
import re
import subprocess
import sys
import tempfile
import threading
from xml.etree.ElementTree import Element, ParseError
from xml.sax.saxutils import escape, quoteattr

from defusedxml import DefusedXmlException
from defusedxml.ElementTree import fromstring

from app.core.config import settings
from app.core.logging import get_logger

log = get_logger(__name__)

SVG_NS = "http://www.w3.org/2000/svg"
XLINK_NS = "http://www.w3.org/1999/xlink"
XML_NS = "http://www.w3.org/XML/1998/namespace"

# ── Caps (research R2) ─────────────────────────────────────────────────────
#: Hard ceiling on an SVG upload. ``CREATIVE_SVG_MAX_BYTES`` may lower it,
#: never raise it.
SVG_MAX_BYTES = 256 * 1024
MAX_ELEMENTS = 5000
MAX_DEPTH = 32
MAX_USE = 50
#: Elements after every ``<use>`` is expanded, so fan-out (a group of uses of
#: a group of uses ...) cannot multiply a small file into a huge render.
MAX_EXPANDED_ELEMENTS = 20_000
#: Largest width or height the document may declare, in CSS pixels.
MAX_DECLARED_PX = 4096
#: The render is fitted inside this square, preserving the aspect ratio.
RENDER_MAX_PX = 1024
RENDER_TIMEOUT_SECONDS = 5
RENDER_MEMORY_BYTES = 512 * 1024 * 1024
#: A 1024x1024 RGBA PNG is at most about 4 MB; anything bigger is not ours.
RENDER_OUTPUT_MAX_BYTES = 8 * 1024 * 1024

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

ALLOWED_ELEMENTS: frozenset[str] = frozenset(
    {
        "svg", "g", "path", "rect", "circle", "ellipse", "line", "polyline",
        "polygon", "defs", "linearGradient", "radialGradient", "stop",
        "clipPath", "mask", "text", "tspan", "title", "desc",
        # Kept only with a safe href (see _clean_element).
        "image", "use",
    }
)
#: Elements whose presence means the file is hostile, not merely unusual.
HOSTILE_ELEMENTS: frozenset[str] = frozenset(
    {"script", "foreignObject", "iframe", "embed", "object", "handler", "listener"}
)
#: Elements whose character data is content (everything else is whitespace).
_TEXT_ELEMENTS: frozenset[str] = frozenset({"text", "tspan", "title", "desc"})
#: Elements that may carry an ``#id`` href.
_FRAGMENT_HREF_ELEMENTS: frozenset[str] = frozenset(
    {"use", "linearGradient", "radialGradient"}
)

ALLOWED_ATTRIBUTES: frozenset[str] = frozenset(
    {
        "id", "x", "y", "x1", "y1", "x2", "y2", "cx", "cy", "r", "rx", "ry",
        "fx", "fy", "fr", "width", "height", "d", "points", "pathLength",
        "viewBox", "preserveAspectRatio", "transform", "version",
        "fill", "fill-opacity", "fill-rule", "stroke", "stroke-width",
        "stroke-opacity", "stroke-linecap", "stroke-linejoin",
        "stroke-miterlimit", "stroke-dasharray", "stroke-dashoffset",
        "opacity", "color", "visibility", "display",
        "offset", "stop-color", "stop-opacity", "gradientUnits",
        "gradientTransform", "spreadMethod",
        "clip-path", "clip-rule", "clipPathUnits", "mask", "maskUnits",
        "maskContentUnits",
        "font-family", "font-size", "font-weight", "font-style",
        "text-anchor", "dominant-baseline", "letter-spacing", "word-spacing",
        "dx", "dy", "rotate", "textLength", "lengthAdjust",
        "style",
    }
)

_FRAGMENT = re.compile(r"#[A-Za-z_][\w.\-:]*")
_DATA_RASTER = re.compile(r"data:image/(?:png|jpeg);base64,[A-Za-z0-9+/=\s]*")
#: ``url(#id)`` with an optional fallback paint, e.g. ``url(#g) #000``. The
#: fallback may only be ``none``, ``currentColor``, a hex colour or a colour
#: keyword: never a second ``url(`` or any other function.
_FRAGMENT_URL_VALUE = re.compile(
    r"\s*url\(\s*(['\"]?)#[A-Za-z_][\w.\-:]*\1\s*\)"
    r"(?:\s+(?:none|currentColor|#[0-9A-Fa-f]{3,8}|[a-z]+))?\s*"
)
#: ``url(`` followed by anything other than a fragment.
_EXTERNAL_URL = re.compile(r"url\(\s*['\"]?\s*(?!#)", re.I)
_LENGTH = re.compile(
    r"\s*([+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)\s*(px|pt|pc|mm|cm|in|em|ex|%)?\s*"
)
_UNIT_PX: dict[str, float] = {
    "": 1.0, "px": 1.0, "pt": 96 / 72, "pc": 16.0, "mm": 96 / 25.4,
    "cm": 96 / 2.54, "in": 96.0, "em": 16.0, "ex": 8.0,
}

# Only a few renders at once: each may hold up to RENDER_MEMORY_BYTES.
_RENDER_SLOTS = threading.BoundedSemaphore(max(1, settings.creative_max_concurrent_renders))

_RENDER_SCRIPT = r"""
import json
import sys

try:
    import resource
except ImportError:
    resource = None
if resource is not None:
    limit = int(sys.argv[1])
    _soft, hard = resource.getrlimit(resource.RLIMIT_AS)
    if hard != resource.RLIM_INFINITY:
        limit = min(limit, hard)
    resource.setrlimit(resource.RLIMIT_AS, (limit, limit))

import resvg_py

job = json.loads(sys.stdin.buffer.read().decode("utf-8"))
png = resvg_py.svg_to_bytes(
    svg_string=job["svg"],
    width=job["width"],
    height=job["height"],
    skip_system_fonts=True,
)
sys.stdout.buffer.write(bytes(png))
"""


class SvgRejected(ValueError):
    """The SVG cannot be used. ``reason`` is a short machine key.

    Reasons: ``too_large``, ``invalid``, ``unsafe``, ``too_complex``,
    ``busy``, ``render_failed``. The message is never shown to users; the
    upload service maps the reason to plain English.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def svg_byte_limit() -> int:
    """The effective SVG size cap: the setting, never above ``SVG_MAX_BYTES``."""
    return min(settings.creative_svg_max_bytes, SVG_MAX_BYTES)


def looks_like_svg(data: bytes) -> bool:
    """Whether ``data`` starts the way R2 requires an SVG to start."""
    head = data[:1024].lstrip(b"\xef\xbb\xbf").lstrip()
    return head.startswith(b"<?xml") or head.startswith(b"<svg")


# ── Sanitiser ──────────────────────────────────────────────────────────────
def _split(name: str) -> tuple[str, str]:
    """``{ns}local`` → ``(ns, local)``; a bare name → ``("", name)``."""
    if name.startswith("{"):
        ns, _, local = name[1:].partition("}")
        return ns, local
    return "", name


def _check_raw_structure(root: Element) -> None:
    """Element-count and depth caps, on the tree as parsed."""
    count = 0
    stack: list[tuple[Element, int]] = [(root, 1)]
    while stack:
        elem, depth = stack.pop()
        count += 1
        if count > MAX_ELEMENTS or depth > MAX_DEPTH:
            raise SvgRejected("too_complex")
        stack.extend((child, depth + 1) for child in elem)


def _parse_length(value: str) -> float | None:
    """A length in CSS pixels, ``None`` for a percentage; ``SvgRejected`` if malformed."""
    match = _LENGTH.fullmatch(value)
    if match is None:
        raise SvgRejected("invalid")
    unit = match.group(2) or ""
    if unit == "%":
        return None
    number = float(match.group(1)) * _UNIT_PX[unit]
    if not math.isfinite(number) or number <= 0:
        raise SvgRejected("invalid")
    return number


def _check_declared_size(root: Element) -> None:
    for name in ("width", "height"):
        raw = root.get(name)
        if raw is None:
            continue
        size = _parse_length(raw)
        if size is not None and size > MAX_DECLARED_PX:
            raise SvgRejected("too_large")


def _clean_style(value: str) -> str | None:
    """The style value to keep, ``None`` to drop it; ``SvgRejected`` if hostile."""
    lowered = value.lower()
    if "@import" in lowered or _EXTERNAL_URL.search(lowered):
        raise SvgRejected("unsafe")
    # url(#id) is harmless but R2 drops it; a backslash can spell either of
    # the above in CSS escapes, so it is dropped too.
    if "url(" in lowered or "\\" in value:
        return None
    return value


def _clean_presentation(value: str) -> str | None:
    """A presentation attribute value to keep, ``None`` to drop it."""
    if "\\" in value:
        return None
    lowered = value.lower()
    if "url(" in lowered:
        if lowered.count("url(") > 1:
            # One paint server per value; a second url( is never a fallback.
            raise SvgRejected("unsafe")
        if _FRAGMENT_URL_VALUE.fullmatch(value):
            return value
        if _EXTERNAL_URL.search(value):
            raise SvgRejected("unsafe")
        return None
    return value


def _href_kind(value: str) -> str:
    """``fragment``, ``data_raster``; anything else is hostile."""
    candidate = value.strip()
    if _FRAGMENT.fullmatch(candidate):
        return "fragment"
    if _DATA_RASTER.fullmatch(candidate):
        return "data_raster"
    raise SvgRejected("unsafe")


class _Cleaner:
    def __init__(self) -> None:
        self.seen_ids: set[str] = set()

    def element(self, source: Element) -> Element | None:
        """A rebuilt copy of ``source`` from the allow-lists, ``None`` to drop it."""
        ns, tag = _split(source.tag) if isinstance(source.tag, str) else ("", "")
        if tag in HOSTILE_ELEMENTS:
            raise SvgRejected("unsafe")
        # Hostile content is refused even inside an element that is dropped.
        for name in source.attrib:
            if _split(name)[1].lower().startswith("on"):
                raise SvgRejected("unsafe")
        if ns not in ("", SVG_NS) or tag not in ALLOWED_ELEMENTS:
            self._scan_dropped(source)
            return None

        attrs: dict[str, str] = {}
        href_kind = ""
        for name, value in source.attrib.items():
            attr_ns, attr = _split(name)
            if attr == "href" and attr_ns in ("", XLINK_NS):
                kind = _href_kind(value)
                if kind == "fragment" and tag in _FRAGMENT_HREF_ELEMENTS:
                    attrs["href"] = value.strip()
                    href_kind = kind
                elif kind == "data_raster" and tag == "image":
                    attrs["href"] = "".join(value.split())
                    href_kind = kind
                continue
            if attr_ns == XML_NS and attr == "space":
                if value in ("default", "preserve"):
                    attrs["xml:space"] = value
                continue
            if attr_ns or attr not in ALLOWED_ATTRIBUTES:
                continue
            if attr == "id":
                if value in self.seen_ids:
                    continue
                self.seen_ids.add(value)
                attrs["id"] = value
                continue
            kept = _clean_style(value) if attr == "style" else _clean_presentation(value)
            if kept is not None:
                attrs[attr] = kept

        if tag == "use" and href_kind != "fragment":
            self._scan_dropped(source)
            return None
        if tag == "image" and href_kind != "data_raster":
            self._scan_dropped(source)
            return None

        clean = Element(tag, attrs)
        if tag in _TEXT_ELEMENTS and source.text:
            clean.text = source.text
        for child in source:
            kept_child = self.element(child)
            if kept_child is None:
                continue
            if tag in _TEXT_ELEMENTS and child.tail:
                kept_child.tail = child.tail
            clean.append(kept_child)
        return clean

    def _scan_dropped(self, source: Element) -> None:
        """Refuse hostile content anywhere inside an element being dropped."""
        for elem in source.iter():
            _ns, tag = _split(elem.tag) if isinstance(elem.tag, str) else ("", "")
            if tag in HOSTILE_ELEMENTS:
                raise SvgRejected("unsafe")
            if tag == "style" and elem.text:
                _clean_style(elem.text)
            for name, value in elem.attrib.items():
                attr_ns, attr = _split(name)
                if attr.lower().startswith("on"):
                    raise SvgRejected("unsafe")
                if attr == "href" and attr_ns in ("", XLINK_NS):
                    _href_kind(value)
                if attr == "style":
                    _clean_style(value)


def _check_uses(root: Element) -> None:
    """``<use>`` count cap, no reference cycles, and an expanded-size cap."""
    uses = [elem for elem in root.iter("use")]
    if len(uses) > MAX_USE:
        raise SvgRejected("too_complex")
    by_id = {elem.get("id"): elem for elem in root.iter() if elem.get("id")}

    def targets_in(elem: Element) -> list[str]:
        return [str(use.get("href"))[1:] for use in elem.iter("use")]

    totals: dict[str, int] = {}
    visiting: set[str] = set()

    def expanded(target: str) -> int:
        if target in totals:
            return totals[target]
        if target in visiting:
            raise SvgRejected("too_complex")
        elem = by_id.get(target)
        if elem is None:
            return 0
        visiting.add(target)
        total = sum(1 for _ in elem.iter())
        for inner in targets_in(elem):
            total += expanded(inner)
            if total > MAX_EXPANDED_ELEMENTS:
                raise SvgRejected("too_complex")
        visiting.discard(target)
        totals[target] = total
        return total

    grand = sum(1 for _ in root.iter())
    for target in targets_in(root):
        grand += expanded(target)
        if grand > MAX_EXPANDED_ELEMENTS:
            raise SvgRejected("too_complex")


def _serialise(elem: Element, out: list[str], *, root: bool = False) -> None:
    namespaces = f' xmlns="{SVG_NS}"' if root else ""
    attrs = "".join(f" {name}={quoteattr(value)}" for name, value in elem.attrib.items())
    out.append(f"<{elem.tag}{namespaces}{attrs}>")
    if elem.text:
        out.append(escape(elem.text))
    for child in elem:
        _serialise(child, out)
        if child.tail:
            out.append(escape(child.tail))
    out.append(f"</{elem.tag}>")


def sanitise_svg(data: bytes) -> str:
    """Rebuild an untrusted SVG from the allow-lists; ``SvgRejected`` if it cannot be.

    Returns markup that references nothing outside itself.
    """
    if len(data) > svg_byte_limit():
        raise SvgRejected("too_large")
    if not looks_like_svg(data):
        raise SvgRejected("invalid")
    try:
        root = fromstring(data, forbid_dtd=True, forbid_entities=True, forbid_external=True)
    except DefusedXmlException as exc:
        raise SvgRejected("unsafe") from exc
    except (ParseError, ValueError) as exc:
        raise SvgRejected("invalid") from exc

    ns, tag = _split(root.tag) if isinstance(root.tag, str) else ("", "")
    if tag != "svg" or ns not in ("", SVG_NS):
        raise SvgRejected("invalid")
    _check_raw_structure(root)
    _check_declared_size(root)

    clean = _Cleaner().element(root)
    if clean is None:
        raise SvgRejected("invalid")
    _check_uses(clean)

    out: list[str] = []
    _serialise(clean, out, root=True)
    return "".join(out)


# ── Rasteriser ─────────────────────────────────────────────────────────────
def _child_env() -> dict[str, str]:
    """Just enough environment for Python to start; no secrets cross over."""
    return {key: os.environ[key] for key in ("SYSTEMROOT", "PATH") if key in os.environ}


def rasterise_svg(markup: str) -> bytes:
    """Render already-sanitised markup to PNG bytes, fitted inside 1024x1024.

    Only ever call this with the output of :func:`sanitise_svg`.
    """
    job = json.dumps(
        {"svg": markup, "width": RENDER_MAX_PX, "height": RENDER_MAX_PX}
    ).encode("utf-8")
    if not _RENDER_SLOTS.acquire(timeout=RENDER_TIMEOUT_SECONDS):
        raise SvgRejected("busy")
    try:
        with tempfile.TemporaryDirectory(prefix="svg-render-") as workdir:
            result = subprocess.run(  # noqa: S603 - fixed argv, no shell, no user input in argv
                [sys.executable, "-I", "-c", _RENDER_SCRIPT, str(RENDER_MEMORY_BYTES)],
                input=job,
                capture_output=True,
                timeout=RENDER_TIMEOUT_SECONDS,
                cwd=workdir,
                env=_child_env(),
                check=False,
            )
    except subprocess.TimeoutExpired as exc:
        log.warning("SVG render timed out after %ss", RENDER_TIMEOUT_SECONDS)
        raise SvgRejected("too_complex") from exc
    finally:
        _RENDER_SLOTS.release()

    png = result.stdout
    if result.returncode != 0:
        # stderr may echo parts of the document; only the exit code is logged.
        log.warning("SVG render failed: exit=%s", result.returncode)
        raise SvgRejected("render_failed")
    if len(png) > RENDER_OUTPUT_MAX_BYTES or not png.startswith(PNG_MAGIC):
        log.warning("SVG render returned unexpected output: bytes=%s", len(png))
        raise SvgRejected("render_failed")
    return png


def svg_to_png(data: bytes) -> bytes:
    """Sanitise an uploaded SVG and rasterise it. The SVG itself is discarded."""
    return rasterise_svg(sanitise_svg(data))
