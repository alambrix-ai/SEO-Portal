"""Content-Security-Policy violation reports from the console.

* ``POST /csp-report``  where browsers send CSP violations (``report-uri`` and
  the ``Reporting-Endpoints`` header set by the console's nginx).

No login: browsers send these on their own, without credentials. That makes
it an open endpoint, so it does as little as possible:

* only ``application/csp-report`` (``report-uri``) and
  ``application/reports+json`` (Reporting API) bodies are read;
* the body is capped at :data:`MAX_REPORT_BYTES`, checked before and while
  reading;
* nothing from the report is echoed back: every outcome is an empty response;
* only three facts are logged, each reduced to a strict character set first:
  the violated directive, the blocked resource's host (or CSP keyword such as
  ``inline``) and the page's path. Query strings, fragments, script samples
  and full URLs are never logged, since they can carry tokens or personal data.

The general per-address API rate limit applies (RateLimitMiddleware).
"""
from __future__ import annotations

import json
import re
from urllib.parse import urlsplit

from fastapi import APIRouter, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.core.logging import get_logger

log = get_logger(__name__)

router = APIRouter(tags=["csp"])

#: Largest report body read. A real violation report is well under 2 KB.
MAX_REPORT_BYTES = 8 * 1024
#: Most reports logged from one Reporting API batch.
MAX_REPORTS_PER_BATCH = 10

LEGACY_CONTENT_TYPE = "application/csp-report"
REPORTING_API_CONTENT_TYPE = "application/reports+json"
ALLOWED_CONTENT_TYPES = frozenset({LEGACY_CONTENT_TYPE, REPORTING_API_CONTENT_TYPE})

#: What ``blocked-uri`` / ``blockedURL`` holds when no URL was involved.
_CSP_KEYWORDS = frozenset(
    {"inline", "eval", "wasm-eval", "data", "blob", "self", "trusted-types-policy", "trusted-types-sink"}
)
_DIRECTIVE = re.compile(r"[a-z][a-z-]{0,39}")
_HOST = re.compile(r"[a-z0-9.-]{1,253}")
_PATH = re.compile(r"/[A-Za-z0-9._~/-]{0,199}")
_UNKNOWN = "unknown"


class _LegacyBody(BaseModel):
    """The ``csp-report`` object a ``report-uri`` report carries."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    effective_directive: str = Field(default="", alias="effective-directive", max_length=200)
    violated_directive: str = Field(default="", alias="violated-directive", max_length=2000)
    blocked_uri: str = Field(default="", alias="blocked-uri", max_length=4096)
    document_uri: str = Field(default="", alias="document-uri", max_length=4096)


class _LegacyReport(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    csp_report: _LegacyBody = Field(alias="csp-report")


class _ReportBody(BaseModel):
    """The ``body`` of one Reporting API ``csp-violation`` report."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    effective_directive: str = Field(default="", alias="effectiveDirective", max_length=200)
    blocked_url: str = Field(default="", alias="blockedURL", max_length=4096)
    document_url: str = Field(default="", alias="documentURL", max_length=4096)


class _Report(BaseModel):
    model_config = ConfigDict(extra="ignore")

    type: str = Field(default="", max_length=64)
    body: _ReportBody | None = None


class _ReportBatch(BaseModel):
    reports: list[_Report] = Field(max_length=100)


def _directive(value: str) -> str:
    # violated-directive may hold the whole directive ("img-src 'self' ..."):
    # keep only its name.
    name = value.strip().split(" ", 1)[0].lower()
    return name if _DIRECTIVE.fullmatch(name) else _UNKNOWN


def _blocked_host(value: str) -> str:
    value = value.strip()
    if value.lower() in _CSP_KEYWORDS:
        return value.lower()
    try:
        host = urlsplit(value).hostname or ""
    except ValueError:
        return _UNKNOWN
    if not host:
        # e.g. a bare scheme such as "data:" or "chrome-extension".
        scheme = value.split(":", 1)[0].lower()
        return scheme if scheme in _CSP_KEYWORDS else _UNKNOWN
    return host if _HOST.fullmatch(host) else _UNKNOWN


def _document_path(value: str) -> str:
    try:
        path = urlsplit(value.strip()).path
    except ValueError:
        return _UNKNOWN
    return path if _PATH.fullmatch(path) else _UNKNOWN


def _violations(content_type: str, payload: object) -> list[tuple[str, str, str]]:
    """``(directive, blocked host, document path)`` for each usable report.

    Raises ``ValidationError`` when the payload does not have the expected shape.
    """
    if content_type == LEGACY_CONTENT_TYPE:
        body = _LegacyReport.model_validate(payload).csp_report
        directive = body.effective_directive or body.violated_directive
        return [(_directive(directive), _blocked_host(body.blocked_uri), _document_path(body.document_uri))]

    batch = _ReportBatch.model_validate({"reports": payload})
    return [
        (
            _directive(report.body.effective_directive),
            _blocked_host(report.body.blocked_url),
            _document_path(report.body.document_url),
        )
        for report in batch.reports[:MAX_REPORTS_PER_BATCH]
        if report.type == "csp-violation" and report.body is not None
    ]


async def _read_capped(request: Request) -> bytes | None:
    """The request body, or ``None`` once it passes :data:`MAX_REPORT_BYTES`."""
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > MAX_REPORT_BYTES:
            return None
    return bytes(body)


@router.post("/csp-report", status_code=status.HTTP_204_NO_CONTENT, include_in_schema=False)
async def receive_csp_report(request: Request) -> Response:
    """Log a browser's CSP violation report. Always answers with an empty body."""
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type not in ALLOWED_CONTENT_TYPES:
        return Response(status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE)

    declared = request.headers.get("content-length")
    if declared is not None:
        if not declared.isdigit():
            return Response(status_code=status.HTTP_400_BAD_REQUEST)
        if int(declared) > MAX_REPORT_BYTES:
            return Response(status_code=status.HTTP_413_CONTENT_TOO_LARGE)

    raw = await _read_capped(request)
    if raw is None:
        return Response(status_code=status.HTTP_413_CONTENT_TOO_LARGE)

    try:
        violations = _violations(content_type, json.loads(raw))
    except (ValueError, ValidationError):
        # json.JSONDecodeError and UnicodeDecodeError are ValueErrors. The
        # body is not logged: it is attacker-controlled.
        log.info("CSP report ignored: unreadable %s body (%d bytes)", content_type, len(raw))
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    for directive, blocked, path in violations:
        log.warning("CSP violation: directive=%s blocked=%s path=%s", directive, blocked, path)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
