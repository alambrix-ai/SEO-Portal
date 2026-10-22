"""Anthropic Claude connector — citation tracking via the web search tool.

Uses the official Anthropic SDK rather than raw HTTP, and the server-side web
search tool: the search runs on Anthropic's infrastructure and the sources
come back as structured result blocks, so a citation is observed rather than
parsed out of prose.
"""
from __future__ import annotations

from app.connectors.base.aeo_base import AEO_CAPABILITIES, BaseAeoConnector, urls_in_text
from app.connectors.base.connector import ConnectorSpec, HealthReport, SetupGuide
from app.connectors.base.credentials import secret
from app.connectors.base.llm_fields import anthropic_llm_fields
from app.connectors.base.llm_models import ModelChoice
from app.core.config import settings
from app.core.exceptions import ConnectorError
from app.core.logging import get_logger
from app.core.user_messages import message_for_http_status, message_for_unreachable
from app.llm.model_catalog import ModelInfo, newest_first, paginate, parse_anthropic_page

log = get_logger(__name__)

# The current web search tool version, which returns structured results.
WEB_SEARCH_TOOL = "web_search_20260209"


GUIDE = SetupGuide(
    steps=(
        'Open the API keys page in your Claude Console with the button below, and sign in if asked.',
        'Click “Create Key”, give it a name such as Synaprank, and create it.',
        'Copy the key and paste it here. It is shown only once.',
    ),
    key_url='https://platform.claude.com/settings/keys',
    key_url_label='Open your Claude Console API keys',
    key_field='apiKey',
    key_pattern='^sk-ant-[A-Za-z0-9_\\-]{20,}$',
    key_hint='Claude API keys start with sk-ant-',
)


class AnthropicClaudeConnector(ModelChoice, BaseAeoConnector):
    engine = "claude"
    models_url = "https://platform.claude.com/docs/en/about-claude/models/overview"

    spec = ConnectorSpec(
        slug="anthropic_claude",
        guide=GUIDE,
        name="Anthropic Claude",
        category="AI models",
        description="Writing for agents, and citation checks in Claude web search.",
        fields=(
            secret("apiKey", "API key", "sk-ant-••••••••"),
            *anthropic_llm_fields(),
        ),
        capabilities=AEO_CAPABILITIES,
        docs_url="https://docs.anthropic.com/en/api/messages",
        # The Models API is read over plain HTTP; citation checks use the SDK.
        base_url="https://api.anthropic.com/v1",
    )

    @property
    def model_id(self) -> str:
        """The model picked from the vendor's list. Required; there is no default."""
        return self.credentials.require("model").strip()

    def auth_headers(self) -> dict[str, str]:
        return {
            "x-api-key": self.credentials.require("apiKey"),
            "anthropic-version": settings.anthropic_api_version,
        }

    # ── Models: GET /v1/models, paginated (has_more / last_id -> after_id) ──
    # https://platform.claude.com/docs/en/api/models/list
    def list_models(self) -> list[ModelInfo]:
        def page(cursor: str) -> tuple[list[ModelInfo], str]:
            params: dict[str, str | int] = {"limit": 1000}
            if cursor:
                params["after_id"] = cursor
            return parse_anthropic_page(self.request("GET", "/models", params=params))

        return newest_first(paginate(page))

    def retrieve_model(self, model_id: str) -> None:
        self.request("GET", f"/models/{model_id}")

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
        model = self.model_id

        try:
            message = client.messages.create(
                model=model,
                max_tokens=1024,
                messages=[{"role": "user", "content": query}],
                tools=[{"type": WEB_SEARCH_TOOL, "name": "web_search"}],
            )
        except anthropic.NotFoundError as exc:
            from app.llm.model_catalog import stale_model_message

            raise ConnectorError(stale_model_message(model, self.name)) from exc
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
        self.check_models()

    def verify_steps(self) -> list:
        """List models (GET /v1/models) and one tiny Messages call
        (POST /v1/messages, max_tokens 16) through the provider agents write
        with. Read only; nothing is created."""
        from app.connectors.base.llm_verify import model_list_step, tiny_completion_step

        return [model_list_step(self), tiny_completion_step(self)]

    def check_health(self) -> HealthReport:
        return self.model_health()


CONNECTOR_CLASS = AnthropicClaudeConnector
