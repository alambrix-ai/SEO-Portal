"""Shared HTTP plumbing for the model providers.

Every provider here speaks JSON over HTTPS, so the transport, the timeout, the
retry policy and the error translation live once. What differs per vendor is
the request body and where the text sits in the reply, and that is all a
subclass supplies.

Two rules this module exists to enforce:

**No fallbacks.** If the configured model is unavailable, the key is wrong or
the response is not what the vendor documents, the call raises. It does not
retry on a different model, downgrade to another provider, or return an empty
string that an agent might mistake for content. An agent that cannot reach its
model reports that it is waiting on one; an agent that silently used something
else would publish copy nobody chose to the customer's live site.

**No invented numbers.** Token counts come from the vendor's own usage block.
Cost comes from the configured price, and when no price is configured the cost
is zero *and says so at startup* — rather than a plausible-looking figure from
a built-in table that goes stale the week a vendor changes its pricing.
"""
from __future__ import annotations

import re
from abc import abstractmethod
from collections.abc import Iterable
from typing import Any

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential_jitter

from app.core.config import settings
from app.core.logging import get_logger
from app.llm.base import LLMError, LLMProvider, LLMResponse, LLMUsage

log = get_logger(__name__)

#: Longest slice of a vendor error body that may reach a log line.
VENDOR_LOG_CHARS = 200
#: How much of a body is scanned for secrets before it is cut down.
_VENDOR_SCAN_CHARS = 4000
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_BEARER = re.compile(r"(?i)\bbearer\s+\S+")
#: Vendor key shapes, plus any long opaque token (keys, session ids).
_KEY_LIKE = re.compile(
    r"\b(?:sk|pk|rk|xai|pplx|AIza|ya29)[-_.A-Za-z0-9]{8,}|[A-Za-z0-9_\-]{32,}"
)


def redact_vendor_text(text: str, *, secrets: Iterable[str] = ()) -> str:
    """A vendor error body made safe for a log line.

    The given ``secrets`` (the connection's own key), email addresses, bearer
    tokens and key-shaped strings are blanked out, and the result is cut to
    :data:`VENDOR_LOG_CHARS`. Redaction runs before the cut, so a key that
    straddles the cut cannot leave its prefix behind. Never shown to users.
    """
    body = (text or "")[:_VENDOR_SCAN_CHARS]
    for secret in secrets:
        if secret and len(secret) >= 4:
            body = body.replace(secret, "[redacted]")
    body = _BEARER.sub("[redacted]", body)
    body = _EMAIL.sub("[redacted-email]", body)
    body = _KEY_LIKE.sub("[redacted]", body)
    body = " ".join(body.split())
    if len(body) > VENDOR_LOG_CHARS:
        body = body[:VENDOR_LOG_CHARS] + "..."
    return body


def price(input_tokens: int, output_tokens: int) -> float:
    """Cost of one call, from the configured per-million-token rates.

    Zero when the rates are unset, which the startup check reports. A built-in
    price table would be wrong within a quarter and wrong silently — the
    figure lands in ``agent_runs.cost`` and gets reported as spend.
    """
    return round(
        (input_tokens / 1_000_000) * settings.llm_price_input_per_mtok
        + (output_tokens / 1_000_000) * settings.llm_price_output_per_mtok,
        6,
    )


