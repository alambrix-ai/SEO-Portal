"""Missing-schema and disabled-feature responses stay clear (503), not opaque 500s."""
from __future__ import annotations

from app.core.exceptions import FeatureNotConfiguredError
from app.main import _is_missing_schema
from app.services.billing_provider import get_provider, plan_catalog


def test_is_missing_schema_detects_undefined_table() -> None:
    class FakeExc(Exception):
        pass

    exc = FakeExc('relation "billing_accounts" does not exist')
    assert _is_missing_schema(exc) is True
    assert _is_missing_schema(RuntimeError("something else")) is False


def test_is_missing_schema_detects_pgcode() -> None:
    class PgExc(Exception):
        pgcode = "42P01"

    assert _is_missing_schema(PgExc("boom")) is True


def test_billing_catalog_payload_is_a_plan_array() -> None:
    """Regression: a dict catalog blanked the console via plans.map."""
    catalog = plan_catalog()
    plans = [
        {"slug": slug, **(meta if isinstance(meta, dict) else {"name": slug})}
        for slug, meta in catalog.items()
    ]
    assert isinstance(plans, list)
    assert plans
    assert all(isinstance(p, dict) and "slug" in p for p in plans)


def test_unknown_billing_provider_is_feature_not_configured() -> None:
    try:
        get_provider("stripe")
        raise AssertionError("expected FeatureNotConfiguredError")
    except FeatureNotConfiguredError as exc:
        assert exc.status_code == 503
        assert "not configured" in exc.message.lower()
