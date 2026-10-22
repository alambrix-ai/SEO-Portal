"""A recording fake vendor for the live-check (``verify_steps``) tests.

``install(connector, routes)`` swaps the connector's HTTP client for one on an
``httpx.MockTransport``, so every call still goes through
``BaseConnector.request`` — the same path the live run and its cassette
recording take — and the full URL (host included) can be asserted on.

``routes`` is a list of ``(METHOD, pattern, response)``. ``pattern`` is a
regular expression searched in the full URL. ``response`` is a dict/list
(JSON, status 200), an ``(status, body)`` tuple, or a callable taking the
``httpx.Request`` and returning either. The first matching route wins; an
unmatched call fails the test with the call it did not expect.
"""
from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any

import httpx

Route = tuple[str, str, Any]


class Recorder:
    def __init__(self, routes: list[Route]) -> None:
        self.routes = [(m.upper(), re.compile(p), r) for m, p, r in routes]
        self.calls: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        url = str(request.url)
        for method, pattern, response in self.routes:
            if method != request.method or not pattern.search(url):
                continue
            if callable(response):
                response = response(request)
            status, body = (response if isinstance(response, tuple) else (200, response))
            if isinstance(body, (bytes, str)):
                return httpx.Response(status, content=body if isinstance(body, bytes) else body.encode())
            if body is None:
                return httpx.Response(status)
            return httpx.Response(status, json=body)
        raise AssertionError(f"Unexpected call: {request.method} {url}")

    # ── Assertions ─────────────────────────────────────────────────────────
    @property
    def methods_and_urls(self) -> list[tuple[str, str]]:
        return [(c.method, str(c.url)) for c in self.calls]

    def find(self, method: str, pattern: str) -> list[httpx.Request]:
        rx = re.compile(pattern)
        return [c for c in self.calls if c.method == method.upper() and rx.search(str(c.url))]

    def index(self, method: str, pattern: str) -> int:
        rx = re.compile(pattern)
        for i, c in enumerate(self.calls):
            if c.method == method.upper() and rx.search(str(c.url)):
                return i
        raise AssertionError(f"No {method} call matching {pattern}: {self.methods_and_urls}")

    def body(self, method: str, pattern: str, n: int = 0) -> Any:  # noqa: ANN401
        request = self.find(method, pattern)[n]
        raw = request.content.decode() if request.content else ""
        try:
            return json.loads(raw) if raw else {}
        except ValueError:
            return raw


def install(connector: Any, routes: list[Route]) -> Recorder:  # noqa: ANN401
    from app.core.exceptions import ConnectorConfigError

    recorder = Recorder(routes)
    try:
        base = connector.base_url
    except ConnectorConfigError:
        # Mailchimp, Salesforce: the host comes from the key or the sign-in.
        base = "https://unconfigured.invalid"
    connector._client = httpx.Client(
        base_url=base,
        transport=httpx.MockTransport(recorder),
        headers={"User-Agent": "Synaprank/1.0", "Accept": "application/json"},
    )
    return recorder


def run_all(connector: Any, *, kinds: tuple[str, ...] = ("read", "write")) -> dict[str, tuple[str, dict]]:  # noqa: ANN401
    """Run every step of the given kinds, in order, keyed by step key."""
    out: dict[str, tuple[str, dict]] = {}
    for step in connector.verify_steps():
        if step.kind in kinds:
            out[step.key] = step.run()
    return out


def assert_no_secrets(connector: Any, results: dict[str, tuple[str, dict]]) -> None:  # noqa: ANN401
    """No secret credential value (four characters or more) in any detail or evidence."""
    secret_keys = {f.key for f in connector.spec.fields if f.is_secret} | {
        "accessToken", "refreshToken", "clientSecret", "apiKey", "password", "apiToken",
    }
    secrets = [
        v for k, v in connector.credentials.values.items()
        if k in secret_keys and isinstance(v, str) and len(v) >= 4
    ]
    for key, (detail, evidence) in results.items():
        blob = detail + " " + json.dumps(evidence, default=str)
        for value in secrets:
            assert value not in blob, f"{key} leaks a credential value"


Handler = Callable[[httpx.Request], Any]
