"""The contract every connector implements.

A connector is a class in its own package under ``app/connectors/<slug>/``
that declares a :class:`ConnectorSpec` and implements the capabilities it
actually has. Agents talk to capabilities, not vendors: the on-page agent asks
*a CMS* for pages and writes back to it, so adding WordPress or Shopify or a
bespoke webhook is a new folder rather than a change to the agent.

Every outbound call goes through :meth:`request`, which supplies TLS
verification, timeouts, retries with backoff, and redaction — so no connector
has to remember to do any of that, and no credential is ever logged.
"""
from __future__ import annotations

from abc import ABC
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

from app.connectors.base.credentials import CredentialField, Credentials
from app.core.exceptions import ConnectorError
from app.core.config import settings
from app.core.logging import get_logger

log = get_logger(__name__)

DEFAULT_TIMEOUT = 20.0
MAX_ATTEMPTS = 3


class Capability(StrEnum):
    """What a connector can do, from an agent's point of view."""

    # CMS
    LIST_PAGES = "list_pages"
    READ_PAGE = "read_page"
    WRITE_PAGE = "write_page"
    INJECT_SCHEMA = "inject_schema"
    # Ads
    READ_AD_PERFORMANCE = "read_ad_performance"
    WRITE_AD_BUDGET = "write_ad_budget"
    UPLOAD_CREATIVE = "upload_creative"
    PUSH_AUDIENCE = "push_audience"
    READ_TRAFFIC_QUALITY = "read_traffic_quality"
    BLOCK_PLACEMENT = "block_placement"
    # Analytics & search
    READ_PAGE_SPEED = "read_page_speed"
    READ_SESSIONS = "read_sessions"
    READ_REFERRERS = "read_referrers"
    READ_SEARCH_PERFORMANCE = "read_search_performance"
    READ_BACKLINKS = "read_backlinks"
    # CRM
    READ_CONVERSIONS = "read_conversions"
    READ_FIRST_PARTY_SIGNALS = "read_first_party_signals"
    # AEO / LLM monitoring
    CHECK_CITATIONS = "check_citations"
    # Collaboration
    SEND_NOTIFICATION = "send_notification"
    SEND_EMAIL = "send_email"


@dataclass(frozen=True, slots=True)
class ConnectorSpec:
    slug: str
    name: str
    category: str
    description: str = ""
    fields: tuple[CredentialField, ...] = ()
    capabilities: frozenset[Capability] = frozenset()
    docs_url: str = ""
    #: What this platform needs before a connection can work — a plan tier, a
    #: minimum version, an app type, a token scope. Shown in the dialog
    #: *before* the operator tries, because "your plan does not include the
    #: API" is not something a failed request can be relied on to explain, and
    #: it is not something they can fix by retyping the token.
    requirements: tuple[str, ...] = ()
    # Base URL for the vendor API; overridable per installation where the
    # customer self-hosts (Magento, custom endpoints).
    base_url: str = ""
    base_url_field: str = ""


@dataclass(slots=True)
class HealthReport:
    ok: bool
    detail: str = ""
    checked_at: datetime | None = None
    #: False when nothing was actually asked of the vendor. Credentials are
    #: verified at the moment they are submitted, so "fine" and "not checked"
    #: must not arrive as the same answer — otherwise a connector with no
    #: probe would report every wrong password as a successful connection.
    probed: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)


