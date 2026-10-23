"""Walk FastAPI routers across 0.115 and 0.161+ include shapes.

FastAPI 0.161 stores ``include_router`` results as ``_IncludedRouter``
wrappers without a top-level ``path``. Tests that assert mount points must
walk ``original_router`` (and any nested includes) instead of reading
``api_router.routes`` flat.
"""
from __future__ import annotations

from collections.abc import Iterable, Iterator
from typing import Any


def iter_api_routes(router: Any) -> Iterator[Any]:
    """Yield leaf route objects that expose ``path`` (and usually ``methods``)."""
    for entry in getattr(router, "routes", []) or []:
        path = getattr(entry, "path", None)
        if isinstance(path, str) and path:
            yield entry
        nested = getattr(entry, "original_router", None)
        if nested is not None:
            yield from iter_api_routes(nested)


def route_paths(router: Any) -> set[str]:
    return {r.path for r in iter_api_routes(router)}


def routes_by_path(router: Any) -> dict[str, Any]:
    """Last route wins when the same path is registered more than once."""
    return {r.path: r for r in iter_api_routes(router)}


def route_path_methods(router: Any) -> set[tuple[str, tuple[str, ...]]]:
    out: set[tuple[str, tuple[str, ...]]] = set()
    for route in iter_api_routes(router):
        methods = getattr(route, "methods", None) or ()
        out.add((route.path, tuple(sorted(methods))))
    return out