class HttpLLMProvider(LLMProvider):
    """A model provider reached over HTTP."""

    #: Vendor API root, from settings so a gateway or proxy can be pointed at.
    base_url: str = ""
    #: Which setting holds this provider's key, for error messages that name it.
    key_setting: str = ""
    #: True where the vendor API itself requires a token cap on every request
    #: (Anthropic's ``max_tokens``). Informational only: every provider now
    #: sends a cap (see :meth:`resolve_max_tokens`), so no call is unbounded.
    requires_max_tokens: bool = False

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str,
        default_max_tokens: int | None = None,
        temperature: float | None = None,
        top_p: float | None = None,
        effort: str | None = None,
        thinking_level: str | None = None,
        vendor_label: str = "",
    ) -> None:
        if not api_key:
            raise LLMError(
                f"{self.name} is missing its API key. Open Connectors, "
                "reconnect this model, and try again."
            )
        if not model:
            raise LLMError(
                f"{self.name} has no model name set. Open Connectors and "
                "enter the exact model name — there is no default."
            )
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.default_max_tokens = default_max_tokens
        self.temperature = temperature
        self.top_p = top_p
        self.effort = effort
        self.thinking_level = thinking_level
        #: The connection's name in messages ("Google Gemini"), else ``name``.
        self.vendor_label = vendor_label or self.name
        self._client: httpx.Client | None = None

    # ── Vendor specifics ───────────────────────────────────────────────────
    @abstractmethod
    def _endpoint(self) -> str:
        ...

    @abstractmethod
    def _payload(
        self, prompt: str, *, system: str, max_tokens: int | None
    ) -> dict[str, Any]:
        """The request body. ``max_tokens`` None means "leave it out"."""

    @abstractmethod
    def _parse(self, data: dict[str, Any]) -> tuple[str, LLMUsage, str]:
        """Return ``(text, usage, model_reported)``."""

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}

    def _params(self) -> dict[str, str]:
        return {}

    # ── Transport ──────────────────────────────────────────────────────────
    def _http(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(
                base_url=self.base_url,
                timeout=settings.llm_timeout_seconds,
                # Never disabled: prompts carry the customer's page content.
                verify=True,
                headers={"User-Agent": "Synaprank/1.0"},
            )
        return self._client

    @retry(
        retry=retry_if_exception_type((httpx.TransportError, httpx.HTTPStatusError)),
        stop=stop_after_attempt(settings.llm_max_attempts),
        wait=wait_exponential_jitter(initial=1.0, max=20),
        reraise=True,
    )
    def _send(self, request: httpx.Request) -> httpx.Response:
        response = self._http().send(request)
        # Only the transient statuses. A 400 or a 401 will not fix itself, and
        # retrying it just delays the error the operator needs to see.
        if response.status_code in (429, 500, 502, 503, 504):
            response.raise_for_status()
        return response

    def complete(
        self,
        prompt: str,
        *,
        system: str = "",
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> LLMResponse:
        # Sampling settings come only from the connection, and only when the
        # person set them; a per-call value is ignored so no code path can
        # send a temperature nobody chose (newer models reject one).
        _ = temperature
        return self._complete_payload(
            self._payload(prompt, system=system, max_tokens=self.resolve_max_tokens(max_tokens))
        )

    def resolve_max_tokens(self, requested: int | None) -> int:
        """The caller's cap, else the connection's, else the platform's
        ``LLM_MAX_TOKENS``. Always a positive number: no call goes out without
        an output cap, whatever the vendor's own default would be."""
        for candidate in (requested, self.default_max_tokens, settings.llm_max_tokens):
            if candidate and candidate > 0:
                return int(candidate)
        raise LLMError(
            "No output limit is configured for the AI model. Ask an "
            "administrator to set LLM_MAX_TOKENS."
        )

    def _log_vendor_error(self, status: int, body: str) -> None:
        """Log a vendor's error body, bounded and redacted. Never raised."""
        log.warning(
            "%s rejected request with HTTP %s: %s",
            self.name,
            status,
            redact_vendor_text(body, secrets=(self.api_key,)),
        )

    def _complete_payload(self, payload: dict[str, Any]) -> LLMResponse:
        client = self._http()
        request = client.build_request(
            "POST",
            self._endpoint(),
            json=payload,
            headers=self._headers(),
            params=self._params(),
        )
        try:
            response = self._send(request)
        except httpx.HTTPStatusError as exc:
            log.warning(
                "%s HTTP %s after retries: %s",
                self.name,
                exc.response.status_code,
                redact_vendor_text(exc.response.text or "", secrets=(self.api_key,)),
            )
            from app.core.user_messages import message_for_http_status

            raise LLMError(
                message_for_http_status(exc.response.status_code, service=self.name)
            ) from exc
        except httpx.TransportError as exc:
            log.warning("%s transport error: %s", self.name, type(exc).__name__)
            from app.core.user_messages import message_for_unreachable

            raise LLMError(message_for_unreachable(self.name)) from exc

        if response.status_code == 401 or response.status_code == 403:
            from app.core.user_messages import message_for_http_status

            raise LLMError(message_for_http_status(response.status_code, service=self.name))
        if response.status_code == 404:
            from app.llm.model_catalog import stale_model_message

            raise LLMError(stale_model_message(self.model, self.vendor_label))
        if response.status_code >= 400:
            # The vendor's own wording goes to the log only, bounded and
            # redacted: it can echo the key, the prompt or an email, and it is
            # written for developers, not for the people using the console.
            self._log_vendor_error(response.status_code, response.text or "")
            from app.core.user_messages import message_for_http_status

            raise LLMError(message_for_http_status(response.status_code, service=self.name))

        try:
            data = response.json()
        except ValueError as exc:
            raise LLMError(f"{self.name} returned a non-JSON response") from exc

        text, usage, reported = self._parse(data)
        if not text.strip():
            # An empty completion is a failure, not an answer. Returning it
            # would let an agent treat "" as generated content.
            raise LLMError(
                f"{self.name} returned no text for {self.model!r} — "
                "the request may have been filtered or truncated."
            )
        log.debug(
            "%s %s: in=%d out=%d cost=%.6f",
            self.name,
            reported or self.model,
            usage.input_tokens,
            usage.output_tokens,
            usage.cost,
        )
        return LLMResponse(text=text, usage=usage, model=reported or self.model)

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None
