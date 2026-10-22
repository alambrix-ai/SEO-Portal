"""The model list in the connect dialog, for servers that need no key.

A self-hosted server (vLLM, Ollama) may run without a key, so the list must
load as soon as its address is typed. A cloud vendor still needs its key
before anything is sent. Pure unit tests: the server is an
httpx.MockTransport and no record is read from a database.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest

from app.connectors.base.llm_models import SelfHostedModelConnector
from app.llm import model_catalog
from app.services import connectors as service

BASE = "https://models.example.com"


@pytest.fixture(autouse=True)
def _fresh_cache():
    model_catalog.clear_cache()
    yield
    model_catalog.clear_cache()


@pytest.fixture()
def calls(monkeypatch: pytest.MonkeyPatch) -> list[httpx.Request]:
    """Every request a connector sends, answered with a two-model list."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200, json={"object": "list", "data": [{"id": "m-a"}, {"id": "org/m-b"}]}
        )

    # Captured first: the cloud vendors' client is patched below, on the
    # same httpx module.
    real_client = httpx.Client

    def _http(self):  # noqa: ANN001, ANN202
        if self._client is None:
            self._client = real_client(
                base_url=self.base_url, transport=httpx.MockTransport(handler)
            )
        return self._client

    monkeypatch.setattr(SelfHostedModelConnector, "_http", _http)

    def cloud_client(**kwargs):  # noqa: ANN003, ANN202
        return real_client(transport=httpx.MockTransport(handler), **kwargs)

    from app.connectors.base import connector as base

    monkeypatch.setattr(base.httpx, "Client", cloud_client)
    record = SimpleNamespace(
        connected=False, credential_hints={}, credentials_encrypted="", oauth_completed=False
    )
    monkeypatch.setattr(service, "get_record", lambda db, tenant_id, slug: record)
    monkeypatch.setattr(service.OrgCipher, "for_org", classmethod(lambda cls, org: None))
    return seen


def _list(slug: str, values: dict[str, str]) -> service.ModelListing:
    return service.model_listing(None, org=SimpleNamespace(id="o"), slug=slug, values=values)


@pytest.mark.parametrize("slug", ["vllm", "ollama"])
def test_a_keyless_self_hosted_server_lists_its_models(slug: str, calls: list[httpx.Request]):
    listing = _list(slug, {"baseUrl": BASE})
    assert listing.error == ""
    assert [m.id for m in listing.models] == ["m-a", "org/m-b"]
    assert len(calls) == 1 and calls[0].url.path == "/v1/models"
    # No key was typed, so none is sent.
    assert "authorization" not in calls[0].headers


@pytest.mark.parametrize("slug", ["vllm", "ollama"])
def test_a_self_hosted_server_without_an_address_is_not_called(
    slug: str, calls: list[httpx.Request]
):
    listing = _list(slug, {"apiKey": "proxy-key-123", "baseUrl": "   "})
    assert listing.models == []
    assert "address" in listing.error
    assert calls == []


@pytest.mark.parametrize("slug", ["openai", "anthropic_claude", "google_gemini", "xai_grok"])
def test_a_cloud_vendor_without_a_key_is_still_refused(slug: str, calls: list[httpx.Request]):
    # A base URL typed into a cloud vendor's form changes nothing.
    listing = _list(slug, {"baseUrl": BASE})
    assert listing.models == []
    assert "API key first" in listing.error
    assert calls == []


def test_the_key_is_never_returned_with_a_self_hosted_list(calls: list[httpx.Request]):
    from app.api.v1 import connectors as routes

    secret = "proxy-SECRET-key-0123456789"
    body = routes._model_list_out(_list("vllm", {"baseUrl": BASE, "apiKey": secret})).model_dump_json()
    assert secret not in body
    assert [m["id"] for m in json.loads(body)["models"]] == ["m-a", "org/m-b"]
    assert calls[0].headers["authorization"] == f"Bearer {secret}"
