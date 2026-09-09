"""Money formatting.

The platform reports the customer's ad spend, their cost per acquisition and
what invalid-traffic blocking saved them. Those are amounts a marketing lead
reads off a screen and repeats in a meeting, so they have to be in the
currency they actually budget in, grouped the way they expect to see it.

Two things this module exists to stop:

* **A hard-coded symbol in every f-string.** There were nine of them, which
  meant "change the currency" was a nine-file search-and-replace with no way
  to be sure it was complete. The symbol now lives in one setting.

* **Western digit grouping on Indian amounts.** ``1,234,567`` is not how
  ₹12,34,567 is written or read. The lakh/crore grouping is not decoration —
  a figure grouped in thousands is genuinely misread by someone who expects
  lakhs, and it is the tell that a product was localised by changing the
  symbol and nothing else.

Model API cost goes through here too. It reads zero until somebody sets
``LLM_PRICE_INPUT_PER_MTOK`` and ``LLM_PRICE_OUTPUT_PER_MTOK``, and those are
entered in the same currency as everything else on this screen — whatever the
deployment is actually contracted at. There is no built-in price table and no
conversion rate, because both would be invented numbers reported as spend.
"""
from __future__ import annotations

from app.core.config import settings


def group_indian(whole: str) -> str:
    """Group digits the Indian way: last three, then twos.

    ``1234567`` becomes ``12,34,567``. Below four digits nothing is grouped.
    """
    if len(whole) <= 3:
        return whole
    head, tail = whole[:-3], whole[-3:]
    parts: list[str] = []
    while len(head) > 2:
        parts.insert(0, head[-2:])
        head = head[:-2]
    if head:
        parts.insert(0, head)
    return ",".join([*parts, tail])


def _group(whole: str) -> str:
    if settings.currency_grouping == "indian":
        return group_indian(whole)
    return f"{int(whole):,}"


def money(amount: float, *, decimals: int = 0) -> str:
    """``₹12,34,567`` — an amount with its symbol and correct grouping.

    ``decimals`` is 0 for totals, where the paise are noise, and 2 for a cost
    per acquisition, where they are the point.
    """
    negative = amount < 0
    text = f"{abs(amount):.{decimals}f}"
    whole, _, fraction = text.partition(".")
    grouped = _group(whole)
    if fraction:
        grouped = f"{grouped}.{fraction}"
    return f"{'-' if negative else ''}{settings.currency_symbol}{grouped}"


def compact(amount: float) -> str:
    """A short form for tiles: ``₹1.2Cr``, ``₹4.5L``, ``₹12,500``.

    Crore and lakh rather than M and K, for the same reason as the grouping.
    """
    symbol = settings.currency_symbol
    if settings.currency_grouping != "indian":
        for size, suffix in ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "K")):
            if abs(amount) >= size:
                return f"{symbol}{amount / size:.1f}{suffix}".replace(".0", "")
        return money(amount)

    for size, suffix in ((10_000_000, "Cr"), (100_000, "L")):
        if abs(amount) >= size:
            trimmed = f"{amount / size:.2f}".rstrip("0").rstrip(".")
            return f"{symbol}{trimmed}{suffix}"
    return money(amount)
