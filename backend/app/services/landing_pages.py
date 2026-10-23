"""Landing page drafts and A/B experiment helpers."""
from __future__ import annotations

import re
from dataclasses import dataclass

_SLUG = re.compile(r"[^a-z0-9]+")
ALLOWED_METRICS = frozenset({"sessions", "conversions", "bounce_rate"})


@dataclass(frozen=True, slots=True)
class LandingDraft:
    title: str
    path: str
    body_html: str


@dataclass(frozen=True, slots=True)
class ExperimentDraft:
    slug: str
    name: str
    hypothesis: str
    variant_weight: int
    metric_key: str


def sanitize_path(path: str) -> str:
    path = "/" + (path or "").strip().lstrip("/")
    path = re.sub(r"/{2,}", "/", path)
    if not path.startswith("/") or path.startswith("//") or "://" in path:
        raise ValueError("Path must be site-relative")
    if len(path) > 512:
        raise ValueError("Path is too long")
    return path


def slugify(value: str) -> str:
    slug = _SLUG.sub("-", (value or "").lower()).strip("-")
    return (slug or "experiment")[:80]


def build_landing_body(*, title: str, headline: str, body: str, cta: str) -> LandingDraft:
    title = (title or "Landing page").strip()[:255]
    headline = (headline or title).strip()[:200]
    body = (body or "").strip()
    if len(body) < 40:
        raise ValueError("Landing page body is too short")
    cta = (cta or "Get started").strip()[:80]
    html = (
        f"<main><h1>{_esc(headline)}</h1>"
        f"<p>{_esc(body)}</p>"
        f'<p><a href="#cta">{_esc(cta)}</a></p></main>'
    )
    path = sanitize_path(f"/lp/{slugify(title)}")
    return LandingDraft(title=title, path=path, body_html=html)


def validate_experiment(
    *,
    name: str,
    hypothesis: str = "",
    variant_weight: int = 50,
    metric_key: str = "sessions",
) -> ExperimentDraft:
    name = (name or "").strip()[:160]
    if len(name) < 3:
        raise ValueError("Experiment name is required")
    weight = int(variant_weight)
    if weight < 5 or weight > 95:
        raise ValueError("Variant weight must be between 5 and 95")
    metric = (metric_key or "sessions").strip().lower()
    if metric not in ALLOWED_METRICS:
        raise ValueError(f"Unsupported metric: {metric}")
    return ExperimentDraft(
        slug=slugify(name),
        name=name,
        hypothesis=(hypothesis or "").strip()[:2000],
        variant_weight=weight,
        metric_key=metric,
    )


def record_metric_point(
    *,
    experiment_id: str,
    variant: str,
    metric_key: str,
    value: float,
) -> dict:
    """Shape for metric_points.dimension — no PII."""
    if variant not in {"control", "variant"}:
        raise ValueError("variant must be control or variant")
    if metric_key not in ALLOWED_METRICS:
        raise ValueError(f"Unsupported metric: {metric_key}")
    return {
        "dimension": f"ab:{experiment_id}:{variant}",
        "metric": metric_key,
        "value": float(value),
    }


def _esc(text: str) -> str:
    return (
        (text or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )
