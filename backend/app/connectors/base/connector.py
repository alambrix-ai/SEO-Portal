"""The contract every connector implements.

A connector is a class in its own package under ``app/connectors/<slug>/``
that declares a :class:`ConnectorSpec` and implements the capabilities it
actually has. Agents talk to capabilities, not vendors: the on-page agent asks
*a CMS* for pages and writes back to it, so adding WordPress or Shopify or a
bespoke webhook is a new folder rather than a change to the agent.

Every outbound call goes through :meth:`request`, which supplies TLS
verification, timeouts, retries with backoff, and redaction — so no connector
has to remember to do any of that, and no credential is ever logged.

A connector whose address the customer types (``ConnectorSpec.base_url_field``:
a CMS, a webhook, a CRM instance, a site to crawl) talks through the egress
guard (:mod:`app.core.egress`): https only, public addresses only, the socket
pinned to the address that was checked, redirects followed by hand and
re-checked on every hop, and the reply size capped. Private hosts an
administrator lists in ``CONNECTOR_PRIVATE_HOSTS_ALLOWED`` (an intranet
WordPress or Magento) are let through, still over https only; cloud metadata
never is. Vendor-fixed
addresses (``api.openai.com``) keep the plain client.
"""
from __future__ import annotations

from abc import ABC
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

import httpcore
import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

from app.connectors.base.credentials import CredentialField, Credentials
from app.core import egress
from app.core.exceptions import ConnectorConfigError, ConnectorError
from app.core.config import settings
from app.core.logging import get_logger

log = get_logger(__name__)

DEFAULT_TIMEOUT = 20.0
MAX_ATTEMPTS = 3

#: Largest reply read from a customer-supplied address (a CMS page, a
#: sitemap, a CRM batch). Larger replies are refused, not truncated.
MAX_GUARDED_RESPONSE_BYTES = 10_000_000
#: Connectors that may read plain-http addresses. The Site Crawler reads a
#: customer's public website exactly as a search engine does, sends no
#: credentials, and plain-http sites (or http -> https redirects) worked before
#: the guard; it still gets public addresses only. Every other customer
#: address carries credentials or signed content, so it must be https.
PLAIN_HTTP_CONNECTORS = frozenset({"site_crawler"})
#: Egress refusals that concern one reply rather than the connection's
#: settings: the batch skips that item instead of failing as misconfigured.
_PER_REQUEST_EGRESS_REASONS = frozenset({"too_large", "redirects"})
#: Request headers httpx recomputes when a request is rebuilt for the guard.
_REBUILT_HEADERS = frozenset({"host", "content-length", "transfer-encoding"})


def private_hosts_allow_list() -> egress.AllowList:
    """The administrator's ``CONNECTOR_PRIVATE_HOSTS_ALLOWED``, as the egress
    allow-list for customer-address connectors. Read per client, so a
    changed setting applies to the next connection built."""
    return egress.AllowList.from_entries(settings.connector_private_hosts_allowed)


def _require_https(request: httpx.Request) -> None:
    """Request hook for credentialed customer-address connectors: every hop,
    redirects included, must be https, even to an allow-listed private host.
    (The egress guard would accept plain http to an allow-listed host; these
    connectors carry keys and passwords, so they never do.)"""
    if request.url.scheme != "https":
        raise egress.EgressBlockedError("scheme", egress.MSG_SCHEME)


def _cassette_replay_active() -> bool:
    """Whether a cassette replay (``cassettes.Player.active``) has replaced
    ``httpx.Client.send``: it answers from the recording and never reaches
    the network. The recorder's patch is not matched: it does send."""
    send = httpx.Client.send
    return getattr(send, "__module__", "") == "app.connectors.base.cassettes" and getattr(
        send, "__qualname__", ""
    ).startswith("Player.active.")


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
    # Text generation for agents (rewrite, outreach, creative, etc.)
    COMPLETE = "complete"
    # Local business
    READ_LOCAL_RANKINGS = "read_local_rankings"
    READ_REVIEWS = "read_reviews"
    REPLY_REVIEW = "reply_review"
    # Content creation and publishing
    CREATE_PAGE = "create_page"
    PUBLISH_SOCIAL = "publish_social"
    CREATE_EMAIL_CAMPAIGN = "create_email_campaign"
    CREATE_SEARCH_CAMPAIGN = "create_search_campaign"
    GENERATE_IMAGE = "generate_image"
    # Collaboration
    SEND_NOTIFICATION = "send_notification"
    SEND_EMAIL = "send_email"


@dataclass(frozen=True, slots=True)
class OAuthBinding:
    """How a connector is connected through a platform sign-in.

    ``provider`` names an entry in :mod:`app.connectors.base.oauth_providers`
    (``"google"``, ``"wordpress"``, …). The provider holds what is shared —
    endpoints, PKCE, client credentials — and this holds what is particular
    to the connector: the scopes it needs and the fields the platform fills
    in on the customer's behalf.
    """

    provider: str
    #: The minimum scopes this connector's own API calls need.
    scopes: tuple[str, ...] = ()
    #: Fields the platform supplies when connected through its sign-in — the
    #: OAuth client id and secret, and anything else that belongs to the
    #: platform's app rather than to the customer (Google Ads developer token).
    platform_fields: frozenset[str] = frozenset({"clientId", "clientSecret"})
    #: Settings besides the provider's client credentials that must be set
    #: before sign-in is offered (``google_ads_developer_token``).
    requires_settings: tuple[str, ...] = ()
    #: One plain sentence: what access is requested and why. Shown under the
    #: sign-in button.
    access_summary: str = ""
    #: Label of the thing chosen after sign-in ("property", "site").
    choice_noun: str = "account"
    #: The connector fields the "choose" step fills in. Hidden from the
    #: sign-in path's form, since the picker sets them.
    choice_fields: tuple[str, ...] = ()
    #: The paste-it-yourself credential fields. Hidden on the sign-in path
    #: and offered under Advanced.
    token_fields: frozenset[str] = frozenset(
        {"refreshToken", "accessToken", "clientId", "clientSecret"}
    )
    # ── Phase 2B (business apps) ───────────────────────────────────────────
    #: The vendor's own wording for the button ("Add to Slack", "Install on
    #: GitHub"). Empty: "Sign in with <Vendor>".
    button_label: str = ""
    #: Asked on the connect screen before leaving for the vendor (a Shopify
    #: store address, a Salesforce sandbox switch). Checked server-side and
    #: kept with the sign-in's state.
    sign_in_inputs: tuple[SignInInput, ...] = ()

    # ── Sign-in inputs that change the request (Microsoft Ads sandbox) ─────
    def checked_inputs(self, raw: dict[str, str] | None) -> dict[str, str]:
        """The answers to :attr:`sign_in_inputs`, cleaned: a toggle is
        ``"true"`` or ``""``, text is trimmed and bounded. Unknown keys are
        dropped."""
        raw = raw or {}
        out: dict[str, str] = {}
        for item in self.sign_in_inputs:
            value = str(raw.get(item.key, "") or "").strip()
            if item.kind == "toggle":
                out[item.key] = "true" if toggle_on(value) else ""
            else:
                out[item.key] = value[:200]
        return out

    def scopes_for_inputs(self, checked: dict[str, str] | None) -> tuple[str, ...]:
        """The connector's scopes for these answers: a ticked toggle with
        ``scopes_when_on`` asks for those instead of :attr:`scopes`."""
        for item in self.sign_in_inputs:
            if item.kind == "toggle" and item.scopes_when_on and toggle_on((checked or {}).get(item.key, "")):
                return item.scopes_when_on
        return self.scopes

    def values_for_inputs(self, checked: dict[str, str] | None) -> dict[str, str]:
        """Connector field values these answers set on the connection. A
        toggle with ``value_when_on`` writes it when ticked and clears the
        field when not (``""``), so signing in again without the tick
        undoes an earlier one."""
        out: dict[str, str] = {}
        for item in self.sign_in_inputs:
            if item.kind == "toggle" and item.field and item.value_when_on:
                on = toggle_on((checked or {}).get(item.key, ""))
                out[item.field] = item.value_when_on if on else ""
        return out


def toggle_on(value: object) -> bool:
    """A ticked sign-in toggle, however the browser sent it."""
    return str(value or "").strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True, slots=True)
class SignInInput:
    """One thing typed or ticked before a sign-in starts."""

    key: str
    label: str
    #: ``text`` or ``toggle``.
    kind: str = "text"
    placeholder: str = ""
    help_text: str = ""
    #: The connector field the checked value ends up in (shown back as the
    #: starting value when signing in again).
    field: str = ""
    #: For a toggle: what :attr:`field` is set to when ticked (cleared when
    #: not). Empty: the vendor's own sign-in code decides (Salesforce).
    value_when_on: str = ""
    #: For a toggle: the connector scopes requested instead of the binding's
    #: own when ticked (a vendor's sandbox resource). Empty: unchanged.
    scopes_when_on: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PlatformKey:
    """A key the platform supplies, so a workspace can connect in one click.

    When the setting is filled in, the connect screen offers "Use the
    included access" and the connection stores no key of its own: it reads
    the platform's key at call time. The workspace's own key stays available
    under Advanced.
    """

    #: The settings attribute holding the key (``platform_pagespeed_api_key``).
    setting: str
    #: The connector field the key would otherwise be pasted into.
    field: str
    #: One plain sentence shown with the button.
    summary: str = ""

    @property
    def available(self) -> bool:
        return bool(str(getattr(settings, self.setting, "") or "").strip())

    @property
    def value(self) -> str:
        return str(getattr(settings, self.setting, "") or "").strip()


#: Non-secret marker: this connection uses the platform's own key.
KEY_SOURCE_KEY = "keySource"
PLATFORM_KEY_SOURCE = "platform"


def uses_platform_key(values: dict[str, str]) -> bool:
    return (values.get(KEY_SOURCE_KEY) or "") == PLATFORM_KEY_SOURCE


def resolve_key(credentials: Credentials, option: PlatformKey | None) -> str:
    """The key to call with: the workspace's own, else the platform's.

    A workspace's own key always wins, so pasting one under Advanced takes
    over from the included access without anything else changing.
    """
    if option is not None:
        own = credentials.get(option.field)
        if own:
            return own
        if uses_platform_key(credentials.values):
            if not option.available:
                from app.core.exceptions import ConnectorConfigError

                raise ConnectorConfigError(
                    "The included access for this integration is no longer offered on "
                    "this platform. Add your own key under Advanced."
                )
            return option.value
        return credentials.require(option.field)
    raise ValueError("resolve_key needs a PlatformKey")


@dataclass(frozen=True, slots=True)
class SetupGuide:
    """Plain-language steps for a connector that is connected with a key.

    Lives on the spec so the console has one source for it, and so a new
    vendor's guide is a data change, not a frontend change.
    """

    #: Numbered steps, in the vendor's own button wording.
    steps: tuple[str, ...]
    #: The exact page where the key is created.
    key_url: str = ""
    #: Button text for that link ("Open your OpenAI API keys page").
    key_url_label: str = ""
    #: Which field the key is pasted into.
    key_field: str = ""
    #: A regular expression (JavaScript-compatible) a well-formed key matches.
    key_pattern: str = ""
    #: What a well-formed key looks like, in words ("starts with sk-").
    key_hint: str = ""


@dataclass(frozen=True, slots=True)
class OAuthChoice:
    """One thing the signed-in account can reach — a property, a site, an
    ad account — and the connector field values that select it."""

    id: str
    label: str
    detail: str = ""
    values: dict[str, str] = field(default_factory=dict)


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
    #: Set when the connector can be connected through a platform sign-in.
    oauth: OAuthBinding | None = None
    #: Set for connectors connected with a key: the guided steps.
    guide: SetupGuide | None = None
    #: A developer integration (webhooks, self-hosted APIs, SMTP). The form
    #: stays, labelled Advanced.
    advanced: bool = False
    #: A platform-supplied key, for a one-click connection (phase 2B).
    platform_key: PlatformKey | None = None


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
    #: Tests only: the raw socket layer under the egress guard (see
    #: ``egress.pinned_client(inner_backend=...)``). DNS, the address policy
    #: and the pinning still run in front of it.
    _egress_inner_backend: httpcore.NetworkBackend | None = None
    #: The allow-list the pinned client was built with (see
    #: :func:`private_hosts_allow_list`); fetches reuse exactly this one.
    _egress_allow: egress.AllowList = egress.NO_ALLOWLIST

    def __init__(self, credentials: Credentials | None = None, *, org_id: str = "") -> None:
        if not getattr(self, "spec", None):
            raise TypeError(f"{type(self).__name__} must declare a class-level `spec`")
        self.credentials = credentials or Credentials()
        self.org_id = org_id
        self._client: httpx.Client | None = None
        #: The egress-pinned client this instance built, when guarded.
        self._pinned_client: httpx.Client | None = None

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

    @property
    def egress_guarded(self) -> bool:
        """Whether the address comes from the customer, so the egress guard applies."""
        return bool(self.spec.base_url_field)

    def _http(self) -> httpx.Client:
        if self._client is None:
            headers = {"User-Agent": "Synaprank/1.0", "Accept": "application/json"}
            if self.egress_guarded:
                # Pinned, no redirects, no proxy variables; TLS verified.
                # Redirects are followed in _guarded_send, checked per hop.
                # The crawler keeps its public-only, plain-http-allowed rules;
                # credentialed connectors get the admin's private hosts, and
                # https on every hop (see _require_https).
                self._egress_allow = (
                    egress.NO_ALLOWLIST
                    if self.slug in PLAIN_HTTP_CONNECTORS
                    else private_hosts_allow_list()
                )
                client = egress.pinned_client(
                    timeout=settings.connector_timeout_seconds,
                    allow=self._egress_allow,
                    headers=headers,
                    inner_backend=self._egress_inner_backend,
                )
                client.base_url = self.base_url
                if self.slug not in PLAIN_HTTP_CONNECTORS:
                    hooks = client.event_hooks
                    client.event_hooks = {
                        **hooks,
                        "request": [*hooks.get("request", []), _require_https],
                    }
                self._pinned_client = client
                self._client = client
            else:
                self._client = httpx.Client(
                    base_url=self.base_url,
                    timeout=settings.connector_timeout_seconds,
                    follow_redirects=True,
                    # Certificate verification is never disabled: credentials and
                    # customer content travel on these connections.
                    verify=True,
                    headers=headers,
                )
        return self._client

    @retry(
        retry=retry_if_exception_type((httpx.TransportError, httpx.HTTPStatusError)),
        stop=stop_after_attempt(settings.connector_max_attempts),
        wait=wait_exponential_jitter(initial=0.5, max=8),
        reraise=True,
    )
    def _send(self, request: httpx.Request) -> httpx.Response:
        client = self._http()
        if self.egress_guarded:
            response = self._guarded_send(client, request)
        else:
            response = client.send(request)
        # Only transient statuses are retried; a 4xx will not fix itself.
        if response.status_code in (429, 500, 502, 503, 504):
            response.raise_for_status()
        return response

    def _guarded_send(self, client: httpx.Client, request: httpx.Request) -> httpx.Response:
        """Send a customer-address request through the egress guard.

        Only the pinned client this instance built may reach the network. A
        test may put a client on ``httpx.MockTransport`` in ``_client``: it
        answers from a function and cannot open a socket. Any other client
        is refused, so nothing can swap the guard out for a plain client.
        """
        pinned = getattr(self, "_pinned_client", None)
        if pinned is not None and client is pinned:
            headers = [
                (name, value)
                for name, value in request.headers.multi_items()
                if name.lower() not in _REBUILT_HEADERS
            ]
            return egress.fetch(
                client,
                request.method,
                str(request.url),
                # The same list the pinned client was built with.
                allow=self._egress_allow,
                require_https=self.slug not in PLAIN_HTTP_CONNECTORS,
                max_bytes=MAX_GUARDED_RESPONSE_BYTES,
                max_redirects=egress.MAX_REDIRECTS,
                # Credentialed connectors stay on their own origin: custom key
                # headers (X-Api-Key...) and a 307/308 body would follow a hop.
                cross_origin_redirects=self.slug in PLAIN_HTTP_CONNECTORS,
                headers=headers,
                content=request.content or None,
            )
        if _cassette_replay_active():
            # The replay answers every send itself; nothing reaches the network.
            return client.send(request)
        if isinstance(getattr(client, "_transport", None), httpx.MockTransport) and not getattr(
            client, "_mounts", None
        ):
            return client.send(request)
        raise RuntimeError(f"{self.slug}: a customer address must use the egress-pinned client")

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
        content: bytes | None = None,
        return_response: bool = False,
    ) -> Any:  # noqa: ANN401
        """Make one authenticated call, with retries and redacted logging.

        ``form`` sends form-encoded fields instead of JSON. Only one vendor
        needs it — Bitbucket's commit endpoint takes the file contents as form
        fields rather than a JSON body — but reaching around this method to
        use the client directly would skip the retries, the error translation
        and the redacted logging, which is worse than one extra parameter.

        ``content`` sends these exact bytes as the body. A connector that signs
        its payload (HMAC over the body) must sign what goes on the wire, and
        ``json_body`` is serialised by httpx in its own way.

        ``return_response`` hands back the :class:`httpx.Response` itself
        (after the same error handling), for vendors that put what the caller
        needs in a header — LinkedIn's created ids, Shopify's ``Link`` paging.
        """
        if not self.base_url:
            raise ConnectorError(f"{self.name} has no base URL configured")
        if sum(x is not None for x in (json_body, form, content)) > 1:
            raise ValueError("Pass only one of json_body, form or content")

        merged = {**self.auth_headers(), **(headers or {})}
        client = self._http()
        request = client.build_request(
            method.upper(),
            path,
            params=params,
            json=json_body,
            data=form,
            content=content,
            headers=merged,
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
                self._redact(exc.response.text or "")[:300],
            )
            from app.core.user_messages import message_for_http_status

            error = ConnectorError(message_for_http_status(status, service=self.name))
            error.http_status = status  # type: ignore[attr-defined]
            # Redacted vendor text, for diagnose() to tell refusals apart. Never shown.
            error.vendor_body = self._redact(exc.response.text or "")[:2000]  # type: ignore[attr-defined]
            raise error from exc
        except httpx.TransportError as exc:
            log.warning("%s transport error on %s %s: %s", self.slug, method.upper(), path, exc)
            from app.core.user_messages import message_for_unreachable

            raise ConnectorError(message_for_unreachable(self.name)) from exc
        except egress.EgressBlockedError as exc:
            # The guard already logged a hash of the host; never the address.
            log.warning(
                "%s %s refused by the egress guard: reason=%s", self.slug, method.upper(), exc.reason
            )
            # A private address, a plain-http address or an unknown host is
            # the connection's settings, so retrying cannot help; one
            # oversized or endlessly redirecting reply is just that item.
            error_class = (
                ConnectorError if exc.reason in _PER_REQUEST_EGRESS_REASONS else ConnectorConfigError
            )
            error = error_class(exc.message, details={"reason": exc.reason})
            raise error from None

        if response.status_code >= 400:
            # Vendor bodies may contain keys or internals — log only.
            log.warning(
                "%s rejected %s %s with HTTP %s: %s",
                self.slug,
                method.upper(),
                path,
                response.status_code,
                self._redact(response.text or "")[:300],
            )
            from app.core.user_messages import message_for_http_status

            error = ConnectorError(
                message_for_http_status(response.status_code, service=self.name)
            )
            # The status, for callers that treat "not found" differently from
            # "not allowed" (the AI connectors' model checks). Never shown.
            error.http_status = response.status_code  # type: ignore[attr-defined]
            # Redacted vendor text, for diagnose() to tell refusals apart. Never shown.
            error.vendor_body = self._redact(response.text or "")[:2000]  # type: ignore[attr-defined]
            raise error

        log.debug("%s %s %s -> %s", self.slug, method.upper(), path, response.status_code)
        if return_response:
            return response
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

    def _redact(self, text: str) -> str:
        """``text`` with this connection's own credential values blanked out.

        Some vendors echo the key they were sent in an error body; that body is
        logged, and a key must never reach the logs.
        """
        for value in self.credentials.values.values():
            if isinstance(value, str) and len(value) >= 8 and value in text:
                text = text.replace(value, "[redacted]")
        return text

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None
        self._pinned_client = None

    # ── Lifecycle hooks ────────────────────────────────────────────────────
    def account_environment(self) -> str:
        """"live", "test_account" or "sandbox" — what this connection points at.

        Read from the optional ``accountEnvironment`` credential value (see
        ``base/verification.py``). Connectors whose vendor uses different
        addresses for a sandbox switch their base URL on it.
        """
        value = (self.credentials.get("accountEnvironment") or "").strip().lower()
        return value if value in ("test_account", "sandbox") else "live"

    def verify_steps(self) -> list:  # list[verification.VerifyStep]
        """The live checks for this connector (see ``base/verification.py``).

        Empty means "no live check written yet" — the connector then stays
        labelled Beta until one exists and passes.
        """
        return []

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

    def list_oauth_choices(self) -> list[OAuthChoice]:
        """What the signed-in account can reach, for the "choose" step.

        Only connectors with an :class:`OAuthBinding` implement this; it is
        called with the tokens from the sign-in and none of the connector's
        own selection fields yet.
        """
        raise NotImplementedError

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
