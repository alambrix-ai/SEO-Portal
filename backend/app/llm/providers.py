"""The model providers, one class each.

Anthropic, OpenAI, Gemini and Grok. Which one runs, and which model, comes
from ``LLM_PROVIDER`` and ``LLM_MODEL`` — nothing in the codebase names a
vendor except this file.

OpenAI and Grok share a class because Grok's API is deliberately
OpenAI-compatible; that is shared implementation, not a fallback. Anthropic
and Gemini each have their own request and response shape.

:class:`SelfHostedOpenAIProvider` talks to a customer's own OpenAI-compatible
server (vLLM, Ollama). Its address is customer-supplied, so every connection
goes through the egress guard (:mod:`app.core.egress`).
"""
from __future__ import annotations

import hashlib
import threading
import time
from dataclasses import dataclass
from typing import Any

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential_jitter

from app.core import egress
from app.core.config import settings
from app.core.logging import get_logger
from app.llm.base import LLMError, LLMResponse, LLMUsage
from app.llm.http_provider import HttpLLMProvider, price, redact_vendor_text

log = get_logger(__name__)


#: Anthropic model capabilities, read from ``GET /v1/models/{id}`` and kept
#: for a few minutes: ``(base_url, key hash, model) -> (when, capabilities)``.
_CAPABILITY_SECONDS = 600
_capability_lock = threading.Lock()
_capability_cache: dict[tuple[str, str, str], tuple[float, dict[str, Any]]] = {}


def _supported(block: Any) -> bool:  # noqa: ANN401
    return isinstance(block, dict) and block.get("supported") is True


