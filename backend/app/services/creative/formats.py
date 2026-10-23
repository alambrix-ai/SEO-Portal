"""The format catalogue: every size the studio can produce, with its limits.

One table holds every platform number (research R8): pixel sizes, file-size
caps, copy-length caps and safe zones. The schema, QA and renderer all read
from here, so a limit cannot drift between the check that enforces it and the
template that lays it out.

* Display banners are the Google Display sizes, capped at 150 KB
  (``creative_banner_max_kb``). Headlines are short: about five to eight
  words, and never more than eight.
* Social sizes follow each network's recommended upload shape.
* Print formats are defined by their trim size in millimetres plus a 3 mm
  bleed on every edge; the raster size is the bleed page at 300 dpi.

Format ids are stored on ``CreativeJob.formats``, so an id, once shipped, is
never renamed.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from app.core.config import settings

FormatKind = Literal["banner", "social", "email", "print"]
OutputMime = Literal["image/png", "application/pdf"]

#: Print resolution for raster previews of print formats.
PRINT_DPI = 300
#: Bleed on every edge of every print format.
PRINT_BLEED_MM = 3.0
_MM_PER_INCH = 25.4

#: Recommended headline length for display banners, in words.
BANNER_HEADLINE_WORDS = (5, 8)


@dataclass(frozen=True, slots=True)
class SafeZone:
    """Pixels at each edge the platform's own interface may cover."""

    top: int = 0
    right: int = 0
    bottom: int = 0
    left: int = 0


@dataclass(frozen=True, slots=True)
class CopyLimits:
    headline_chars: int
    #: ``0`` means the layout has no room for body text at all.
    body_chars: int
    cta_chars: int
    #: Hard cap on headline words; ``None`` where only characters matter.
    headline_words: int | None = None


@dataclass(frozen=True, slots=True)
class FormatSpec:
    id: str
    #: Plain-English name shown to marketers.
    label: str
    kind: FormatKind
    width_px: int
    height_px: int
    limits: CopyLimits
    mime: OutputMime = "image/png"
    #: Platform cap on the file, in KB; ``None`` where none applies.
    max_kb: int | None = None
    network: str = ""
    safe_zone: SafeZone = SafeZone()
    #: Print only: finished size, bleed, and fold panels (1 = flat sheet).
    trim_mm: tuple[float, float] | None = None
    bleed_mm: float = 0.0
    panels: int = 1

    @property
    def size(self) -> str:
        return f"{self.width_px}x{self.height_px}"

    @property
    def page_mm(self) -> tuple[float, float] | None:
        """Print only: the PDF page size, trim plus bleed on both sides."""
        if self.trim_mm is None:
            return None
        return (self.trim_mm[0] + 2 * self.bleed_mm, self.trim_mm[1] + 2 * self.bleed_mm)

    @property
    def max_bytes(self) -> int | None:
        """The file-size cap. Banners use the configured cap, so an operator
        can follow a network that changes it without a code change."""
        if self.kind == "banner":
            return settings.creative_banner_max_kb * 1024
        return None if self.max_kb is None else self.max_kb * 1024


def _banner(width: int, height: int, limits: CopyLimits) -> FormatSpec:
    return FormatSpec(
        id=f"banner_{width}x{height}",
        label=f"Display banner {width}×{height}",
        kind="banner",
        width_px=width,
        height_px=height,
        limits=limits,
        max_kb=150,
        network="google_display",
    )


def _px(mm: float) -> int:
    return round(mm / _MM_PER_INCH * PRINT_DPI)


def _print(format_id: str, label: str, trim_mm: tuple[float, float], limits: CopyLimits,
           panels: int = 1) -> FormatSpec:
    page_w = trim_mm[0] + 2 * PRINT_BLEED_MM
    page_h = trim_mm[1] + 2 * PRINT_BLEED_MM
    return FormatSpec(
        id=format_id,
        label=label,
        kind="print",
        width_px=_px(page_w),
        height_px=_px(page_h),
        limits=limits,
        mime="application/pdf",
        trim_mm=trim_mm,
        bleed_mm=PRINT_BLEED_MM,
        panels=panels,
    )


_BANNER_WORDS = BANNER_HEADLINE_WORDS[1]
_SOCIAL = CopyLimits(headline_chars=60, body_chars=200, cta_chars=24)
_FLYER = CopyLimits(headline_chars=70, body_chars=600, cta_chars=30)
_BROCHURE = CopyLimits(headline_chars=70, body_chars=1500, cta_chars=30)

