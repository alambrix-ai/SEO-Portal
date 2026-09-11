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

from abc import abstractmethod
from typing import Any

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential_jitter

from app.core.config import settings
from app.core.logging import get_logger
from app.llm.base import LLMError, LLMProvider, LLMResponse, LLMUsage

log = get_logger(__name__)


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
        self._client: httpx.Client | None = None

    # ── Vendor specifics ───────────────────────────────────────────────────
    @abstractmethod
    def _endpoint(self) -> str:
        ...

    @abstractmethod
    def _payload(self, prompt: str, *, system: str, max_tokens: int) -> dict[str, Any]:
        ...

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
                headers={"User-Agent": "AutoMarket-AI/1.0"},
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
        temperature: float = 0.2,
    ) -> LLMResponse:
        # Per-call temperature is ignored when the connector already set one;
        # otherwise vendors that support sampling use the connector (or this)
        # value inside _payload. Callers that still pass temperature= are fine.
        _ = temperature

        resolved_max = (
            max_tokens
            or self.default_max_tokens
            or settings.llm_max_tokens
        )
        client = self._http()
        request = client.build_request(
            "POST",
            self._endpoint(),
            json=self._payload(prompt, system=system, max_tokens=resolved_max),
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
                (exc.response.text or "")[:300],
            )
            from app.core.user_messages import message_for_http_status

            raise LLMError(
                message_for_http_status(exc.response.status_code, service=self.name)
            ) from exc
        except httpx.TransportError as exc:
            log.warning("%s transport error: %s", self.name, exc)
            from app.core.user_messages import message_for_unreachable

            raise LLMError(message_for_unreachable(self.name)) from exc

        if response.status_code == 401 or response.status_code == 403:
            from app.core.user_messages import message_for_http_status

            raise LLMError(message_for_http_status(response.status_code, service=self.name))
        if response.status_code == 404:
            raise LLMError(
                f"{self.name}: that model name was not found. "
                "Check the model setting and try again."
            )
        if response.status_code >= 400:
            log.warning(
                "%s rejected request with HTTP %s: %s",
                self.name,
                response.status_code,
                response.text[:300],
            )
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
