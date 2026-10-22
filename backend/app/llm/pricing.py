"""Estimated LLM cost, priced when it is read rather than when it is spent.

A run stores its tokens and the connector and model that used them. What
those tokens cost is worked out here, from ``LLM_PRICE_TABLE``, every time a
usage figure is shown — so correcting a price corrects every report at once,
and nothing about a vendor's rates is frozen into old rows.

Three rules:

**Unpriced is not free.** A model with no configured price gives ``None``,
which the console shows as "No price set". It is never ``0``: a zero would
read as spend nobody incurred.

**Most specific wins.** Keys are ``"<connector>:<model pattern>"`` with
shell-style wildcards (``*``, ``?``, ``[...]``). When several match, the one
whose *model* part pins down the most literal characters wins, then the one
with the fewest wildcards, then the same two tests on the connector part;
the key text breaks any remaining tie so the answer never depends on the
order the table was written in.

**Every figure is an estimate.** Vendors bill on their own meters; this is
tokens times a configured rate, and the console labels it so.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from fnmatch import fnmatchcase
from functools import lru_cache

from app.core.config import settings

#: One ``[...]`` character class, or a lone ``*`` or ``?``.
_WILDCARD = re.compile(r"\[[^\]]*\]|[*?]")
#: Rounded like ``http_provider.price`` so the two never disagree in the
#: last place.
_DECIMALS = 6


@dataclass(frozen=True, slots=True)
class Price:
    """Currency units per million tokens, and the table key that set them."""

    input_per_mtok: float
    output_per_mtok: float
    #: The ``LLM_PRICE_TABLE`` key, or ``""`` for the two flat settings.
    source: str


@dataclass(frozen=True, slots=True)
class _Entry:
    connector: str
    model: str
    rank: tuple[int, int, int, int, str]
    price: Price


def _specificity(pattern: str) -> tuple[int, int]:
    """``(literal characters, wildcard count)`` for one side of a key."""
    wildcards = len(_WILDCARD.findall(pattern))
    literal = len(_WILDCARD.sub("", pattern))
    return literal, wildcards


@lru_cache(maxsize=8)
def _compile(table: tuple[tuple[str, tuple[float, float]], ...]) -> tuple[_Entry, ...]:
    """The table as entries, most specific first. Cached per table content,
    so a changed setting (or a test's monkeypatch) is picked up at once."""
    entries: list[_Entry] = []
    for key, (price_in, price_out) in table:
        connector, sep, model = key.partition(":")
        # The settings validator checks the characters; a key without the
        # connector part cannot say which connector it prices, so it prices
        # nothing rather than every connector at once.
        if not sep or not connector or not model:
            continue
        model_literal, model_wild = _specificity(model)
        conn_literal, conn_wild = _specificity(connector)
        entries.append(
            _Entry(
                connector=connector.lower(),
                model=model.lower(),
                # Sorted ascending, so "more literal" is negated.
                rank=(-model_literal, model_wild, -conn_literal, conn_wild, key),
                price=Price(float(price_in), float(price_out), key),
            )
        )
    entries.sort(key=lambda e: e.rank)
    return tuple(entries)


def _table() -> tuple[_Entry, ...]:
    raw = settings.llm_price_table or {}
    frozen = tuple(
        sorted((str(k), (float(v[0]), float(v[1]))) for k, v in raw.items() if len(v) == 2)
    )
    return _compile(frozen)


def _flat_price() -> Price | None:
    """The two flat settings, when at least one of them is set.

    Both at zero is the shipped default and means "not configured", so it
    prices nothing — see the module docstring.
    """
    price_in = float(settings.llm_price_input_per_mtok or 0.0)
    price_out = float(settings.llm_price_output_per_mtok or 0.0)
    if price_in <= 0 and price_out <= 0:
        return None
    return Price(price_in, price_out, "")


def lookup(connector: str, model: str) -> Price | None:
    """The price for one connector and model, or ``None`` when none is set.

    A run from before models were recorded has neither; only the flat
    settings can price it, since no table key can be said to match a model
    nobody wrote down.
    """
    connector = (connector or "").strip().lower()
    model = (model or "").strip().lower()
    if connector and model:
        for entry in _table():
            if fnmatchcase(connector, entry.connector) and fnmatchcase(model, entry.model):
                return entry.price
    return _flat_price()


def cost_of(price: Price, tokens_in: int, tokens_out: int) -> float:
    """Tokens times the rate, per million, rounded like the provider's own."""
    return round(
        (max(0, tokens_in) / 1_000_000) * price.input_per_mtok
        + (max(0, tokens_out) / 1_000_000) * price.output_per_mtok,
        _DECIMALS,
    )


def estimate(
    connector: str, model: str, tokens_in: int | None, tokens_out: int | None
) -> tuple[float | None, bool]:
    """``(estimated cost, priced)`` for one connector, model and token count.

    ``(None, False)`` when no price is configured for the model. ``(None,
    True)`` when it is priced but the token counts are unknown (a
    self-hosted model that reported no usage): the rate exists, the amount
    does not. Never ``0`` for an unpriced model.
    """
    price = lookup(connector, model)
    if price is None:
        return None, False
    if tokens_in is None or tokens_out is None:
        return None, True
    return cost_of(price, tokens_in, tokens_out), True
