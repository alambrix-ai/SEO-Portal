"""Perplexity connector — citation tracking via the Sonar API.

Perplexity is the most directly measurable answer engine: it returns the
sources behind every answer as structured data, so a citation is a fact rather
than an inference.
"""
from __future__ import annotations

from app.connectors.base.aeo_base import AEO_CAPABILITIES, BaseAeoConnector, urls_in_text
from app.connectors.base.connector import ConnectorSpec, HealthReport, SetupGuide
from app.connectors.base.credentials import secret
from app.connectors.base.llm_fields import perplexity_llm_fields
from app.connectors.base.llm_models import ModelChoice
from app.core.exceptions import ConnectorError
from app.core.logging import get_logger

log = get_logger(__name__)


GUIDE = SetupGuide(
    steps=(
        'Open the API Keys page in your Perplexity API console with the button below. Add credit first if it asks.',
        'Create a new API key.',
        'Copy the key and paste it here. It is shown only once.',
    ),
    key_url='https://console.perplexity.ai/project/keys',
    key_url_label='Open your Perplexity API keys',
    key_field='apiKey',
    key_pattern='^pplx-[A-Za-z0-9_\\-]{20,}$',
    key_hint='Perplexity keys start with pplx-',
)


class PerplexityConnector(ModelChoice, BaseAeoConnector):
    """Perplexity documents no models endpoint for this (Sonar chat
    completions) API — its ``GET /v1/models`` lists Agent API ids, which this
    endpoint does not take — so the model is typed, and checked with one tiny
    request when the connection is saved."""

    engine = "perplexity"
    models_url = "https://docs.perplexity.ai/docs/getting-started/models"

    spec = ConnectorSpec(
        slug="perplexity",
        guide=GUIDE,
        name="Perplexity",
        category="AI models",
        description="Writing for agents, and citation checks in Perplexity answers.",
        fields=(
            secret("apiKey", "API key", "pplx-••••••••"),
            *perplexity_llm_fields(),
        ),
        capabilities=AEO_CAPABILITIES,
        docs_url="https://docs.perplexity.ai/docs/sonar/openai-compatibility",
        base_url="https://api.perplexity.ai",
    )

    @property
    def model_id(self) -> str:
        """The model the person typed. Required; there is no default."""
        return self.credentials.require("model").strip()

    def auth_headers(self) -> dict[str, str]:
        return {
            "Authorization": "Bearer " + self.credentials.require("apiKey"),
            "Content-Type": "application/json",
        }

    def retrieve_model(self, model_id: str) -> None:
        # No model-lookup endpoint: one tiny completion proves the key and
        # the model name without running a full citation search.
        try:
            self.request(
                "POST",
                "/chat/completions",
                json_body={
                    "model": model_id,
                    "messages": [{"role": "user", "content": "ping"}],
                    "max_tokens": 1,
                },
            )
        except ConnectorError as exc:
            if getattr(exc, "http_status", None) in (400, 404):
                raise ConnectorError(
                    f"Perplexity did not accept the model {model_id}. Check the "
                    "name on Perplexity's models page and type it exactly."
                ) from exc
            raise

    def _probe_credentials(self) -> None:
        self.check_models()

    def verify_steps(self) -> list:
        """One tiny Sonar chat completion (POST /chat/completions,
        max_tokens 16) through the provider agents write with.

        There is no model-list step: Perplexity documents no models list for
        the Sonar API (see the class docstring), so a completion that the
        model name is accepted for is the check. Every Sonar request runs a
        web search and carries a per-request fee, hence the cost in the label.
        """
        from app.connectors.base.llm_verify import tiny_completion_step

        return [tiny_completion_step(self, cost="half a US cent to about 1.5 cents, by model")]

    def check_health(self) -> HealthReport:
        report = self.model_health()
        if report.ok:
            # There is no list to compare with, so the tiny request above was
            # the whole check; say so plainly rather than as a warning.
            return HealthReport(ok=True, detail="Connection looks good", checked_at=report.checked_at)
        return report

    def _ask_with_search(self, query: str) -> list[str]:
        data = self.request(
            "POST",
            "/chat/completions",
            json_body={
                "model": self.model_id,
                "messages": [{"role": "user", "content": query}],
                # Keep the answer short: only the source list is of interest.
                "max_tokens": 300,
                "return_citations": True,
            },
        )
        payload = data if isinstance(data, dict) else {}

        # Sonar returns citations as a top-level list of URLs; newer responses
        # use `search_results` objects instead, so both are handled.
        citations = payload.get("citations")
        if isinstance(citations, list) and citations:
            return [str(url) for url in citations if url]

        results = payload.get("search_results")
        if isinstance(results, list) and results:
            return [str(entry.get("url")) for entry in results if entry.get("url")]

        # Last resort: pull URLs out of the answer text itself.
        choices = payload.get("choices") or []
        content = ""
        if choices:
            content = ((choices[0] or {}).get("message") or {}).get("content") or ""
        return urls_in_text(content)


CONNECTOR_CLASS = PerplexityConnector
