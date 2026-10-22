"""OpenAI connector — citation tracking through the Responses API web search.

ChatGPT's browsing answers are one of the highest-volume answer surfaces, so
this checks whether its search tool is surfacing the customer's pages.
"""
from __future__ import annotations

from app.connectors.base.connector import SetupGuide
from app.connectors.base.connector import Capability
from app.connectors.base.aeo_base import AEO_CAPABILITIES, BaseAeoConnector, urls_in_text
from app.connectors.base.connector import ConnectorSpec
from app.connectors.base.connector import HealthReport
from app.connectors.base.credentials import secret
from app.connectors.base.llm_fields import openai_llm_fields
from app.connectors.base.llm_models import ModelChoice
from app.core.logging import get_logger
from app.llm.model_catalog import ModelInfo, parse_openai

log = get_logger(__name__)


GUIDE = SetupGuide(
    steps=(
        'Open your OpenAI API keys page with the button below, and sign in if asked.',
        'Click “Create new secret key”, give it a name such as Synaprank, and create it.',
        'Copy the key and paste it here. OpenAI shows it only once.',
    ),
    key_url='https://platform.openai.com/api-keys',
    key_url_label='Open your OpenAI API keys page',
    key_field='apiKey',
    key_pattern='^sk-[A-Za-z0-9_\\-]{20,}$',
    key_hint='OpenAI keys start with sk-',
)


class OpenAiConnector(ModelChoice, BaseAeoConnector):
    engine = "openai"
    models_url = "https://platform.openai.com/docs/models"

    spec = ConnectorSpec(
        slug="openai",
        guide=GUIDE,
        name="OpenAI",
        category="AI models",
        description="Writing for agents, and citation checks in ChatGPT search.",
        fields=(
            secret("apiKey", "API key", "sk-••••••••"),
            *openai_llm_fields(),
        ),
        # Also makes images (app/services/images.py) with the same API key.
        capabilities=AEO_CAPABILITIES | {Capability.GENERATE_IMAGE},
        docs_url="https://platform.openai.com/docs/api-reference/chat",
        base_url="https://api.openai.com/v1",
    )

    def auth_headers(self) -> dict[str, str]:
        headers = {
            "Authorization": "Bearer " + self.credentials.require("apiKey"),
            "Content-Type": "application/json",
        }
        organization = self.credentials.get("organization")
        if organization:
            headers["OpenAI-Organization"] = organization
        return headers

    # ── Models: GET /v1/models (id, created, owned_by — no capability data) ──
    # https://developers.openai.com/api/reference/resources/models/methods/list
    def list_models(self) -> list[ModelInfo]:
        return parse_openai(self.request("GET", "/models"))

    def retrieve_model(self, model_id: str) -> None:
        self.request("GET", f"/models/{model_id}")

    def check_health(self) -> HealthReport:
        return self.model_health()

    def _probe_credentials(self) -> None:
        self.check_models()

    def verify_steps(self) -> list:
        """List models (GET /v1/models) and one tiny chat completion
        (POST /v1/chat/completions, max_completion_tokens 16) through the
        provider agents write with. Read only; nothing is created."""
        from app.connectors.base.llm_verify import model_list_step, tiny_completion_step

        return [model_list_step(self), tiny_completion_step(self)]

    def _ask_with_search(self, query: str) -> list[str]:
        data = self.request(
            "POST",
            "/responses",
            json_body={
                "model": self.credentials.require("model"),
                "input": query,
                "tools": [{"type": "web_search"}],
                "max_output_tokens": 400,
            },
        )
        payload = data if isinstance(data, dict) else {}
        urls: list[str] = []
        text_parts: list[str] = []

        # Citations arrive as url_citation annotations on the output text.
        for item in payload.get("output") or []:
            for block in (item or {}).get("content") or []:
                if block.get("type") in ("output_text", "text"):
                    text_parts.append(block.get("text") or "")
                for annotation in block.get("annotations") or []:
                    if annotation.get("type") == "url_citation" and annotation.get("url"):
                        urls.append(str(annotation["url"]))

        if urls:
            # Preserve order but drop repeats of the same source.
            seen: set[str] = set()
            return [u for u in urls if not (u in seen or seen.add(u))]

        return urls_in_text("\n".join(text_parts))


CONNECTOR_CLASS = OpenAiConnector
