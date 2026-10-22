"""Download a website analysis report as a PDF or a standalone HTML page.

Both are built from the stored report (and, for workspace runs, the
comparison with the previous run and what was connected at the time), so a
download always matches what the screen shows. Everything under
``report["ai"]`` is model output and is read defensively.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any

#: format -> (label, media type, extension)
FORMATS: dict[str, tuple[str, str, str]] = {
    "pdf": ("PDF", "application/pdf", "pdf"),
    "html": ("Interactive web report (HTML)", "text/html; charset=utf-8", "html"),
}

STATUS_LABEL = {"fail": "Fix", "warn": "Improve", "info": "Note", "pass": "Good"}


# ── Reading the report safely ───────────────────────────────────────────────
def _d(v: Any) -> dict[str, Any]:
    return v if isinstance(v, dict) else {}


def _l(v: Any) -> list[Any]:
    return v if isinstance(v, list) else []


def _s(v: Any) -> str:
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return str(v)
    return v.strip() if isinstance(v, str) else ""


def host(url: str) -> str:
    return re.sub(r"^https?://", "", url or "").rstrip("/")


def filename(report: dict[str, Any], fmt: str) -> str:
    site = re.sub(r"[^a-z0-9.-]+", "-", host(_s(report.get("website"))).lower()).strip("-") or "website"
    day = _s(report.get("generated_at"))[:10] or datetime.now().strftime("%Y-%m-%d")
    return f"analysis-{site}-{day}.{FORMATS[fmt][2]}"


def _date(value: str) -> str:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).strftime("%d %b %Y")
    except (ValueError, AttributeError):
        return value[:10] if value else ""


def _sections(report: dict[str, Any]) -> dict[str, Any]:
    ai = _d(report.get("ai"))
    strategy, creative = _d(ai.get("strategy")), _d(ai.get("creative"))
    return {
        "ai": ai,
        "strategy": strategy,
        "creative": creative,
        "business": _d(strategy.get("business")),
        "messaging": _d(strategy.get("messaging")),
        "campaign": _d(creative.get("search_campaign")),
        "roadmap": _d(strategy.get("roadmap")),
    }


def render(report: dict[str, Any], fmt: str, *, extra: dict[str, Any] | None = None) -> tuple[bytes, str, str]:
    """``(content, media type, filename)``."""
    from app.core.exceptions import InvalidInputError

    if fmt not in FORMATS:
        raise InvalidInputError(f"Unknown format {fmt!r}. Use one of: {', '.join(FORMATS)}.")
    if fmt == "pdf":
        from app.services.demo_analysis.export_pdf import to_pdf

        content = to_pdf(report, extra=extra)
    else:
        from app.services.demo_analysis.export_html import to_html

        content = to_html(report, extra=extra)
    return content, FORMATS[fmt][1], filename(report, fmt)