class AnthropicProvider(HttpLLMProvider):
    """Claude, over the Messages API.

    Whether to ask for adaptive thinking, and which ``effort`` levels may be
    sent, come from the model's own ``capabilities`` in Anthropic's Models API
    (``thinking.types.adaptive.supported``, ``effort.<level>.supported``;
    https://platform.claude.com/docs/en/api/models/list) — never from the
    shape of the model's name. If the capabilities cannot be read, neither is
    sent and Anthropic's defaults apply. ``max_tokens`` is required by this
    API, so it is always sent. Refusal fallbacks are
    **not** used: they re-run a declined request on a different model
    server-side, which means the copy on a customer's page could come from a
    model nobody configured, and the reply would not say so.
    """

    name = "Anthropic"
    key_setting = "ANTHROPIC_API_KEY"
    requires_max_tokens = True
    #: Set by set_capabilities() when the caller already has them.
    _capabilities: dict[str, Any] | None = None

    def _headers(self) -> dict[str, str]:
        return {
            "x-api-key": self.api_key,
            "anthropic-version": settings.anthropic_api_version,
            "Content-Type": "application/json",
        }

    def _endpoint(self) -> str:
        return "/v1/messages"

    def model_capabilities(self) -> dict[str, Any]:
        """This model's ``capabilities`` from the Models API, or {} if unreadable."""
        if self._capabilities is not None:
            return self._capabilities
        key = (
            self.base_url,
            hashlib.sha256(self.api_key.encode()).hexdigest(),
            self.model,
        )
        now = time.monotonic()
        with _capability_lock:
            hit = _capability_cache.get(key)
            if hit is not None and now - hit[0] < _CAPABILITY_SECONDS:
                return hit[1]
        try:
            response = self._http().get(f"/v1/models/{self.model}", headers=self._headers())
            if response.status_code >= 400:
                log.info("Anthropic model lookup HTTP %s", response.status_code)
                return {}
            capabilities = dict((response.json() or {}).get("capabilities") or {})
        except Exception as exc:  # noqa: BLE001 - a lookup must never stop the call
            log.info("Anthropic model lookup failed: %s", type(exc).__name__)
            return {}
        with _capability_lock:
            _capability_cache[key] = (time.monotonic(), capabilities)
        return capabilities

    def set_capabilities(self, capabilities: dict[str, Any] | None) -> None:
        """Use these capabilities instead of looking them up."""
        self._capabilities = capabilities

    def _payload(self, prompt: str, *, system: str, max_tokens: int | None) -> dict[str, Any]:
        return self._payload_messages(
            [{"role": "user", "content": prompt}],
            system=system,
            max_tokens=max_tokens,
        )

    def _payload_messages(
        self,
        messages: list[dict[str, str]],
        *,
        system: str,
        max_tokens: int | None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": self.model,
            # Required by the Messages API.
            "max_tokens": max_tokens or settings.llm_max_tokens,
            "messages": messages,
        }
        capabilities = self.model_capabilities()
        thinking = capabilities.get("thinking") or {}
        if _supported((thinking.get("types") or {}).get("adaptive")):
            body["thinking"] = {"type": "adaptive"}
        effort = (self.effort or "").strip().lower()
        efforts = capabilities.get("effort") or {}
        if effort and _supported(efforts) and _supported(efforts.get(effort)):
            body["output_config"] = {"effort": effort}
        if system:
            body["system"] = system
        return body

    def complete_messages(
        self,
        messages: list[dict[str, str]],
        *,
        system: str = "",
        max_tokens: int | None = None,
    ) -> LLMResponse:
        """Multi-turn Messages API call (used by the LlamaIndex chat engine)."""
        resolved_max = self.resolve_max_tokens(max_tokens)
        return self._complete_payload(
            self._payload_messages(messages, system=system, max_tokens=resolved_max)
        )

    def stream_messages(
        self,
        messages: list[dict[str, str]],
        *,
        system: str = "",
        max_tokens: int | None = None,
    ):
        """Yield plain-text deltas from Anthropic's streaming Messages API."""
        import json

        resolved_max = self.resolve_max_tokens(max_tokens)
        payload = self._payload_messages(
            messages, system=system, max_tokens=resolved_max
        )
        payload["stream"] = True

        client = self._http()
        try:
            with client.stream(
                "POST",
                self._endpoint(),
                json=payload,
                headers=self._headers(),
                params=self._params(),
            ) as response:
                if response.status_code >= 400:
                    # Vendor wording is logged bounded and redacted, never
                    # raised: it can echo the key, the prompt or an email.
                    detail = ""
                    try:
                        detail = (response.read() or b"").decode("utf-8", errors="replace")
                    except (httpx.HTTPError, httpx.StreamError) as exc:
                        log.info(
                            "%s stream error body unreadable: %s",
                            self.name,
                            type(exc).__name__,
                        )
                    self._log_vendor_error(response.status_code, detail)
                    from app.core.user_messages import message_for_http_status

                    raise LLMError(
                        message_for_http_status(response.status_code, service=self.name)
                    )

                for line in response.iter_lines():
                    if not line:
                        continue
                    if line.startswith("event:"):
                        continue
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if not data or data == "[DONE]":
                        continue
                    try:
                        event = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    etype = event.get("type")
                    if etype == "content_block_delta":
                        delta = event.get("delta") or {}
                        if delta.get("type") == "text_delta":
                            text = delta.get("text") or ""
                            if text:
                                yield text
                    elif etype == "error":
                        err = event.get("error") or {}
                        log.warning(
                            "%s stream error event: %s",
                            self.name,
                            redact_vendor_text(
                                str(err.get("message") or "") if isinstance(err, dict) else "",
                                secrets=(self.api_key,),
                            ),
                        )
                        raise LLMError(
                            f"{self.name} stopped while writing the reply. Try again in a moment."
                        )
        except LLMError:
            raise
        except httpx.TransportError as exc:
            from app.core.user_messages import message_for_unreachable

            raise LLMError(message_for_unreachable(self.name)) from exc

    def _parse(self, data: dict[str, Any]) -> tuple[str, LLMUsage, str]:
        if data.get("stop_reason") == "refusal":
            details = data.get("stop_details") or {}
            raise LLMError(
                f"Anthropic declined the request (category: "
                f"{details.get('category') or 'unspecified'}). Rephrase the "
                "agent prompt or narrow its scope."
            )
        text = "".join(
            block.get("text", "")
            for block in data.get("content") or []
            if block.get("type") == "text"
        )
        usage = data.get("usage") or {}
        tokens_in = int(usage.get("input_tokens") or 0)
        tokens_out = int(usage.get("output_tokens") or 0)
        return (
            text,
            LLMUsage(tokens_in, tokens_out, price(tokens_in, tokens_out)),
            str(data.get("model") or ""),
        )


