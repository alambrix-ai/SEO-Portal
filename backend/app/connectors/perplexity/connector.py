"""Perplexity connector — citation tracking via the Sonar API.

Perplexity is the most directly measurable answer engine: it returns the
sources behind every answer as structured data, so a citation is a fact rather
than an inference.
"""
from __future__ import annotations

from app.connectors.base.aeo_base import AEO_CAPABILITIES, BaseAeoConnector, urls_in_text
from app.connectors.base.connector import ConnectorSpec
from app.connectors.base.credentials import secret, text
from app.core.logging import get_logger

log = get_logger(__name__)


class PerplexityConnector(BaseAeoConnector):
    engine = "perplexity"

    spec = ConnectorSpec(
        slug="perplexity",
        name="Perplexity",
        category="AI models",
        description="Writing for agents, and citation checks in Perplexity answers.",
        fields=(
            secret("apiKey", "API key", "pplx-••••••••"),
            text(
                "model",
                "Model",
                "sonar",
                help_text="The exact model name from Perplexity — there is no default.",
            ),
        ),
        capabilities=AEO_CAPABILITIES,
        docs_url="https://docs.perplexity.ai/",
        base_url="https://api.perplexity.ai",
    )

    def auth_headers(self) -> dict[str, str]:
        return {
            "Authorization": "Bearer " + self.credentials.require("apiKey"),
            "Content-Type": "application/json",
        }

    def _probe_credentials(self) -> None:
        # Perplexity has no cheap model-lookup endpoint; one tiny completion
        # is enough to prove the key without running a full citation search.
        self.request(
            "POST",
            "/chat/completions",
            json_body={
                "model": self.credentials.require("model"),
                "messages": [{"role": "user", "content": "ping"}],
                "max_tokens": 1,
            },
        )

    def _ask_with_search(self, query: str) -> list[str]:
        data = self.request(
            "POST",
            "/chat/completions",
            json_body={
                "model": self.credentials.require("model"),
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
