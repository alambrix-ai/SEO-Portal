"""The font registry: which families the studio ships, and where they live.

Fonts are self-hosted, never fetched at render time (research R5). Eight
SIL OFL 1.1 families from ``google/fonts``, pinned to one commit:

* Chromium, the production renderer, handles every family, variable or not.
* The Pillow fallback cannot select a weight from a variable font reliably,
  so it uses the three families that ship static files (Lato, Poppins and
  Bebas Neue) and maps every other family to the nearest of those.

The files themselves are fetched and hash-checked when the image is built
(step 3.2), against a ``fonts.lock`` of ``<sha256> <path> <url>`` lines
generated once in a trusted environment. This module defines what that lock
may contain and how a file is checked against it; it never downloads
anything.

Every path below is built from this registry only. A font id from a request
or a brand kit is checked against :data:`FONT_IDS` first, so user input never
becomes part of a filesystem path or URL.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import quote

from app.models.creative import FONT_IDS, check_font_id

#: google/fonts commit every file is taken from (research R5).
FONTS_COMMIT = "47d54ca1ebb14fd864695f1d39dc734f39017c33"
_UPSTREAM = "https://raw.githubusercontent.com/google/fonts"

#: Where the fetched files are stored, one sub-directory per family.
FONT_DIR = Path(__file__).resolve().parent / "assets" / "fonts"

#: Shipped beside each family's fonts, as the OFL requires.
LICENCE_FILE = "OFL.txt"

Category = Literal["sans", "serif", "display"]


@dataclass(frozen=True, slots=True)
class FontFile:
    filename: str
    #: The weight a static file provides; ``None`` for a variable font, which
    #: covers a range of weights in one file.
    weight: int | None


@dataclass(frozen=True, slots=True)
class FontSpec:
    id: str
    #: The CSS ``font-family`` name.
    family: str
    category: Category
    #: The family's directory under ``ofl/`` upstream and under FONT_DIR here.
    directory: str
    files: tuple[FontFile, ...]
    #: The static family the Pillow fallback draws this one with.
    pillow_id: str

    @property
    def is_variable(self) -> bool:
        return any(f.weight is None for f in self.files)

    @property
    def is_static(self) -> bool:
        return not self.is_variable

    @property
    def css_stack(self) -> str:
        """The family plus a generic fallback, for a CSS ``font-family``."""
        generic = "serif" if self.category == "serif" else "sans-serif"
        return f'"{self.family}", {generic}'


def _static(*pairs: tuple[str, int]) -> tuple[FontFile, ...]:
    return tuple(FontFile(name, weight) for name, weight in pairs)


def _variable(filename: str) -> tuple[FontFile, ...]:
    return (FontFile(filename, None),)


FONTS: dict[str, FontSpec] = {
    spec.id: spec
    for spec in (
        FontSpec("inter", "Inter", "sans", "inter",
                 _variable("Inter[opsz,wght].ttf"), pillow_id="lato"),
        FontSpec("lato", "Lato", "sans", "lato",
                 _static(("Lato-Regular.ttf", 400), ("Lato-Bold.ttf", 700)),
                 pillow_id="lato"),
        FontSpec("poppins", "Poppins", "sans", "poppins",
                 _static(("Poppins-Regular.ttf", 400), ("Poppins-SemiBold.ttf", 600),
                         ("Poppins-Bold.ttf", 700)),
                 pillow_id="poppins"),
        FontSpec("montserrat", "Montserrat", "sans", "montserrat",
                 _variable("Montserrat[wght].ttf"), pillow_id="poppins"),
        # No serif family ships static files, so the fallback draws serifs
        # with Lato rather than guessing at a variable font's default weight.
        FontSpec("lora", "Lora", "serif", "lora",
                 _variable("Lora[wght].ttf"), pillow_id="lato"),
        FontSpec("playfair_display", "Playfair Display", "serif", "playfairdisplay",
                 _variable("PlayfairDisplay[wght].ttf"), pillow_id="lato"),
        FontSpec("oswald", "Oswald", "display", "oswald",
                 _variable("Oswald[wght].ttf"), pillow_id="bebas_neue"),
        FontSpec("bebas_neue", "Bebas Neue", "display", "bebasneue",
                 _static(("BebasNeue-Regular.ttf", 400)), pillow_id="bebas_neue"),
    )
}

#: The families the Pillow fallback can draw with (research R5).
PILLOW_FONT_IDS: tuple[str, ...] = tuple(s.id for s in FONTS.values() if s.is_static)


def get_font(font_id: str) -> FontSpec:
    """The spec for an allow-listed font id, or ``ValueError``."""
    return FONTS[check_font_id(font_id)]


def pillow_font_path(font_id: str, weight: int = 400) -> Path:
    """The static file the Pillow fallback uses for ``font_id`` at ``weight``.

    The nearest available weight is chosen. The path is built from the
    registry alone; whether the file exists is for the caller to check.
    """
    spec = FONTS[get_font(font_id).pillow_id]
    chosen = min(spec.files, key=lambda f: abs((f.weight or 400) - weight))
    return FONT_DIR / spec.directory / chosen.filename


# ── Fetch manifest and hash pins ───────────────────────────────────────────
def upstream_url(spec: FontSpec, filename: str) -> str:
    """The pinned https URL of one upstream file (brackets percent-encoded)."""
    return f"{_UPSTREAM}/{FONTS_COMMIT}/ofl/{spec.directory}/{quote(filename, safe='')}"


def manifest() -> dict[str, str]:
    """Every file the build fetches: relative path → pinned URL."""
    entries: dict[str, str] = {}
    for spec in FONTS.values():
        for name in (*(f.filename for f in spec.files), LICENCE_FILE):
            entries[f"{spec.directory}/{name}"] = upstream_url(spec, name)
    return entries


class FontIntegrityError(ValueError):
    """A font file or lock entry does not match what the registry allows."""


_LOCK_LINE = re.compile(r"^([0-9a-f]{64}) (\S+) (\S+)$")
#: Git LFS pointers are small text files; one in place of a font means the
#: fetch went wrong even if someone pinned its hash.
_LFS_POINTER = b"version https://git-lfs"
_FONT_MAGIC = (b"\x00\x01\x00\x00", b"true", b"OTTO")


def parse_lock(text: str) -> dict[str, str]:
    """Read ``fonts.lock`` into relative path → SHA-256.

    Every entry must name a file in :func:`manifest` at exactly its pinned
    URL; anything else, including a duplicate, is refused rather than
    skipped, so the lock cannot widen what the build fetches.
    """
    allowed = manifest()
    pins: dict[str, str] = {}
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = _LOCK_LINE.fullmatch(line)
        if match is None:
            raise FontIntegrityError(f"fonts.lock line {number} is malformed")
        digest, path, url = match.groups()
        if allowed.get(path) != url:
            raise FontIntegrityError(f"fonts.lock line {number} names a file outside the registry")
        if path in pins:
            raise FontIntegrityError(f"fonts.lock line {number} repeats a file")
        pins[path] = digest
    return pins


def verify_font_bytes(path: str, data: bytes, pins: dict[str, str]) -> None:
    """Raise :class:`FontIntegrityError` unless ``data`` is the pinned file."""
    expected = pins.get(path)
    if expected is None:
        raise FontIntegrityError("font file is not pinned")
    if data.startswith(_LFS_POINTER):
        raise FontIntegrityError("font file is a Git LFS pointer, not a font")
    if not path.endswith(".txt") and not data.startswith(_FONT_MAGIC):
        raise FontIntegrityError("font file is not a TrueType or OpenType font")
    if hashlib.sha256(data).hexdigest() != expected:
        raise FontIntegrityError("font file does not match its pinned hash")


__all__ = [
    "FONTS",
    "FONTS_COMMIT",
    "FONT_DIR",
    "FONT_IDS",
    "LICENCE_FILE",
    "PILLOW_FONT_IDS",
    "FontFile",
    "FontIntegrityError",
    "FontSpec",
    "get_font",
    "manifest",
    "parse_lock",
    "pillow_font_path",
    "upstream_url",
    "verify_font_bytes",
]
