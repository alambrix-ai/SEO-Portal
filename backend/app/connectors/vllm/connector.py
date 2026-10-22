"""vLLM connector — the customer's own model server, for writing.

vLLM serves an OpenAI-compatible API. The address is the customer's, so the
connector goes through the egress guard (see
:class:`~app.connectors.base.llm_models.SelfHostedModelConnector`). vLLM's
``--api-key`` protects only its ``/v1`` routes, so the description asks for
an HTTPS reverse proxy with authentication in front of it.
"""
from __future__ import annotations

from app.connectors.base.connector import Capability, ConnectorSpec, SetupGuide
from app.connectors.base.llm_models import SelfHostedModelConnector, self_hosted_fields

GUIDE = SetupGuide(
    steps=(
        'Make sure your vLLM server is reachable over HTTPS, ideally behind a reverse proxy that asks for a key.',
        'Paste the server’s address, for example https://models.example.com.',
        'If the server was started with --api-key, or your proxy asks for a key, paste that key too.',
        'Pick the model your agents should write with from the list.',
    ),
    key_field='apiKey',
)


class VllmConnector(SelfHostedModelConnector):
    server_name = "vLLM"
    models_url = "https://docs.vllm.ai/en/latest/serving/openai_compatible_server.html"

    spec = ConnectorSpec(
        slug="vllm",
        guide=GUIDE,
        name="Your own model (vLLM)",
        category="AI models",
        description=(
            "Writing for agents and Creative Studio with a model on your own vLLM "
            "server. Your prompts and content go to your server, not to an AI "
            "company. Put the server behind an HTTPS reverse proxy that asks for "
            "a key. Text only; no images."
        ),
        fields=self_hosted_fields(
            "vLLM",
            key_help=(
                "Optional. The key your server or proxy expects (vLLM's --api-key). "
                "Leave blank if it asks for none."
            ),
        ),
        capabilities=frozenset({Capability.COMPLETE}),
        docs_url="https://docs.vllm.ai/en/latest/serving/openai_compatible_server.html",
        base_url_field="baseUrl",
        advanced=True,
    )


CONNECTOR_CLASS = VllmConnector
