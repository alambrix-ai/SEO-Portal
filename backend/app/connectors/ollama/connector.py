"""Ollama connector — the customer's own model server, for writing.

Ollama serves an OpenAI-compatible API under ``/v1``. The address is the
customer's, so the connector goes through the egress guard (see
:class:`~app.connectors.base.llm_models.SelfHostedModelConnector`). Ollama
has no authentication of its own, so the description asks for an HTTPS
reverse proxy with authentication in front of it.
"""
from __future__ import annotations

from app.connectors.base.connector import Capability, ConnectorSpec, SetupGuide
from app.connectors.base.llm_models import SelfHostedModelConnector, self_hosted_fields

GUIDE = SetupGuide(
    steps=(
        'Ollama has no sign-in of its own. Put it behind an HTTPS reverse proxy that asks for a key.',
        'Paste the proxy’s address, for example https://models.example.com.',
        'If your proxy asks for a key, paste that key too.',
        'Pick the model your agents should write with from the list.',
    ),
    key_field='apiKey',
)


class OllamaConnector(SelfHostedModelConnector):
    server_name = "Ollama"
    models_url = "https://ollama.com/library"

    spec = ConnectorSpec(
        slug="ollama",
        guide=GUIDE,
        name="Your own model (Ollama)",
        category="AI models",
        description=(
            "Writing for agents and Creative Studio with a model on your own "
            "Ollama server. Your prompts and content go to your server, not to an "
            "AI company. Ollama has no sign-in, so put it behind an HTTPS reverse "
            "proxy that asks for a key. Text only; no images."
        ),
        fields=self_hosted_fields(
            "Ollama",
            key_help=(
                "Optional. The key your reverse proxy expects. Ollama itself "
                "ignores keys. Leave blank if your proxy asks for none."
            ),
        ),
        capabilities=frozenset({Capability.COMPLETE}),
        docs_url="https://github.com/ollama/ollama/blob/main/docs/openai.md",
        base_url_field="baseUrl",
        advanced=True,
    )


CONNECTOR_CLASS = OllamaConnector
