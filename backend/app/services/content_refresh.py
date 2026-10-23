"""Detect pages whose Search Console figures have slipped."""
from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit


#: Minimum prior-window clicks before a drop counts (avoids noise on tiny URLs).
MIN_PRIOR_CLICKS = 10
#: Relative click drop that triggers a refresh candidate (30%).
CLICK_DROP_RATIO = 0.30
#: Absolute average-position worsening (e.g. 8.0 → 12.0).
POSITION_WORSEN_BY = 3.0


@dataclass(frozen=True, slots=True)
class DeclineSignal:
    url: str
    reason_codes: tuple[str, ...]
    prior_clicks: int
    recent_clicks: int
    prior_position: float | None
    recent_position: float | None


def normalise_page_url(url: str) -> str:
    """Strip fragments; keep scheme/host/path/query for matching SeoPage.url."""
    raw = (url or "").strip()
    if not raw:
        return ""
    parts = urlsplit(raw if "//" in raw else f"https://{raw}")
    path = parts.path or "/"
    query = f"?{parts.query}" if parts.query else ""
    host = (parts.netloc or "").lower().removeprefix("www.")
    scheme = parts.scheme or "https"
    return f"{scheme}://{host}{path}{query}"


def path_key(url: str) -> str:
    """Host+path without scheme/www for fuzzy matching synced pages."""
    parts = urlsplit(normalise_page_url(url))
    host = (parts.netloc or "").lower().removeprefix("www.")
    path = parts.path or "/"
    return f"{host}{path}".rstrip("/") or host


def decline_signals(rows: list[dict]) -> list[DeclineSignal]:
    """Pick pages with a material click drop and/or position worsening."""
    out: list[DeclineSignal] = []
    for row in rows:
        url = str(row.get("url") or "").strip()
        if not url:
            continue
        prior_clicks = int(row.get("prior_clicks") or 0)
        recent_clicks = int(row.get("recent_clicks") or 0)
        prior_pos = row.get("prior_position")
        recent_pos = row.get("recent_position")
        reasons: list[str] = []

        if prior_clicks >= MIN_PRIOR_CLICKS:
            dropped = prior_clicks - recent_clicks
            if dropped > 0 and dropped / prior_clicks >= CLICK_DROP_RATIO:
                reasons.append("click_drop")

        if (
            prior_clicks >= MIN_PRIOR_CLICKS
            and isinstance(prior_pos, (int, float))
            and isinstance(recent_pos, (int, float))
            and not isinstance(prior_pos, bool)
            and not isinstance(recent_pos, bool)
            and recent_pos - prior_pos >= POSITION_WORSEN_BY
        ):
            reasons.append("position_worsened")

        if reasons:
            out.append(
                DeclineSignal(
                    url=url,
                    reason_codes=tuple(reasons),
                    prior_clicks=prior_clicks,
                    recent_clicks=recent_clicks,
                    prior_position=float(prior_pos) if isinstance(prior_pos, (int, float)) else None,
                    recent_position=float(recent_pos) if isinstance(recent_pos, (int, float)) else None,
                )
            )
    out.sort(
        key=lambda s: (s.prior_clicks - s.recent_clicks, len(s.reason_codes)),
        reverse=True,
    )
    return out
