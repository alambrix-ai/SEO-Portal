"""Deterministic multi-channel drafts from an approved article (no invented claims)."""
from __future__ import annotations

import re
from dataclasses import dataclass

from app.llm import untrusted

_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")


@dataclass(frozen=True, slots=True)
class RepurposePack:
    social_text: str
    hashtags: list[str]
    email_subject: str
    email_body: str
    video_script: str


def strip_html(html: str) -> str:
    text = _TAG.sub(" ", html or "")
    return _WS.sub(" ", text).strip()


def build_pack(*, title: str, body_html: str, site_url: str = "") -> RepurposePack:
    plain = strip_html(body_html)
    if len(plain) < 80:
        raise ValueError("Source article is too short to repurpose")
    title = (title or "Update").strip()[:200]
    hook = plain[:220].rstrip(" .,;")
    bullets = _sentences(plain)[:3]
    hashtags = _hashtags(title, plain)
    link = f" {site_url}" if site_url else ""
    social = f"{title}\n\n{hook}…{link}".strip()[:480]
    email_body = (
        f"Hi,\n\nWe just published “{title}”.\n\n"
        + "\n".join(f"• {b}" for b in bullets)
        + (f"\n\nRead more: {site_url}" if site_url else "")
        + "\n\nThanks"
    )
    script = (
        f"[HOOK] {title}\n"
        f"[POINT 1] {bullets[0] if bullets else hook}\n"
        f"[POINT 2] {bullets[1] if len(bullets) > 1 else 'See the full article for details.'}\n"
        f"[CTA] Read the guide{(' at ' + site_url) if site_url else ''}."
    )
    return RepurposePack(
        social_text=social,
        hashtags=hashtags,
        email_subject=f"New: {title}"[:120],
        email_body=email_body[:4000],
        video_script=script[:4000],
    )


def as_untrusted_block(pack: RepurposePack) -> str:
    """Fence derived copy before any LLM touch-up (injection-safe)."""
    return untrusted.wrap(
        "repurpose_pack",
        "\n".join(
            [
                f"SOCIAL: {pack.social_text}",
                f"HASHTAGS: {', '.join(pack.hashtags)}",
                f"EMAIL_SUBJECT: {pack.email_subject}",
                f"EMAIL_BODY: {pack.email_body}",
                f"VIDEO_SCRIPT: {pack.video_script}",
            ]
        ),
    )


def pack_to_dict(pack: RepurposePack) -> dict:
    return {
        "social_text": pack.social_text,
        "hashtags": list(pack.hashtags),
        "email_subject": pack.email_subject,
        "email_body": pack.email_body,
        "video_script": pack.video_script,
    }


def _sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+", text)
    out = []
    for part in parts:
        s = part.strip()
        if len(s) >= 40:
            out.append(s[:240])
        if len(out) >= 5:
            break
    return out


def _hashtags(title: str, body: str) -> list[str]:
    words = re.findall(r"[A-Za-z]{4,}", f"{title} {body[:400]}")
    seen: list[str] = []
    for w in words:
        tag = w.capitalize()
        if tag.lower() in {t.lower() for t in seen}:
            continue
        seen.append(tag)
        if len(seen) >= 4:
            break
    return seen or ["Update"]
