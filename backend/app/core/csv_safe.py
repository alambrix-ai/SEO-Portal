"""CSV formula-injection defences.

A spreadsheet treats a cell that starts with ``=``, ``+``, ``-`` or ``@`` as a
formula, and a leading tab or carriage return can hide one from a glance. An
audit row whose action text reads ``=HYPERLINK("https://evil.example", "Open")``
would otherwise become a live link the moment an administrator opened the
export in Excel. There are two kinds of export here, and they need opposite
treatment:

* **Files people open in a spreadsheet** (the audit export and any other
  human-readable CSV) use :func:`neutralise`, the full OWASP rule: any cell
  starting with ``= + - @ \\t \\r`` gets a leading apostrophe, which the
  spreadsheet shows as text and does not evaluate.

* **Files a tool imports** (the Google Ads Editor export) must NOT be
  apostrophe-escaped. Ads Editor would import the apostrophe as part of the
  keyword. :func:`check_ads_editor_cell` validates instead. It refuses a
  leading ``=`` or ``@`` and strips a leading ``+``, because broad-match
  modifiers were retired in 2021. A leading ``-`` passes through on purpose,
  since Ads Editor reads it as negative-keyword syntax. Negatives should still
  be written through the match-type column ("Negative Exact" and so on). This
  is a deliberate, documented exception to the OWASP rule.
"""
from __future__ import annotations

import csv
import io
from collections.abc import Iterable, Sequence
from typing import Any

#: Leading characters a spreadsheet may evaluate (OWASP CSV injection).
FORMULA_TRIGGERS: tuple[str, ...] = ("=", "+", "-", "@", "\t", "\r")

#: Leading characters Google Ads Editor cells must never start with.
ADS_EDITOR_REJECTED: tuple[str, ...] = ("=", "@")

# Whitespace a spreadsheet may skip before it looks for a formula.
_LEADING_WS = " \t\r\n"


class UnsafeCellError(ValueError):
    """A cell for a tool-imported CSV would be read as a formula."""


def _text(value: Any) -> str:  # noqa: ANN401 - any cell value
    return "" if value is None else str(value)


def neutralise(value: Any) -> str:  # noqa: ANN401 - any cell value
    """One cell, made safe to open in a spreadsheet.

    Cells that do not start with a trigger character pass through unchanged,
    so ordinary text is not altered.
    """
    text = _text(value)
    if text.startswith(FORMULA_TRIGGERS):
        return "'" + text
    return text


def neutralise_row(cells: Iterable[Any]) -> list[str]:
    return [neutralise(cell) for cell in cells]


def rows_to_csv(header: Sequence[Any], rows: Iterable[Sequence[Any]]) -> bytes:
    """A UTF-8 CSV (with BOM, CRLF) in which every cell is neutralised.

    The header is neutralised too, since it can carry user-chosen names as
    well as the data can.
    """
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(neutralise_row(header))
    for row in rows:
        writer.writerow(neutralise_row(row))
    return ("﻿" + buffer.getvalue()).encode("utf-8")


def check_ads_editor_cell(value: Any) -> str:  # noqa: ANN401 - any cell value
    """Validate one Google Ads Editor cell. This checks; it does not escape.

    * A leading ``=`` or ``@`` (after any leading whitespace) raises
      :class:`UnsafeCellError`. Neither has a meaning in Ads Editor, so the
      cell can only be a formula.
    * A leading ``+`` is stripped. Broad-match-modifier syntax was retired in
      2021, so it carries no meaning and is a formula trigger. The rest of
      the cell is then checked again.
    * A leading ``-`` is left alone, because Ads Editor reads it as a
      negative keyword.
    """
    text = _text(value)
    candidate = text
    while candidate.lstrip(_LEADING_WS).startswith("+"):
        candidate = candidate.lstrip(_LEADING_WS)[1:]
    if candidate.lstrip(_LEADING_WS).startswith(ADS_EDITOR_REJECTED):
        raise UnsafeCellError("A cell in this export starts with a formula character.")
    return candidate


def check_ads_editor_row(cells: Iterable[Any]) -> list[str]:
    return [check_ads_editor_cell(cell) for cell in cells]