class OpenAICompatibleProvider(HttpLLMProvider):
    """The chat-completions shape, used by OpenAI and by Grok."""

    def _endpoint(self) -> str:
        return "/v1/chat/completions"

    def _payload(self, prompt: str, *, system: str, max_tokens: int | None) -> dict[str, Any]:
        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        body: dict[str, Any] = {"model": self.model, "messages": messages}
        # OpenAI's Chat Completions reference marks `max_tokens` deprecated in
        # favour of `max_completion_tokens`, which every current model takes
        # and reasoning models require:
        # https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create
        # Always present: resolve_max_tokens falls back to LLM_MAX_TOKENS, so
        # no call is left to the model's own (possibly very large) limit.
        if max_tokens:
            body["max_completion_tokens"] = max_tokens
        # Sampling settings only when the person set them on the connection.
        # Some models accept only their default temperature/top_p; OpenAI
        # publishes no per-model flag for this, so nothing is guessed from the
        # model name — a value a model rejects comes back as OpenAI's own
        # message (shown as is), and clearing the setting fixes it.
        if self.temperature is not None:
            body["temperature"] = self.temperature
        if self.top_p is not None:
            body["top_p"] = self.top_p
        return body

    def _parse(self, data: dict[str, Any]) -> tuple[str, LLMUsage, str]:
        choices = data.get("choices") or []
        if not choices:
            raise LLMError(f"{self.name} returned no choices")
        first = choices[0]
        if first.get("finish_reason") == "content_filter":
            raise LLMError(
                f"{self.name} filtered the request. Rephrase the agent prompt "
                "or narrow its scope."
            )
        text = ((first.get("message") or {}).get("content")) or ""
        usage = data.get("usage") or {}
        tokens_in = int(usage.get("prompt_tokens") or 0)
        tokens_out = int(usage.get("completion_tokens") or 0)
        return (
            str(text),
            LLMUsage(tokens_in, tokens_out, price(tokens_in, tokens_out)),
            str(data.get("model") or ""),
        )


class OpenAIProvider(OpenAICompatibleProvider):
    name = "OpenAI"
    key_setting = "OPENAI_API_KEY"


class GrokProvider(OpenAICompatibleProvider):
    name = "Grok"
    key_setting = "GROK_API_KEY"


