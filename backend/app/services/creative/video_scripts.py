"""Short-form video scripts and captions from approved product facts only."""
from __future__ import annotations

from dataclasses import dataclass

from app.llm import untrusted
from app.services.creative.formats import FORMATS

THUMBNAIL_FORMAT_IDS = (
    "youtube_thumbnail",
    "tiktok_cover",
    "video_square_thumb",
)


@dataclass(frozen=True, slots=True)
class VideoScriptDraft:
    title: str
    script: str
    captions: list[str]
    thumbnail_formats: list[str]
    cta: str


def build_draft(
    *,
    name: str,
    description: str = "",
    claims: list[str] | None = None,
    duration_sec: int = 30,
) -> VideoScriptDraft:
    """Deterministic script — never invents prices, ratings, or medical claims."""
    name = (name or "Product").strip()[:120]
    claims = [str(c).strip() for c in (claims or []) if str(c).strip()][:4]
    blurb = (description or "").strip()[:280]
    if not blurb and claims:
        blurb = claims[0]
    if not blurb:
        blurb = f"{name} — see the full details on our site."
    duration_sec = max(15, min(60, int(duration_sec or 30)))
    beats = max(3, duration_sec // 10)
    captions: list[str] = [f"Meet {name}"]
    for claim in claims[: beats - 1]:
        captions.append(claim[:80])
    captions.append(f"Learn more about {name}")
    script_lines = [
        f"[0s HOOK] {captions[0]}",
        *[f"[{i * 8}s] {c}" for i, c in enumerate(captions[1:-1], start=1)],
        f"[{duration_sec - 5}s CTA] {captions[-1]}",
    ]
    formats = [fid for fid in THUMBNAIL_FORMAT_IDS if fid in FORMATS]
    return VideoScriptDraft(
        title=f"Short video · {name}"[:200],
        script="\n".join(script_lines),
        captions=captions,
        thumbnail_formats=formats,
        cta=captions[-1],
    )


def as_untrusted(draft: VideoScriptDraft) -> str:
    return untrusted.wrap(
        "video_script",
        "\n".join(
            [
                f"TITLE: {draft.title}",
                f"SCRIPT:\n{draft.script}",
                f"CAPTIONS: {' | '.join(draft.captions)}",
                f"CTA: {draft.cta}",
            ]
        ),
    )


def to_dict(draft: VideoScriptDraft) -> dict:
    return {
        "title": draft.title,
        "script": draft.script,
        "captions": list(draft.captions),
        "thumbnail_formats": list(draft.thumbnail_formats),
        "cta": draft.cta,
    }


def validate_thumbnail_formats(ids: list[str]) -> list[str]:
    out = []
    for fid in ids or []:
        if fid in FORMATS and fid in THUMBNAIL_FORMAT_IDS:
            out.append(fid)
    return out
