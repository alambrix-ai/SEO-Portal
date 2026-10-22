"""Model choice for the AI connectors: the vendor's live list, and checking it.

Mixed into the OpenAI, Anthropic Claude, Google Gemini, Perplexity and Grok
connectors. Each one implements :meth:`ModelChoice.list_models` against its
vendor's official models endpoint (see :mod:`app.llm.model_catalog`) and
:meth:`ModelChoice.retrieve_model` against the single-model endpoint, which is
the fallback when a key may use models but not list them.

:class:`SelfHostedModelConnector` is the shared base of the vLLM and Ollama
connectors: a customer's own server, reached only through the egress guard.
"""
from __future__ import annotations

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential_jitter

from app.connectors.base.connector import BaseConnector, HealthReport
from app.core import egress
from app.core.config import settings
from app.core.exceptions import ConnectorConfigError, ConnectorError
from app.core.logging import get_logger
from app.db.base import utcnow
from app.llm import model_catalog
from app.llm.model_catalog import ModelInfo, ModelListUnavailable
from app.llm.providers import (
    SELF_HOSTED_REDIRECT_MESSAGE,
    check_self_hosted_base,
    is_redirect,
    self_hosted_client,
    self_hosted_root,
    send_capped,
)

log = get_logger(__name__)

#: The connector field holding the writing model, and the optional image model.
MODEL_FIELD = "model"
IMAGE_MODEL_FIELD = "imageModel"


class ModelChoice:
    """Live model list + "is the saved model still offered?" for an AI connector."""

    #: Where a person can read the vendor's own model descriptions.
    models_url: str = ""

    # Provided by BaseConnector.
    credentials: object
    name: str
    slug: str
    base_url: str

    # ── Per vendor ─────────────────────────────────────────────────────────
    def list_models(self) -> list[ModelInfo]:
        """Every model this key can use, from the vendor's models endpoint."""
        raise ModelListUnavailable(self.name)

    def retrieve_model(self, model_id: str) -> None:
        """Raise ConnectorError (with ``http_status``) unless the vendor knows this model."""
        raise NotImplementedError

    # ── Shared ─────────────────────────────────────────────────────────────
    def available_models(self) -> list[ModelInfo]:
        """:meth:`list_models`, reused for a few minutes per key."""
        # The key is optional on self-hosted servers; "" is then the key.
        key = model_catalog.cache_key(
            self.slug, self.base_url, self.credentials.get("apiKey") or ""  # type: ignore[attr-defined]
        )
        return model_catalog.cached(key, self.list_models)

    def chosen_model(self) -> str:
        return (self.credentials.get(MODEL_FIELD) or "").strip()  # type: ignore[attr-defined]

    def chosen_image_model(self) -> str:
        return (self.credentials.get(IMAGE_MODEL_FIELD) or "").strip()  # type: ignore[attr-defined]

    def check_models(self) -> str:
        """Verify the key and that the saved models are still offered.

        Returns the health detail. Raises :class:`ConnectorError` with the
        plain message the card and the runs show when a saved model has gone.
        When the key may not list models (or the vendor has no list), the
        writing model is checked on its own instead and the detail says so —
        a warning, not a failure.
        """
        model = self.chosen_model()
        if not model:
            raise ConnectorError(f"Choose a model for {self.name} from the list.")
        try:
            models = self.available_models()
        except ModelListUnavailable:
            models = None
        except ConnectorError as exc:
            # A refused key fails the same way on the single-model call
            # below, with the vendor's own wording; anything else (listing
            # not permitted for this key) is only a warning.
            log.info("%s model list unavailable: %s", self.slug, exc)
            models = None

        if models is None:
            self._retrieve_or_stale(model)
            return (
                "Connection looks good. The model list could not be read with "
                "this key, so only the chosen model was checked."
            )

        if model_catalog.find(models, model) is None:
            raise ConnectorError(model_catalog.stale_model_message(model, self.name))
        image = self.chosen_image_model()
        if image and model_catalog.find(models, image) is None:
            raise ConnectorError(model_catalog.stale_image_model_message(image, self.name))
        return "Connection looks good"

    def _retrieve_or_stale(self, model: str) -> None:
        try:
            self.retrieve_model(model)
        except ConnectorError as exc:
            if getattr(exc, "http_status", None) == 404:
                raise ConnectorError(
                    model_catalog.stale_model_message(model, self.name)
                ) from exc
            raise

    def model_health(self):  # noqa: ANN201 - HealthReport (import cycle)
        from app.connectors.base.connector import HealthReport
        from app.core.exceptions import ConnectorConfigError

        try:
            detail = self.check_models()
        except ConnectorConfigError:
            raise
        except ConnectorError as exc:
            return HealthReport(ok=False, detail=str(exc), checked_at=utcnow())
        return HealthReport(ok=True, detail=detail, checked_at=utcnow())