class GeminiProvider(HttpLLMProvider):
    """Gemini, over the generateContent API.

    The key goes in Google's own ``x-goog-api-key`` header (never in the URL,
    where proxies, access logs and error messages would record it), the
    system prompt is its own field, and the reply is a list of parts — the
    differences that are why this is not the OpenAI-compatible class.
    """

    name = "Gemini"
    key_setting = "GEMINI_API_KEY"

    def _headers(self) -> dict[str, str]:
        return {"x-goog-api-key": self.api_key, "Content-Type": "application/json"}

    def _params(self) -> dict[str, str]:
        # Deliberately empty: a ``key=`` query parameter would put the secret
        # in the request URL.
        return {}

    def _endpoint(self) -> str:
        return f"/v1beta/models/{self.model}:generateContent"

    def _payload(self, prompt: str, *, system: str, max_tokens: int | None) -> dict[str, Any]:
        # Every generationConfig field is optional
        # (https://ai.google.dev/api/generate-content#generationconfig); each
        # is sent only when set, so Google's defaults apply otherwise. The
        # output cap is always set (resolve_max_tokens never returns blank).
        generation: dict[str, Any] = {}
        if max_tokens:
            generation["maxOutputTokens"] = max_tokens
        if self.temperature is not None:
            generation["temperature"] = self.temperature
        level = (self.thinking_level or "").strip().lower()
        if level in {"low", "medium", "high"}:
            generation["thinkingConfig"] = {"thinkingLevel": level}
        body: dict[str, Any] = {"contents": [{"role": "user", "parts": [{"text": prompt}]}]}
        if generation:
            body["generationConfig"] = generation
        if system:
            body["systemInstruction"] = {"parts": [{"text": system}]}
        return body

    def _parse(self, data: dict[str, Any]) -> tuple[str, LLMUsage, str]:
        candidates = data.get("candidates") or []
        if not candidates:
            blocked = (data.get("promptFeedback") or {}).get("blockReason")
            raise LLMError(
                "Gemini returned no candidates"
                + (f" (blocked: {blocked})" if blocked else "")
            )
        first = candidates[0]
        if first.get("finishReason") == "SAFETY":
            raise LLMError(
                "Gemini blocked the response on safety grounds. Rephrase the "
                "agent prompt or narrow its scope."
            )
        text = "".join(
            part.get("text", "") for part in ((first.get("content") or {}).get("parts") or [])
        )
        usage = data.get("usageMetadata") or {}
        tokens_in = int(usage.get("promptTokenCount") or 0)
        tokens_out = int(usage.get("candidatesTokenCount") or 0)
        return (
            text,
            LLMUsage(tokens_in, tokens_out, price(tokens_in, tokens_out)),
            str(data.get("modelVersion") or ""),
        )


# ── Self-hosted (vLLM, Ollama) ──────────────────────────────────────────────
#: Largest reply read from a self-hosted server, models list or completion.
SELF_HOSTED_MAX_BYTES = egress.DEFAULT_MAX_BYTES
#: Statuses retried as transient (the same set as every other provider).
_TRANSIENT = (429, 500, 502, 503, 504)
_DROP_ON_COPY = ("content-encoding", "content-length", "transfer-encoding")
#: Shown when a self-hosted server answers with a redirect. Redirects are
#: never followed: the address the administrator approved is the only one used.
SELF_HOSTED_REDIRECT_MESSAGE = (
    "Your model server tried to send the request to a different address, "
    "which is not allowed. Enter the server's final address and try again."
)


@dataclass(slots=True)
class UnreportedUsage(LLMUsage):
    """The server sent no token counts (Ollama and vLLM may leave ``usage``
    out). The zeros are placeholders, not measurements: record the tokens as
    unknown, never as 0. Check with :func:`usage_reported`; adding two usages
    gives a plain :class:`LLMUsage` and drops this flag."""

    reported: bool = False


def usage_reported(usage: LLMUsage) -> bool:
    """False when the token counts in ``usage`` were not reported by the server."""
    return bool(getattr(usage, "reported", True))


def self_hosted_root(raw: str) -> str:
    """The server root from what was typed: trimmed, without a trailing
    ``/`` or ``/v1`` (requests add ``/v1/...`` themselves)."""
    root = (raw or "").strip().rstrip("/")
    if root.lower().endswith("/v1"):
        root = root[: -len("/v1")].rstrip("/")
    return root


def check_self_hosted_base(raw: str, *, allow: egress.AllowList) -> str:
    """:func:`self_hosted_root`, validated by the egress guard.

    https only, unless ``SELF_HOSTED_LLM_ALLOW_PLAIN_HTTP`` is on AND every
    address the host resolves to is private and inside a CIDR entry of
    ``SELF_HOSTED_LLM_ALLOWED_HOSTS`` (a host-name entry never permits http).
    The port must be in ``SELF_HOSTED_LLM_ALLOWED_PORTS``. Private ranges
    need an allow-list entry; metadata addresses are always refused. No
    query string, fragment or user name in the address. Raises
    :class:`~app.core.egress.EgressBlockedError` (a friendly 422).
    """
    root = self_hosted_root(raw)
    if not root or "?" in root or "#" in root:
        raise egress.EgressBlockedError("invalid", egress.MSG_INVALID)
    egress.validate_base_url(
        root,
        allow=allow,
        allow_plain_http=egress.self_hosted_llm_allow_plain_http(),
        allowed_ports=egress.self_hosted_llm_allowed_ports(),
    )
    return root


