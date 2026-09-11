"""OpenAI connector — citation tracking through the Responses API web search.

ChatGPT's browsing answers are one of the highest-volume answer surfaces, so
this checks whether its search tool is surfacing the customer's pages.
"""
from __future__ import annotations

from app.connectors.base.aeo_base import AEO_CAPABILITIES, BaseAeoConnector, urls_in_text
from app.connectors.base.connector import ConnectorSpec
from app.connectors.base.credentials import secret, text
from app.core.logging import get_logger

log = get_logger(__name__)


class OpenAiConnector(BaseAeoConnector):
    engine = "openai"

    spec = ConnectorSpec(
        slug="openai",
        name="OpenAI",
        category="AEO/LLM Monitoring",
        description="Check whether ChatGPT's search surfaces cite your pages.",
        fields=(
            secret("apiKey", "API key", "sk-••••••••"),
            text(
                "model",
                "Model",
                "gpt-4o",
                help_text="The exact model name from OpenAI — there is no default.",
            ),
        ),
        capabilities=AEO_CAPABILITIES,
        docs_url="https://platform.openai.com/docs/api-reference/responses",
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

    def _probe_credentials(self) -> None:
        model = self.credentials.require("model")
        self.request("GET", f"/models/{model}")

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
