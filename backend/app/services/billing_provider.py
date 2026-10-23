"""Billing provider abstraction — fake provider for tests and local/dev.

Real payment provider credentials are never required. Prices live only in
``settings.billing_plan_catalog`` (config), never hard-coded in this module.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from app.core.config import settings
from app.db.base import new_id, utcnow


@dataclass(frozen=True, slots=True)
class ProviderCustomer:
    external_id: str
    status: str
    plan_slug: str
    seats: int
    current_period_end: datetime | None
    #: Opaque provider payload — never log secrets.
    meta: dict[str, Any]


class BillingProvider(ABC):
    name: str

    @abstractmethod
    def ensure_customer(
        self, *, tenant_id: str, plan_slug: str, seats: int, email_fingerprint: str
    ) -> ProviderCustomer:
        raise NotImplementedError

    @abstractmethod
    def change_plan(
        self, *, external_customer_id: str, plan_slug: str, seats: int
    ) -> ProviderCustomer:
        raise NotImplementedError

    @abstractmethod
    def report_usage(
        self, *, external_customer_id: str, metric: str, quantity: int
    ) -> None:
        raise NotImplementedError


class FakeBillingProvider(BillingProvider):
    """In-memory / deterministic provider — no network, no keys."""

    name = "fake"
    _customers: dict[str, ProviderCustomer]

    def __init__(self) -> None:
        self._customers = {}

    def ensure_customer(
        self, *, tenant_id: str, plan_slug: str, seats: int, email_fingerprint: str
    ) -> ProviderCustomer:
        _ = email_fingerprint  # used only to bind without storing PII
        catalog = plan_catalog()
        if plan_slug not in catalog:
            raise ValueError(f"unknown plan_slug: {plan_slug}")
        existing = self._customers.get(tenant_id)
        external_id = existing.external_id if existing else f"fake_{new_id()[:12]}"
        cust = ProviderCustomer(
            external_id=external_id,
            status="active" if plan_slug != "trial" else "trial",
            plan_slug=plan_slug,
            seats=max(1, int(seats)),
            current_period_end=utcnow() + timedelta(days=30),
            meta={"provider": "fake"},
        )
        self._customers[tenant_id] = cust
        return cust

    def reset(self) -> None:
        """Clear in-memory customers (tests only)."""
        self._customers.clear()

    def change_plan(
        self, *, external_customer_id: str, plan_slug: str, seats: int
    ) -> ProviderCustomer:
        catalog = plan_catalog()
        if plan_slug not in catalog:
            raise ValueError(f"unknown plan_slug: {plan_slug}")
        for tid, cust in list(self._customers.items()):
            if cust.external_id == external_customer_id:
                updated = ProviderCustomer(
                    external_id=cust.external_id,
                    status="active",
                    plan_slug=plan_slug,
                    seats=max(1, int(seats)),
                    current_period_end=utcnow() + timedelta(days=30),
                    meta=cust.meta,
                )
                self._customers[tid] = updated
                return updated
        raise LookupError("customer not found")

    def report_usage(
        self, *, external_customer_id: str, metric: str, quantity: int
    ) -> None:
        # Fake provider accepts usage for any known customer; no egress.
        if not any(c.external_id == external_customer_id for c in self._customers.values()):
            raise LookupError("customer not found")
        if quantity < 0:
            raise ValueError("quantity must be non-negative")
        _ = metric


_FAKE = FakeBillingProvider()


def plan_catalog() -> dict[str, dict[str, Any]]:
    """Plan definitions and prices from config only."""
    raw = settings.billing_plan_catalog or {}
    if not isinstance(raw, dict) or not raw:
        # Safe defaults for tests / unset env — amounts are placeholders in config units.
        return {
            "trial": {
                "name": "Trial",
                "price_monthly": 0,
                "currency": "USD",
                "seats": 3,
                "features": {
                    "site_analysis": {"allowed": True, "max_per_day": 3},
                    "audit_history": {"allowed": True, "max_history": 30},
                },
                "metrics": {"agent_runs": 100, "llm_tokens": 500_000, "api_calls": 1_000},
            },
            "growth": {
                "name": "Growth",
                "price_monthly": 0,  # real price must come from BILLING_PLAN_CATALOG
                "currency": "USD",
                "seats": 10,
                "features": {
                    "site_analysis": {"allowed": True, "max_per_day": 20},
                    "audit_history": {"allowed": True, "max_history": 180},
                },
                "metrics": {"agent_runs": 2_000, "llm_tokens": 5_000_000, "api_calls": 50_000},
            },
            "enterprise": {
                "name": "Enterprise",
                "price_monthly": 0,
                "currency": "USD",
                "seats": 50,
                "features": {
                    "site_analysis": {"allowed": True, "max_per_day": None},
                    "audit_history": {"allowed": True, "max_history": None},
                },
                "metrics": {"agent_runs": None, "llm_tokens": None, "api_calls": None},
            },
        }
    return raw


def get_provider(name: str | None = None) -> BillingProvider:
    chosen = (name or settings.billing_provider or "fake").strip().lower()
    if chosen == "fake":
        return _FAKE
    # Real providers (Stripe etc.) are intentionally not wired until Dishant
    # certifies which one to use — refuse rather than half-implement.
    raise ValueError(
        f"billing provider {chosen!r} is not enabled; use 'fake' or configure a certified provider"
    )
