"""Which catalogue entries and modules are visible across every workspace.

Portal Admin toggles these. Customers never see a disabled connector, agent,
or nav module — list APIs, provisioning, and runs all consult this service.
"""
from __future__ import annotations

import threading
import time
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.base.registry import all_agents
from app.connectors.base import registry as connector_registry
from app.core.logging import get_logger
from app.core.rbac import Module
from app.models.portal import FeatureKind, PortalFeatureFlag

if TYPE_CHECKING:
    from app.models.workspace import User

log = get_logger(__name__)

# In-process cache: one Render free instance is the common deploy. Invalidate
# on every write so toggles apply on the next request without waiting.
_CACHE_TTL_SECONDS = 5.0
_lock = threading.Lock()
_cache_at = 0.0
_cache: dict[tuple[str, str], bool] = {}


def invalidate_cache() -> None:
    global _cache_at, _cache
    with _lock:
        _cache_at = 0.0
        _cache = {}


def _load_disabled(db: Session) -> dict[tuple[str, str], bool]:
    """Map (kind, slug) → enabled for rows that exist."""
    rows = db.execute(select(PortalFeatureFlag)).scalars().all()
    return {(row.kind, row.slug): bool(row.enabled) for row in rows}


def _snapshot(db: Session) -> dict[tuple[str, str], bool]:
    global _cache_at, _cache
    now = time.monotonic()
    with _lock:
        if _cache and (now - _cache_at) < _CACHE_TTL_SECONDS:
            return dict(_cache)
    fresh = _load_disabled(db)
    with _lock:
        _cache = fresh
        _cache_at = time.monotonic()
        return dict(fresh)


def is_enabled(db: Session, kind: FeatureKind | str, slug: str) -> bool:
    """True when the feature is on. Missing row → enabled."""
    key = (str(kind), slug)
    flags = _snapshot(db)
    if key not in flags:
        return True
    return flags[key]


def enabled_connector_slugs(db: Session) -> frozenset[str]:
    return frozenset(
        spec.slug
        for spec in connector_registry.all_specs()
        if is_enabled(db, FeatureKind.CONNECTOR, spec.slug)
    )


def enabled_agent_slugs(db: Session) -> frozenset[str]:
    return frozenset(
        agent.spec.slug
        for agent in all_agents()
        if is_enabled(db, FeatureKind.AGENT, agent.spec.slug)
    )


def enabled_modules(db: Session) -> frozenset[str]:
    return frozenset(
        module.value
        for module in Module
        if is_enabled(db, FeatureKind.MODULE, module.value)
    )


def catalogue_entries() -> list[tuple[str, str, str]]:
    """(kind, slug, display_name) for every toggleable entry."""
    out: list[tuple[str, str, str]] = []
    for spec in connector_registry.all_specs():
        out.append((FeatureKind.CONNECTOR.value, spec.slug, spec.name))
    for agent in all_agents():
        out.append((FeatureKind.AGENT.value, agent.spec.slug, agent.spec.name))
    for module in Module:
        out.append((FeatureKind.MODULE.value, module.value, module.value.replace("_", " ").title()))
    return out


def list_flags(db: Session) -> list[dict]:
    """Every catalogue entry with its current enabled state."""
    flags = _snapshot(db)
    items = []
    for kind, slug, name in catalogue_entries():
        enabled = flags.get((kind, slug), True)
        items.append(
            {
                "kind": kind,
                "slug": slug,
                "name": name,
                "enabled": enabled,
            }
        )
    return items


def set_flag(
    db: Session,
    *,
    kind: str,
    slug: str,
    enabled: bool,
    actor: User,
) -> PortalFeatureFlag:
    """Upsert one flag. Raises ValueError if kind/slug is not in the catalogue."""
    valid = {(k, s) for k, s, _ in catalogue_entries()}
    if (kind, slug) not in valid:
        raise ValueError(f"Unknown feature {kind}/{slug}")

    row = db.execute(
        select(PortalFeatureFlag).where(
            PortalFeatureFlag.kind == kind,
            PortalFeatureFlag.slug == slug,
        )
    ).scalar_one_or_none()

    email = (getattr(actor, "email", None) or "").strip() or actor.id
    if row is None:
        row = PortalFeatureFlag(
            kind=kind,
            slug=slug,
            enabled=enabled,
            updated_by=email,
        )
        db.add(row)
    else:
        row.enabled = enabled
        row.updated_by = email

    db.flush()
    invalidate_cache()
    log.info(
        "Portal feature %s/%s set to %s by %s",
        kind,
        slug,
        "on" if enabled else "off",
        email,
    )
    return row