def is_plain_http(url: str | httpx.URL) -> bool:
    """Whether ``url`` is not https (so no credential may travel on it)."""
    return httpx.URL(str(url)).scheme.lower() != "https"


def self_hosted_client(base_url: str, *, allow: egress.AllowList) -> httpx.Client:
    """A pinned client (no redirects, no proxy variables) rooted at ``base_url``.

    A plain-http root gets a client that connects only to addresses inside
    an allow-listed private CIDR, re-checked at every connection.
    """
    client = egress.pinned_client(
        timeout=settings.self_hosted_llm_timeout_seconds,
        allow=allow,
        headers={"User-Agent": "Synaprank/1.0", "Accept": "application/json"},
        private_cidr_only=is_plain_http(base_url),
    )
    client.base_url = base_url
    return client


def send_capped(
    client: httpx.Client, request: httpx.Request, *, max_bytes: int = SELF_HOSTED_MAX_BYTES
) -> httpx.Response:
    """Send ``request`` and read at most ``max_bytes`` of the reply.

    Raises :class:`~app.core.egress.EgressBlockedError` (``too_large``) past
    the cap. The returned response is fully read and closed. An
    ``Authorization`` header is never sent over plain http: it is removed
    here, whoever built the request.
    """
    if is_plain_http(request.url) and "authorization" in request.headers:
        del request.headers["authorization"]
        log.info("Self-hosted model key not sent: the server address is plain http")
    response = client.send(request, stream=True)
    try:
        declared = response.headers.get("content-length", "")
        if declared.isdigit() and int(declared) > max_bytes:
            raise egress.EgressBlockedError("too_large", egress.MSG_TOO_LARGE)
        body = bytearray()
        for chunk in response.iter_bytes():
            body += chunk
            if len(body) > max_bytes:
                raise egress.EgressBlockedError("too_large", egress.MSG_TOO_LARGE)
    finally:
        response.close()
    headers = [
        (k, v) for k, v in response.headers.multi_items() if k.lower() not in _DROP_ON_COPY
    ]
    return httpx.Response(
        response.status_code, headers=headers, content=bytes(body), request=request
    )


def is_redirect(status: int) -> bool:
    return 300 <= status < 400


#: Stands in for "no key" while the parent constructor runs (it refuses a
#: blank key); replaced with ``""`` straight after, so it is never sent.
_NO_KEY = "no-key"