class BaseConnector(ABC):
    """Base class for every connector."""

    spec: ConnectorSpec

    def __init__(self, credentials: Credentials | None = None, *, org_id: str = "") -> None:
        if not getattr(self, "spec", None):
            raise TypeError(f"{type(self).__name__} must declare a class-level `spec`")
        self.credentials = credentials or Credentials()
        self.org_id = org_id
        self._client: httpx.Client | None = None

    # ── Identity ───────────────────────────────────────────────────────────
    @property
    def slug(self) -> str:
        return self.spec.slug

    @property
    def name(self) -> str:
        return self.spec.name

    def supports(self, capability: Capability) -> bool:
        return capability in self.spec.capabilities

    def require(self, capability: Capability) -> None:
        if not self.supports(capability):
            raise ConnectorError(f"{self.name} cannot {capability.value.replace('_', ' ')}")

    # ── HTTP ───────────────────────────────────────────────────────────────
    @property
    def base_url(self) -> str:
        if self.spec.base_url_field:
            configured = self.credentials.get(self.spec.base_url_field)
            if configured:
                return configured.rstrip("/")
        return self.spec.base_url.rstrip("/")

    def auth_headers(self) -> dict[str, str]:
        """Per-vendor auth. Overridden by most connectors."""
        return {}

    def _http(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(
                base_url=self.base_url,
                timeout=settings.connector_timeout_seconds,
                follow_redirects=True,
                # Certificate verification is never disabled: credentials and
                # customer content travel on these connections.
                verify=True,
                headers={"User-Agent": "AutoMarket-AI/1.0", "Accept": "application/json"},
            )
        return self._client

    @retry(
        retry=retry_if_exception_type((httpx.TransportError, httpx.HTTPStatusError)),
        stop=stop_after_attempt(settings.connector_max_attempts),
        wait=wait_exponential_jitter(initial=0.5, max=8),
        reraise=True,
    )
    def _send(self, request: httpx.Request) -> httpx.Response:
        response = self._http().send(request)
        # Only transient statuses are retried; a 4xx will not fix itself.
        if response.status_code in (429, 500, 502, 503, 504):
            response.raise_for_status()
        return response

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        json_body: Any = None,  # noqa: ANN401
        form: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        expect_json: bool = True,
    ) -> Any:  # noqa: ANN401
        """Make one authenticated call, with retries and redacted logging.

        ``form`` sends form-encoded fields instead of JSON. Only one vendor
        needs it — Bitbucket's commit endpoint takes the file contents as form
        fields rather than a JSON body — but reaching around this method to
        use the client directly would skip the retries, the error translation
        and the redacted logging, which is worse than one extra parameter.
        """
        if not self.base_url:
            raise ConnectorError(f"{self.name} has no base URL configured")
        if json_body is not None and form is not None:
            raise ValueError("Pass either json_body or form, not both")

        merged = {**self.auth_headers(), **(headers or {})}
        client = self._http()
        request = client.build_request(
            method.upper(), path, params=params, json=json_body, data=form, headers=merged
        )
        try:
            response = self._send(request)
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            log.warning(
                "%s %s %s failed after retries: HTTP %s body=%s",
                self.slug,
                method.upper(),
                path,
                status,
                (exc.response.text or "")[:300],
            )
            from app.core.user_messages import message_for_http_status

            raise ConnectorError(message_for_http_status(status, service=self.name)) from exc
        except httpx.TransportError as exc:
            log.warning("%s transport error on %s %s: %s", self.slug, method.upper(), path, exc)
            from app.core.user_messages import message_for_unreachable

            raise ConnectorError(message_for_unreachable(self.name)) from exc

        if response.status_code >= 400:
            # Vendor bodies may contain keys or internals — log only.
            log.warning(
                "%s rejected %s %s with HTTP %s: %s",
                self.slug,
                method.upper(),
                path,
                response.status_code,
                response.text[:300],
            )
            from app.core.user_messages import message_for_http_status

            raise ConnectorError(
                message_for_http_status(response.status_code, service=self.name)
            )

        log.debug("%s %s %s -> %s", self.slug, method.upper(), path, response.status_code)
        if not expect_json:
            return response.text
        if not response.content:
            return {}
        try:
            return response.json()
        except ValueError as exc:
            raise ConnectorError(
                f"{self.name} sent a response that could not be read. Try again."
            ) from exc

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    # ── Lifecycle hooks ────────────────────────────────────────────────────
    def check_health(self) -> HealthReport:
        """Verify the credentials still work.

        The default reports unknown rather than guessing; connectors that have
        a cheap probe endpoint override it.
        """
        from app.db.base import utcnow

        return HealthReport(
            ok=True,
            detail="No health probe implemented",
            checked_at=utcnow(),
            probed=False,
        )

    def diagnose(self, error: Exception) -> str | None:
        """Turn a vendor's refusal into something the operator can act on.

        The raw error is usually accurate and useless: "returned 401" does not
        distinguish a mistyped token from a plan that has no API at all, and
        the second cannot be fixed by trying again. A connector that knows its
        platform's limitations translates here; the rest return None and the
        original error stands.
        """
        return None

    def on_connect(self) -> None:
        """Called once after credentials are saved (register webhooks, etc.)."""

    def on_disconnect(self) -> None:
        """Called before credentials are removed (deregister webhooks, etc.)."""

    def __repr__(self) -> str:  # pragma: no cover
        return f"<{type(self).__name__} {self.spec.slug}>"

    def __enter__(self) -> BaseConnector:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