_CATALOGUE: tuple[FormatSpec, ...] = (
    # ── Display banners (Google Display, 150 KB) ──
    _banner(300, 250, CopyLimits(40, 90, 18, _BANNER_WORDS)),
    _banner(728, 90, CopyLimits(40, 60, 15, _BANNER_WORDS)),
    _banner(160, 600, CopyLimits(40, 90, 15, _BANNER_WORDS)),
    _banner(336, 280, CopyLimits(40, 90, 18, _BANNER_WORDS)),
    _banner(300, 600, CopyLimits(40, 120, 18, _BANNER_WORDS)),
    _banner(970, 250, CopyLimits(50, 120, 20, _BANNER_WORDS)),
    # A mobile leaderboard has room for a headline and a button, nothing more.
    _banner(320, 50, CopyLimits(30, 0, 12, _BANNER_WORDS)),
    # ── Social ──
    FormatSpec("instagram_portrait", "Instagram post (portrait)", "social", 1080, 1350,
               _SOCIAL, network="instagram"),
    FormatSpec("instagram_square", "Instagram post (square)", "social", 1080, 1080,
               _SOCIAL, network="instagram"),
    FormatSpec("instagram_story", "Instagram story", "social", 1080, 1920,
               CopyLimits(50, 140, 24), network="instagram"),
    FormatSpec("linkedin_post", "LinkedIn post", "social", 1200, 627,
               _SOCIAL, max_kb=3 * 1024, network="linkedin"),
    FormatSpec("x_post", "X post", "social", 1600, 900,
               _SOCIAL, max_kb=5 * 1024, network="x"),
    FormatSpec("pinterest_pin", "Pinterest pin", "social", 1000, 1500,
               _SOCIAL, max_kb=20 * 1024, network="pinterest",
               safe_zone=SafeZone(top=270, right=195, bottom=790, left=65)),
    FormatSpec("facebook_post", "Facebook post", "social", 1200, 630,
               _SOCIAL, network="facebook"),
    # ── Video thumbnails / covers (Creative Studio expansion) ──
    FormatSpec(
        "youtube_thumbnail",
        "YouTube thumbnail",
        "social",
        1280,
        720,
        CopyLimits(60, 100, 24),
        max_kb=2 * 1024,
        network="youtube",
    ),
    FormatSpec(
        "tiktok_cover",
        "TikTok / Reels cover",
        "social",
        1080,
        1920,
        CopyLimits(50, 120, 24),
        max_kb=2 * 1024,
        network="tiktok",
    ),
    FormatSpec(
        "video_square_thumb",
        "Square video thumbnail",
        "social",
        1080,
        1080,
        CopyLimits(50, 100, 24),
        max_kb=2 * 1024,
        network="video",
    ),
    # ── Email ──
    FormatSpec("email_header", "Email header", "email", 600, 200,
               CopyLimits(50, 80, 20)),
    # ── Print (3 mm bleed) ──
    _print("flyer_a4", "Flyer (A4)", (210.0, 297.0), _FLYER),
    _print("flyer_letter", "Flyer (US Letter)", (215.9, 279.4), _FLYER),
    _print("brochure_bifold", "Brochure (bi-fold, A4)", (297.0, 210.0), _BROCHURE, panels=2),
    _print("brochure_trifold", "Brochure (tri-fold, A4)", (297.0, 210.0), _BROCHURE, panels=3),
)

FORMATS: dict[str, FormatSpec] = {spec.id: spec for spec in _CATALOGUE}

#: Google Display sizes, in catalogue order (research R8).
DISPLAY_BANNER_SIZES: tuple[str, ...] = tuple(s.size for s in _CATALOGUE if s.kind == "banner")

#: The longest any format allows, for the schema's format-independent caps.
MAX_HEADLINE_CHARS = max(s.limits.headline_chars for s in _CATALOGUE)
MAX_BODY_CHARS = max(s.limits.body_chars for s in _CATALOGUE)
MAX_CTA_CHARS = max(s.limits.cta_chars for s in _CATALOGUE)


def get_format(format_id: str) -> FormatSpec:
    """The spec for a catalogue id, or ``ValueError`` in plain English."""
    spec = FORMATS.get(format_id) if isinstance(format_id, str) else None
    if spec is None:
        raise ValueError("That format is not one the studio can make")
    return spec


__all__ = [
    "BANNER_HEADLINE_WORDS",
    "DISPLAY_BANNER_SIZES",
    "FORMATS",
    "MAX_BODY_CHARS",
    "MAX_CTA_CHARS",
    "MAX_HEADLINE_CHARS",
    "PRINT_BLEED_MM",
    "PRINT_DPI",
    "CopyLimits",
    "FormatSpec",
    "SafeZone",
    "get_format",
]