class SelfHostedOpenAIProvider(OpenAICompatibleProvider):
    """A customer's own OpenAI-compatible server: vLLM or Ollama.

    * The base URL is validated with the egress guard when the provider is
      built, and every connection is pinned to a validated address
      (``SELF_HOSTED_LLM_ALLOWED_HOSTS`` opens private ranges; metadata
      addresses stay closed).
    * A redirect is an error, never followed.
    * The key is optional: no ``Authorization`` header is sent without one,
      and never over plain http.
    * ``max_tokens`` is sent (Ollama ignores ``max_completion_tokens``), and
      JSON calls ask for ``response_format: json_object``.
    * A reply without ``usage`` is accepted; its usage is
      :class:`UnreportedUsage`.
    * Replies are capped at :data:`SELF_HOSTED_MAX_BYTES`.
    """

    name = "Your own model"
    key_setting = "connector"

    def __init__(self, *, api_key: str = "", model: str, base_url: str, **options: Any) -> None:  # noqa: ANN401
        self._allow = egress.self_hosted_llm_allow_list()
        root = check_self_hosted_base(base_url, allow=self._allow)
        super().__init__(api_key=api_key or _NO_KEY, model=model, base_url=root, **options)
        self.api_key = api_key or ""

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key and not is_plain_http(self.base_url):
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _http(self) -> httpx.Client:
        if self._client is None:
            self._client = self_hosted_client(self.base_url, allow=self._allow)
        return self._client

    @retry(
        retry=retry_if_exception_type((httpx.TransportError, httpx.HTTPStatusError)),
        stop=stop_after_attempt(settings.llm_max_attempts),
        wait=wait_exponential_jitter(initial=1.0, max=20),
        reraise=True,
    )
    def _send(self, request: httpx.Request) -> httpx.Response:
        try:
            response = send_capped(self._http(), request)
        except egress.EgressBlockedError as exc:
            log.warning("%s refused by the egress guard: %s", self.name, exc.reason)
            raise LLMError(exc.message) from exc
        if is_redirect(response.status_code):
            log.warning("%s answered with a redirect (HTTP %s)", self.name, response.status_code)
            raise LLMError(SELF_HOSTED_REDIRECT_MESSAGE)
        if response.status_code in _TRANSIENT:
            response.raise_for_status()
        return response

    def _payload(self, prompt: str, *, system: str, max_tokens: int | None) -> dict[str, Any]:
        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        body: dict[str, Any] = {"model": self.model, "messages": messages}
        # max_tokens, not max_completion_tokens: Ollama ignores the latter.
        if max_tokens:
            body["max_tokens"] = max_tokens
        if self.temperature is not None:
            body["temperature"] = self.temperature
        return body

    def complete_json(
        self,
        prompt: str,
        *,
        system: str = "",
        schema_hint: str = "",
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> tuple[Any, LLMUsage]:  # noqa: ANN401
        """As :meth:`LLMProvider.complete_json`, asking the server for a JSON
        object. The reply is still parsed defensively, and the caller must
        validate it against its schema."""
        _ = temperature
        instruction = prompt
        if schema_hint:
            instruction = (
                f"{prompt}\n\nRespond with JSON only, matching this shape:\n{schema_hint}"
            )
        payload = self._payload(
            instruction, system=system, max_tokens=self.resolve_max_tokens(max_tokens)
        )
        payload["response_format"] = {"type": "json_object"}
        response = self._complete_payload(payload)
        return response.json(), response.usage

    def _parse(self, data: dict[str, Any]) -> tuple[str, LLMUsage, str]:
        choices = data.get("choices") if isinstance(data, dict) else None
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise LLMError(f"{self.name} returned no choices")
        message = choices[0].get("message")
        text = message.get("content") if isinstance(message, dict) else ""
        if not isinstance(text, str):
            text = ""
        usage = data.get("usage")
        tokens_in = _count(usage, "prompt_tokens")
        tokens_out = _count(usage, "completion_tokens")
        if tokens_in is None and tokens_out is None:
            counted: LLMUsage = UnreportedUsage()
        else:
            counted = LLMUsage(
                tokens_in or 0, tokens_out or 0, price(tokens_in or 0, tokens_out or 0)
            )
        return text, counted, str(data.get("model") or "")


def _count(usage: Any, key: str) -> int | None:  # noqa: ANN401
    """A non-negative token count from a ``usage`` block, or None when absent."""
    if not isinstance(usage, dict):
        return None
    value = usage.get(key)
    if isinstance(value, bool) or not isinstance(value, int | float) or value < 0:
        return None
    return int(value)


#: Everything needed to build a provider from configuration alone.
PROVIDERS: dict[str, tuple[type[HttpLLMProvider], str, str]] = {
    "anthropic": (AnthropicProvider, "anthropic_api_key", "anthropic_base_url"),
    "openai": (OpenAIProvider, "openai_api_key", "openai_base_url"),
    "gemini": (GeminiProvider, "gemini_api_key", "gemini_base_url"),
    "grok": (GrokProvider, "grok_api_key", "grok_base_url"),
}
