"""Money formatting.

Indian grouping is the part worth testing. ``1,234,567`` and ``12,34,567``
are the same number and only one of them is read correctly by someone who
budgets in lakhs, and getting it wrong is the classic sign of a product
localised by swapping the symbol and nothing else.
"""
from __future__ import annotations

import pytest

from app.core.money import compact, group_indian, money


@pytest.mark.parametrize(
    ("digits", "grouped"),
    [
        ("0", "0"),
        ("12", "12"),
        ("123", "123"),
        ("1234", "1,234"),
        ("12345", "12,345"),
        ("123456", "1,23,456"),
        ("1234567", "12,34,567"),
        ("12345678", "1,23,45,678"),
        ("123456789", "12,34,56,789"),
    ],
)
def test_indian_grouping(digits: str, grouped: str):
    assert group_indian(digits) == grouped


def test_money_carries_the_symbol_and_the_grouping():
    assert money(1234567) == "\u20b912,34,567"
    assert money(1234.5, decimals=2) == "\u20b91,234.50"


def test_money_keeps_the_sign_outside_the_symbol():
    """``-₹8,400``, not ``₹-8,400`` — a negative amount reads as a deduction."""
    assert money(-8400) == "-\u20b98,400"


def test_totals_drop_the_paise_and_a_cac_keeps_them():
    """A CAC is the one figure where two decimals are the point.

    The whole job of the budget engine is moving spend between channels whose
    cost per acquisition differs by a few rupees.
    """
    assert money(52340.75) == "\u20b952,341"
    assert money(52340.75, decimals=2) == "\u20b952,340.75"


@pytest.mark.parametrize(
    ("amount", "text"),
    [
        (950, "\u20b9950"),
        (99_999, "\u20b999,999"),
        (100_000, "\u20b91L"),
        (1_234_567, "\u20b912.35L"),
        (10_000_000, "\u20b91Cr"),
        (45_678_901, "\u20b94.57Cr"),
    ],
)
def test_compact_uses_lakhs_and_crores(amount: float, text: str):
    """Not K and M. A spend tile reading "1.2M" tells an Indian team nothing."""
    assert compact(amount) == text


def test_western_grouping_is_still_available(monkeypatch):
    """The currency is configuration, not a hard-coded assumption."""
    from app.core.config import settings

    monkeypatch.setattr(settings, "currency_symbol", "$")
    monkeypatch.setattr(settings, "currency_grouping", "western")
    assert money(1234567) == "$1,234,567"
    assert compact(1_500_000) == "$1.5M"
