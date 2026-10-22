"""Google Gemini connector — citation tracking via grounded generation.

Gemini's grounding metadata names the web sources behind an answer, which is
what makes AI Overviews-style visibility measurable.
"""
from __future__ import annotations

import re
from urllib.parse import urlsplit

from app.connectors.base.aeo_base import AEO_CAPABILITIES, BaseAeoConnector, urls_in_text
from app.connectors.base.connector import Capability, ConnectorSpec, HealthReport, SetupGuide
from app.connectors.base.credentials import secret
from app.connectors.base.llm_fields import gemini_llm_fields
from app.connectors.base.llm_models import ModelChoice
from app.core.logging import get_logger
from app.llm.model_catalog import ModelInfo, newest_first, paginate, parse_gemini_page

log = get_logger(__name__)

_DOMAIN_RE = re.compile(r"^(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,}$", re.I)


def _is_grounding_redirect(uri: str) -> bool:
    host = (urlsplit(uri).netloc or "").lower()
    return host.endswith("vertexaisearch.cloud.google.com")


def _looks_like_domain(value: str) -> bool:
    return bool(_DOMAIN_RE.match(value.removeprefix("www.")))


GUIDE = SetupGuide(
    steps=(
        'Open the API keys page in Google AI Studio with the button below, and sign in with your Google account.',
        'Click “Create API key” and pick a project (or let AI Studio create one).',
        'Copy the key and paste it here.',
    ),
    key_url='https://aistudio.google.com/api-keys',
    key_url_label='Open Google AI Studio API keys',
    key_field='apiKey',
    key_pattern='^AIza[0-9A-Za-z_\\-]{30,}$',
    key_hint='Google API keys start with AIza',
)


class GoogleGeminiConnector(ModelChoice, BaseAeoConnector):
    engine = "gemini"
    models_url = "https://ai.google.dev/gemini-api/docs/models"

    spec = ConnectorSpec(
        slug="google_gemini",
        guide=GUIDE,
        name="Google Gemini",
        category="AI models",
        description="Writing for agents, and citation checks in Gemini answers.",
        fields=(
            secret("apiKey", "API key", "AIza••••••••"),
            *gemini_llm_fields(),
        ),
        # Also makes images (app/services/images.py) with the same API key.
        capabilities=AEO_CAPABILITIES | {Capability.GENERATE_IMAGE},
        docs_url="https://ai.google.dev/gemini-api/docs",
        base_url="https://generativelanguage.googleapis.com/v1beta",
    )

    @property
    def model_id(self) -> str:
        """The model picked from the vendor's list. Required; there is no default."""
        return self.credentials.require("model").strip()

    def auth_headers(self) -> dict[str, str]:
        return {
            # Gemini takes the key in a header rather than a query parameter,
            # which keeps it out of request logs and proxies.
            "x-goog-api-key": self.credentials.require("apiKey"),
            "Content-Type": "application/json",
        }

    # ── Models: GET /v1beta/models, paginated (nextPageToken -> pageToken) ──
    # https://ai.google.dev/api/models — no creation time, no output-modality
    # field; supportedGenerationMethods tells generateContent models apart.
    def list_models(self) -> list[ModelInfo]:
        def page(cursor: str) -> tuple[list[ModelInfo], str]:
            params: dict[str, str | int] = {"pageSize": 1000}
            if cursor:
                params["pageToken"] = cursor
            return parse_gemini_page(self.request("GET", "/models", params=params))

        return newest_first(paginate(page))

    def retrieve_model(self, model_id: str) -> None:
        self.request("GET", f"/models/{model_id}")

    def _probe_credentials(self) -> None:
        self.check_models()

    def verify_steps(self) -> list:
        """List models (GET /v1beta/models) and one tiny generateContent call
        (maxOutputTokens 16) through the provider agents write with. Read
        only; nothing is created. The free tier covers both."""
        from app.connectors.base.llm_verify import model_list_step, tiny_completion_step

        return [
            model_list_step(self),
            tiny_completion_step(self, cost="nothing on the free tier, a fraction of a cent otherwise"),
        ]

    def check_health(self) -> HealthReport:
        return self.model_health()

    def _ask_with_search(self, query: str) -> list[str]:
        model = self.model_id
        data = self.request(
            "POST",
            f"/models/{model}:generateContent",
            json_body={
                "contents": [{"role": "user", "parts": [{"text": query}]}],
                # Grounding is what produces the citation metadata.
                "tools": [{"google_search": {}}],
                "generationConfig": {"maxOutputTokens": 400},
            },
        )
        payload = data if isinstance(data, dict) else {}
        candidates = payload.get("candidates") or []
        if not candidates:
            return []

        candidate = candidates[0] or {}
        grounding = candidate.get("groundingMetadata") or {}
        urls: list[str] = []

        for chunk in grounding.get("groundingChunks") or []:
            web = (chunk or {}).get("web") or {}
            # `uri` is a redirect wrapper on vertexaisearch.cloud.google.com;
            # the cited site's domain is carried in `title` ("example.com").
            # The old loop took `uri` and stopped, so the customer's domain
            # could never match. One source per chunk keeps positions honest:
            # a real URI if Gemini gave one, else the domain from the title.
            uri = str(web.get("uri") or "")
            title = str(web.get("title") or "").strip()
            if uri and not _is_grounding_redirect(uri):
                urls.append(uri)
            elif _looks_like_domain(title):
                urls.append(title)
            elif uri:
                urls.append(uri)

        if urls:
            seen: set[str] = set()
            return [u for u in urls if not (u in seen or seen.add(u))]

        text = "".join(
            part.get("text") or ""
            for part in ((candidate.get("content") or {}).get("parts") or [])
        )
        return urls_in_text(text)


CONNECTOR_CLASS = GoogleGeminiConnector
