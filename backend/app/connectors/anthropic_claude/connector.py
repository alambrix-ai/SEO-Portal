"""Anthropic Claude connector — citation tracking via the web search tool.

Uses the official Anthropic SDK rather than raw HTTP, and the server-side web
search tool: the search runs on Anthropic's infrastructure and the sources
come back as structured result blocks, so a citation is observed rather than
parsed out of prose.
"""
from __future__ import annotations

from app.connectors.base.aeo_base import AEO_CAPABILITIES, BaseAeoConnector, urls_in_text
from app.connectors.base.connector import ConnectorSpec, HealthReport
from app.connectors.base.credentials import secret, text
from app.core.exceptions import ConnectorError
from app.core.logging import get_logger
from app.core.user_messages import message_for_http_status, message_for_unreachable
from app.db.base import utcnow

log = get_logger(__name__)

# The current web search tool version, which returns structured results.
WEB_SEARCH_TOOL = "web_search_20260209"


class AnthropicClaudeConnector(BaseAeoConnector):
    engine = "claude"

    spec = ConnectorSpec(
        slug="anthropic_claude",
        name="Anthropic Claude",
        category="AEO/LLM Monitoring",
        description="Track whether Claude's web search cites your pages.",
        fields=(
            secret("apiKey", "API key", "sk-ant-••••••••"),
            text(
                "model",
                "Model",
                "claude-sonnet-4-5",
                help_text="The exact model name from Anthropic — there is no default.",
            ),
        ),
        capabilities=AEO_CAPABILITIES,
        docs_url="https://docs.anthropic.com/en/api/messages",
    )

    def _anthropic(self):  # noqa: ANN202 - the SDK's client type
        # Named deliberately not `_client`: BaseConnector stores an httpx
        # client on `self._client`, which would shadow a method of that name
        # and make `self._client()` raise "'NoneType' object is not callable".
        try:
            import anthropic
        except ImportError as exc:  # pragma: no cover
            raise ConnectorError(
                "Anthropic Claude is not available in this deployment"
            ) from exc
        return anthropic.Anthropic(api_key=self.credentials.require("apiKey")), anthropic

    def _ask_with_search(self, query: str) -> list[str]:
        client, anthropic = self._anthropic()
        model = self.credentials.require("model")

        try:
            message = client.messages.create(
                model=model,
                max_tokens=1024,
                messages=[{"role": "user", "content": query}],
                tools=[{"type": WEB_SEARCH_TOOL, "name": "web_search"}],
            )
        except anthropic.NotFoundError as exc:
            raise ConnectorError(
                "That model name was not found. Check the model field and try again."
            ) from exc
        except anthropic.RateLimitError as exc:
            raise ConnectorError(
                message_for_http_status(429, service=self.name)
            ) from exc
        except anthropic.AuthenticationError as exc:
            raise ConnectorError(
                message_for_http_status(401, service=self.name)
            ) from exc
        except anthropic.APIStatusError as exc:
            log.warning("Anthropic API status %s: %s", exc.status_code, exc)
            raise ConnectorError(
                message_for_http_status(exc.status_code, service=self.name)
            ) from exc
        except anthropic.APIConnectionError as exc:
            raise ConnectorError(message_for_unreachable(self.name)) from exc

        # A refusal is not a connector failure: it means this particular query
        # was declined, and the citation check simply has no result for it.
        if message.stop_reason == "refusal":
            log.info("Claude declined the citation-check query")
            return []

        urls: list[str] = []
        text_parts: list[str] = []

        for block in message.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "web_search_tool_result":
                # On success `.content` is a list of results; on error it is a
                # single error object, so the type has to be checked.
                results = getattr(block, "content", None)
                if isinstance(results, list):
                    for result in results:
                        url = getattr(result, "url", None)
                        if url:
                            urls.append(str(url))

        if urls:
            seen: set[str] = set()
            return [u for u in urls if not (u in seen or seen.add(u))]
        return urls_in_text("\n".join(text_parts))

    def _probe_credentials(self) -> None:
        client, _ = self._anthropic()
        client.models.retrieve(self.credentials.require("model"))

    def check_health(self) -> HealthReport:
        try:
            self._probe_credentials()
        except ConnectorError as exc:
            return HealthReport(ok=False, detail=str(exc), checked_at=utcnow())
        except Exception as exc:  # noqa: BLE001 - a probe must not raise
            log.warning("Anthropic health probe failed: %s", exc)
            return HealthReport(
                ok=False,
                detail="Could not verify those credentials. Check the key and model name.",
                checked_at=utcnow(),
            )
        return HealthReport(ok=True, detail="Connection looks good", checked_at=utcnow())


CONNECTOR_CLASS = AnthropicClaudeConnector