# ── Self-hosted servers (vLLM, Ollama) ─────────────────────────────────────
#: The connection field holding the server address.
BASE_URL_FIELD = "baseUrl"


def self_hosted_fields(server: str, *, key_help: str) -> tuple:  # tuple[CredentialField, ...]
    """The connect form of a self-hosted model server."""
    from app.connectors.base.credentials import model, number, secret, text

    return (
        text(
            BASE_URL_FIELD,
            "Server address",
            "https://models.example.com",
            help_text=(
                f"The web address of your {server} server. Your prompts and content "
                "are sent to this server, which you run, and nowhere else. Put it "
                "behind an HTTPS reverse proxy that asks for a key. Addresses on a "
                "private network work only after your platform administrator "
                "approves them."
            ),
        ),
        secret("apiKey", "API key", "Optional", required=False, help_text=key_help),
        model(
            "model",
            "Model",
            help_text="The list comes from your server. Pick the model your agents should write with.",
        ),
        number(
            "maxTokens",
            "Max tokens",
            "",
            required=False,
            help_text="Optional. Longest reply allowed, in tokens. Blank uses this platform's standard limit.",
        ),
    )


class SelfHostedModelConnector(ModelChoice, BaseConnector):
    """A customer's own OpenAI-compatible model server (vLLM, Ollama).

    The address is customer-supplied, so it is never trusted:

    * It is validated with :func:`app.core.egress.validate_base_url` against
      ``SELF_HOSTED_LLM_ALLOWED_HOSTS`` before any call (https unless
      allow-listed; private ranges need an entry; metadata never), and the
      connection is pinned by :func:`app.core.egress.pinned_client` with the
      same allow-list.
    * A redirect is an error, never followed. Replies are size-capped.
    * The key is optional; no ``Authorization`` header is sent without one.
    * Models come from ``GET {base}/v1/models``, which also checks that the
      server is reachable and accepts the key.

    Subclasses declare ``spec`` (with ``fields=self_hosted_fields(...)``).
    """

    #: Shown in messages ("your vLLM server").
    server_name: str = "model"

    @property
    def base_url(self) -> str:
        return self_hosted_root(self.credentials.get(BASE_URL_FIELD))

    def validated_base_url(self) -> str:
        """The server root, or :class:`~app.core.egress.EgressBlockedError`
        (a friendly 422) when the address may not be reached."""
        if not self.base_url:
            raise ConnectorConfigError(
                f"Enter the address of your {self.server_name} server first."
            )
        return check_self_hosted_base(
            self.base_url, allow=egress.self_hosted_llm_allow_list()
        )

    def auth_headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        key = self.credentials.get("apiKey")
        if key:
            headers["Authorization"] = "Bearer " + key
        return headers

    def _http(self) -> httpx.Client:
        if self._client is None:
            allow = egress.self_hosted_llm_allow_list()
            root = self.validated_base_url()
            self._client = self_hosted_client(root, allow=allow)
        return self._client

    @retry(
        retry=retry_if_exception_type((httpx.TransportError, httpx.HTTPStatusError)),
        stop=stop_after_attempt(settings.connector_max_attempts),
        wait=wait_exponential_jitter(initial=0.5, max=8),
        reraise=True,
    )
    def _send(self, request: httpx.Request) -> httpx.Response:
        client = self._http()
        try:
            response = send_capped(client, request)
        except egress.EgressBlockedError as exc:
            log.warning("%s refused by the egress guard: %s", self.slug, exc.reason)
            raise ConnectorError(exc.message) from exc
        if is_redirect(response.status_code):
            log.warning("%s answered with a redirect (HTTP %s)", self.slug, response.status_code)
            raise ConnectorError(SELF_HOSTED_REDIRECT_MESSAGE)
        if response.status_code in (429, 500, 502, 503, 504):
            response.raise_for_status()
        return response

    # ── Models: GET {base}/v1/models ───────────────────────────────────────
    def list_models(self) -> list[ModelInfo]:
        return model_catalog.parse_self_hosted(self.request("GET", "/v1/models"))

    def retrieve_model(self, model_id: str) -> None:
        # No single-model endpoint is relied on: the list is the check.
        if model_catalog.find(self.list_models(), model_id) is None:
            error = ConnectorError(model_catalog.stale_model_message(model_id, self.name))
            error.http_status = 404  # type: ignore[attr-defined]
            raise error

    def check_health(self) -> HealthReport:
        self.validated_base_url()
        return self.model_health()

    def verify_steps(self) -> list:
        """List the server's models (GET {base}/v1/models) and one tiny chat
        completion through the provider agents write with. Read only."""
        from app.connectors.base.llm_verify import model_list_step, tiny_completion_step

        return [
            model_list_step(self),
            tiny_completion_step(self, cost="nothing here; it runs on your own server"),
        ]
