"""Grok (xAI) connector — writing for agents and Creative Studio.

xAI's API is OpenAI-compatible: ``https://api.x.ai/v1`` with a Bearer key,
the model list at ``GET /v1/models``, and chat completions that take
``max_completion_tokens``. The address is fixed to xAI's own; nothing the
customer types changes where requests go.
"""
from __future__ import annotations

import re

from app.connectors.base.connector import (
    BaseConnector,
    Capability,
    ConnectorSpec,
    HealthReport,
    SetupGuide,
)
from app.connectors.base.credentials import model, number, secret
from app.connectors.base.llm_models import ModelChoice
from app.core.exceptions import ConnectorError
from app.core.logging import get_logger
from app.llm.model_catalog import ModelInfo, parse_openai, stale_model_message

log = get_logger(__name__)

#: A model id that may go into the ``/models/{id}`` path.
_PATH_SAFE_MODEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


GUIDE = SetupGuide(
    steps=(
        'Open the xAI console with the button below, and sign in if asked.',
        'Open “API Keys” and click “Create API key”. Give it a name such as Synaprank.',
        'Copy the key and paste it here. xAI shows it only once.',
    ),
    key_url='https://console.x.ai',
    key_url_label='Open the xAI console',
    key_field='apiKey',
    key_pattern='^xai-[A-Za-z0-9_\\-]{20,}$',
    key_hint='xAI keys start with xai-',
)


class XaiGrokConnector(ModelChoice, BaseConnector):
    models_url = "https://docs.x.ai/docs/models"

    spec = ConnectorSpec(
        slug="xai_grok",
        guide=GUIDE,
        name="Grok (xAI)",
        category="AI models",
        description=(
            "Writing for agents and Creative Studio with Grok. xAI keeps API "
            "requests for up to 30 days to check for abuse and does not train "
            "its models on them. Zero data retention is available to "
            "enterprise customers."
        ),
        fields=(
            secret("apiKey", "API key", "xai-••••••••"),
            model(
                "model",
                "Model",
                help_text=(
                    "The list comes from your xAI account. Pick the model your "
                    "agents should write with."
                ),
            ),
            number(
                "maxTokens",
                "Max completion tokens",
                "",
                required=False,
                help_text="Optional. Longest reply allowed. Blank uses this platform's standard limit.",
            ),
            number(
                "temperature",
                "Temperature",
                "",
                required=False,
                help_text="Optional, 0–2. Blank uses xAI's default.",
            ),
        ),
        capabilities=frozenset({Capability.COMPLETE}),
        docs_url="https://docs.x.ai/docs/api-reference",
        base_url="https://api.x.ai/v1",
    )

    def auth_headers(self) -> dict[str, str]:
        return {
            "Authorization": "Bearer " + self.credentials.require("apiKey"),
            "Content-Type": "application/json",
        }

    # ── Models: GET /v1/models (id, created, owned_by) ─────────────────────
    def list_models(self) -> list[ModelInfo]:
        return parse_openai(self.request("GET", "/models"))

    def retrieve_model(self, model_id: str) -> None:
        # The id goes into the path, so anything but a plain id is refused.
        if not _PATH_SAFE_MODEL.fullmatch(model_id or ""):
            error = ConnectorError(stale_model_message(model_id, self.name))
            error.http_status = 404  # type: ignore[attr-defined]
            raise error
        self.request("GET", f"/models/{model_id}")

    def check_health(self) -> HealthReport:
        return self.model_health()

    def verify_steps(self) -> list:
        """List models (GET /v1/models) and one tiny chat completion
        (POST /v1/chat/completions, max_completion_tokens 16) through the
        provider agents write with. Read only; nothing is created."""
        from app.connectors.base.llm_verify import model_list_step, tiny_completion_step

        return [model_list_step(self), tiny_completion_step(self)]


CONNECTOR_CLASS = XaiGrokConnector
