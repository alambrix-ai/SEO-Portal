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
from app.core.config import settings
from app.core.logging import get_logger

log = get_logger(__name__)

# Marketplace order: the filter chips on the Connectors screen follow this.
CATEGORY_ORDER: tuple[str, ...] = (
    "CMS",
    "Local business",
    "Ad Platforms",
    "Social media",
    "Analytics & Search",
    "CRM",
    "Email marketing",
    "AEO/LLM Monitoring",
    "Collaboration",
)

# Display order within the marketplace grid.
CATALOG_ORDER: tuple[str, ...] = (
    "wordpress", "shopify", "magento", "webflow", "custom_api",
    "google_ads", "meta_ads", "linkedin_ads", "tiktok_ads", "microsoft_ads", "dsp_exchange",
    "google_analytics_4", "search_console", "adobe_analytics", "bing_webmaster",
    "dataforseo_backlinks",
    "google_business_profile", "google_places",
    "salesforce", "hubspot",
    "openai", "perplexity", "google_gemini", "anthropic_claude",
    "xai_grok", "vllm", "ollama",
    "facebook_pages", "instagram_business", "linkedin_pages", "x_twitter", "pinterest",
    "reddit",
    "slack", "smtp_email", "zapier_webhooks",
    "mailchimp", "brevo", "klaviyo",
)

#: Connectors left out of the catalogue while a platform setting is off.
#: They stay registered (``get_class`` / ``get_spec`` still find them), so a
#: connection made earlier can be read and disconnected; they are only no
#: longer offered, provisioned or picked. Reddit needs commercial API
#: approval first (``REDDIT_ENABLED``).
SWITCHED_OFF_UNLESS: dict[str, str] = {
    "reddit": "reddit_enabled",
}

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


def is_offered(slug: str) -> bool:
    """False while the platform setting this connector needs is off."""
    setting = SWITCHED_OFF_UNLESS.get(slug)
    return setting is None or bool(getattr(settings, setting, False))


def _offered_slugs() -> list[str]:
    return [slug for slug in sorted(_classes, key=_order_key) if is_offered(slug)]


def all_specs() -> list[ConnectorSpec]:
    """Every connector the catalogue offers, in marketplace order."""
    _discover()
    return [_classes[slug].spec for slug in _offered_slugs()]


def get_class(slug: str) -> type[BaseConnector] | None:
    _discover()
    return _classes.get(slug)


def get_spec(slug: str) -> ConnectorSpec | None:
    klass = get_class(slug)
    return klass.spec if klass else None


def connector_slugs() -> list[str]:
    """The slugs of :func:`all_specs`, in the same order."""
    _discover()
    return _offered_slugs()


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
