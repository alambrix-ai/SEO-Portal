"""Sandboxed Jinja rendering for programmatic SEO templates.

Customer templates are never given a filesystem loader and cannot include
remote URLs. Only the single named template ``page`` is resolvable.
"""
from __future__ import annotations

import re
from typing import Any

from jinja2 import BaseLoader, StrictUndefined, TemplateNotFound
from jinja2.sandbox import SandboxedEnvironment

_SLUG = re.compile(r"[^a-z0-9]+")


class _SingleTemplateLoader(BaseLoader):
    """Only the template named ``page`` exists — blocks include/extends chains."""

    def __init__(self, source: str) -> None:
        self._source = source

    def get_source(self, environment: SandboxedEnvironment, template: str):  # noqa: ANN201
        if template != "page":
            raise TemplateNotFound(template)
        return self._source, None, lambda: True


def render_body(template_source: str, row: dict[str, Any]) -> str:
    """Render one page body. Raises ``ValueError`` on unsafe or broken templates."""
    source = (template_source or "").strip()
    if not source:
        raise ValueError("Template body is empty")
    if len(source) > 100_000:
        raise ValueError("Template body is too large")
    # Refuse obvious server-side include / fetch attempts in the source text.
    lowered = source.lower()
    for banned in ("{% include", "{% extends", "{% import", "jinja2.utils", "__class__"):
        if banned in lowered:
            raise ValueError("Template uses a disallowed construct")
    env = SandboxedEnvironment(
        loader=_SingleTemplateLoader(source),
        autoescape=True,
        undefined=StrictUndefined,
        extensions=[],
    )
    # No custom filters that could touch the network or filesystem.
    try:
        return env.get_template("page").render(**_safe_context(row))
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"Template render failed: {exc}") from exc


def render_path(pattern: str, row: dict[str, Any]) -> str:
    """Expand a path pattern with row fields; always returns a site-relative path."""
    ctx = _safe_context(row)
    path = pattern or "/{slug}"
    for key, value in ctx.items():
        path = path.replace("{" + key + "}", _slugify(str(value)))
    if "{slug}" in path:
        path = path.replace("{slug}", _slugify(str(ctx.get("slug") or ctx.get("title") or "page")))
    path = "/" + path.lstrip("/")
    path = re.sub(r"/{2,}", "/", path)
    if not path.startswith("/") or path.startswith("//") or "://" in path:
        raise ValueError("Path must be site-relative")
    return path[:512]


def title_for(row: dict[str, Any], rendered_body: str = "") -> str:
    for key in ("title", "name", "heading", "city", "product"):
        if row.get(key):
            return str(row[key]).strip()[:255]
    # First heading-ish line from body
    for line in (rendered_body or "").splitlines():
        text = re.sub(r"<[^>]+>", "", line).strip(" #")
        if text:
            return text[:255]
    return "Programmatic page"


def _safe_context(row: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in (row or {}).items():
        name = str(key)
        if not (name.isidentifier() or name.replace("_", "").isalnum()):
            continue
        if isinstance(value, (str, int, float, bool)) or value is None:
            out[name] = value
        else:
            out[name] = str(value)[:2000]
    if "slug" not in out:
        out["slug"] = _slugify(str(out.get("title") or out.get("name") or "page"))
    return out


def _slugify(value: str) -> str:
    slug = _SLUG.sub("-", (value or "").lower()).strip("-")
    return (slug or "page")[:80]
