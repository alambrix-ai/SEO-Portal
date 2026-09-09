"""Connector registry.

Like the agent registry, this discovers packages under ``app/connectors/`` and
reads each one's ``CONNECTOR_CLASS`` export. A new integration is a new folder;
nothing central needs editing, and every new organisation is provisioned with
whatever is registered at the time.
"""
from __future__ import annotations

import importlib
import pkgutil
from pathlib import Path

from app.connectors.base.connector import BaseConnector, Capability, ConnectorSpec
from app.core.logging import get_logger

log = get_logger(__name__)

# Marketplace order: the filter chips on the Connectors screen follow this.
CATEGORY_ORDER: tuple[str, ...] = (
    "CMS",
    "Ad Platforms",
    "Analytics & Search",
    "CRM",
    "AEO/LLM Monitoring",
    "Collaboration",
)

# Display order within the marketplace grid.
CATALOG_ORDER: tuple[str, ...] = (
    "wordpress", "shopify", "magento", "webflow", "custom_api",
    "google_ads", "meta_ads", "linkedin_ads", "tiktok_ads", "microsoft_ads", "dsp_exchange",
    "google_analytics_4", "search_console", "adobe_analytics", "bing_webmaster",
    "google_business_profile",
    "salesforce", "hubspot",
    "openai", "perplexity", "google_gemini", "anthropic_claude",
    "slack", "smtp_email", "zapier_webhooks",
)

_classes: dict[str, type[BaseConnector]] = {}
_loaded = False


def _discover() -> None:
    global _loaded
    if _loaded:
        return

    package_dir = Path(__file__).resolve().parent.parent
    for module_info in pkgutil.iter_modules([str(package_dir)]):
        if not module_info.ispkg or module_info.name == "base":
            continue
        module_name = f"app.connectors.{module_info.name}"
        try:
            module = importlib.import_module(module_name)
        except Exception as exc:  # noqa: BLE001 - a broken integration must
            # not take down the marketplace or the API.
            log.error("Could not import connector package %s: %s", module_name, exc)
            continue

        klass = getattr(module, "CONNECTOR_CLASS", None)
        if klass is None:
            log.warning("%s exports no CONNECTOR_CLASS; skipping", module_name)
            continue
        if not (isinstance(klass, type) and issubclass(klass, BaseConnector)):
            log.error("%s.CONNECTOR_CLASS is not a BaseConnector; skipping", module_name)
            continue
        if klass.spec.slug != module_info.name:
            log.error(
                "Connector slug %r does not match its package %r; skipping",
                klass.spec.slug,
                module_info.name,
            )
            continue
        _classes[klass.spec.slug] = klass

    _loaded = True
    log.info("Registered %d connectors", len(_classes))


def _order_key(slug: str) -> tuple[int, str]:
    try:
        return (CATALOG_ORDER.index(slug), slug)
    except ValueError:
        return (len(CATALOG_ORDER), slug)


def register(klass: type[BaseConnector]) -> None:
    """Register a connector class explicitly.

    Package discovery covers the shipped catalogue; this is the seam for
    anything supplied out-of-band — a private integration built against the
    same contract, or a test double.
    """
    _discover()
    if not (isinstance(klass, type) and issubclass(klass, BaseConnector)):
        raise TypeError("register() expects a BaseConnector subclass")
    _classes[klass.spec.slug] = klass


def all_specs() -> list[ConnectorSpec]:
    _discover()
    return [_classes[slug].spec for slug in sorted(_classes, key=_order_key)]


def get_class(slug: str) -> type[BaseConnector] | None:
    _discover()
    return _classes.get(slug)


def get_spec(slug: str) -> ConnectorSpec | None:
    klass = get_class(slug)
    return klass.spec if klass else None


def connector_slugs() -> list[str]:
    _discover()
    return sorted(_classes, key=_order_key)


def categories() -> list[str]:
    """Categories actually present, in marketplace order."""
    present = {spec.category for spec in all_specs()}
    ordered = [c for c in CATEGORY_ORDER if c in present]
    return ordered + sorted(present - set(ordered))


def slugs_with_capability(capability: Capability) -> list[str]:
    return [spec.slug for spec in all_specs() if capability in spec.capabilities]


def reset_registry() -> None:
    """Force rediscovery — used by tests."""
    global _loaded
    _classes.clear()
    _loaded = False
