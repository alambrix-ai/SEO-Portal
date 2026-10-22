"""The request-body size limit (``BodySizeLimitMiddleware``).

Bodies over ``MAX_REQUEST_BODY_BYTES`` get a friendly 413, whether the size
is declared up front or only discovered while the body streams in. The
image-upload route is exempt (it has its own cap). No database is needed.
"""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import asynccontextmanager

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from pydantic import BaseModel

from app.api.middleware import (
    BODY_LIMIT_EXEMPT_PATHS,
    REQUEST_TOO_LARGE_MESSAGE,
    BodySizeLimitMiddleware,
)
from app.core.config import settings

LIMIT = 2048
PREFIX = settings.api_v1_prefix.rstrip("/")


class _Body(BaseModel):
    text: str


def _app(calls: list[str]) -> FastAPI:
    app = FastAPI()
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=LIMIT)

    @app.post(f"{PREFIX}/echo")
    def echo(body: _Body) -> dict[str, int]:
        calls.append("echo")
        return {"length": len(body.text)}

    @app.post(f"{PREFIX}/raw")
    async def raw(request: Request) -> dict[str, int]:
        data = await request.body()
        calls.append("raw")
        return {"length": len(data)}

    @app.post(f"{PREFIX}/brand-kit/uploads")
    async def upload(request: Request) -> dict[str, int]:
        calls.append("upload")
        return {"length": len(await request.body())}

    @app.post(f"{PREFIX}/brand-kit/uploads-extra")
    async def lookalike(request: Request) -> dict[str, int]:
        calls.append("lookalike")
        return {"length": len(await request.body())}

    return app


@pytest.fixture
def calls() -> list[str]:
    return []


@pytest.fixture
def client(calls: list[str]) -> Iterator[TestClient]:
    with TestClient(_app(calls), raise_server_exceptions=False) as test_client:
        yield test_client


def _assert_413(response) -> None:  # noqa: ANN001
    assert response.status_code == 413
    body = response.json()
    assert body["code"] == "request_too_large"
    assert body["detail"] == REQUEST_TOO_LARGE_MESSAGE
    assert body["fields"] == {}


def _chunks(total: int, size: int = 512) -> Iterator[bytes]:
    sent = 0
    while sent < total:
        piece = min(size, total - sent)
        sent += piece
        yield b"a" * piece


# ── Happy path ──────────────────────────────────────────────────────────────
def test_a_small_json_body_passes(client: TestClient, calls: list[str]) -> None:
    response = client.post(f"{PREFIX}/echo", json={"text": "hello"})
    assert response.status_code == 200 and response.json() == {"length": 5}
    assert calls == ["echo"]


def test_a_body_exactly_at_the_limit_passes(client: TestClient) -> None:
    response = client.post(f"{PREFIX}/raw", content=b"a" * LIMIT)
    assert response.status_code == 200 and response.json() == {"length": LIMIT}


def test_a_small_streamed_body_passes(client: TestClient) -> None:
    response = client.post(f"{PREFIX}/raw", content=_chunks(LIMIT // 2))
    assert response.status_code == 200 and response.json() == {"length": LIMIT // 2}


def test_the_upload_route_is_exempt(client: TestClient, calls: list[str]) -> None:
    assert "/brand-kit/uploads" in BODY_LIMIT_EXEMPT_PATHS
    response = client.post(f"{PREFIX}/brand-kit/uploads", content=b"a" * (LIMIT * 4))
    assert response.status_code == 200 and response.json() == {"length": LIMIT * 4}
    assert calls == ["upload"]


# ── Refusals ────────────────────────────────────────────────────────────────
def test_a_declared_oversized_body_is_refused_before_the_route_runs(
    client: TestClient, calls: list[str]
) -> None:
    response = client.post(f"{PREFIX}/echo", json={"text": "x" * (LIMIT + 10)})
    _assert_413(response)
    assert calls == []


def test_a_streamed_body_without_a_length_is_refused(client: TestClient, calls: list[str]) -> None:
    response = client.post(f"{PREFIX}/raw", content=_chunks(LIMIT * 3))
    _assert_413(response)
    assert calls == []


def test_a_streamed_json_body_is_refused_not_reported_as_unparseable(
    client: TestClient, calls: list[str]
) -> None:
    """FastAPI turns a failed body read into a 400; the 413 must win."""
    response = client.post(
        f"{PREFIX}/echo", content=_chunks(LIMIT * 3), headers={"Content-Type": "application/json"}
    )
    _assert_413(response)
    assert calls == []


def test_a_body_longer_than_it_claimed_is_refused(calls: list[str]) -> None:
    """A lying Content-Length does not get past the streamed count."""
    import asyncio

    app = _app(calls)
    sent: list[dict] = []
    body = b"a" * (LIMIT * 3)
    messages = [
        {"type": "http.request", "body": body[:1000], "more_body": True},
        {"type": "http.request", "body": body[1000:], "more_body": False},
    ]

    async def receive() -> dict:
        return messages.pop(0) if messages else {"type": "http.disconnect"}

    async def send(message: dict) -> None:
        sent.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": f"{PREFIX}/raw",
        "raw_path": f"{PREFIX}/raw".encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [(b"content-length", b"10"), (b"host", b"test")],
        "client": ("127.0.0.1", 1),
        "server": ("test", 80),
    }
    asyncio.run(app(scope, receive, send))
    assert sent[0]["type"] == "http.response.start" and sent[0]["status"] == 413
    assert calls == []


def test_a_lookalike_path_is_not_exempt(client: TestClient, calls: list[str]) -> None:
    response = client.post(f"{PREFIX}/brand-kit/uploads-extra", content=b"a" * (LIMIT * 2))
    _assert_413(response)
    assert calls == []


@pytest.mark.parametrize("value", ["99999999999999999999999999999", "²"])
def test_odd_content_length_values_do_not_crash(value: str) -> None:
    from app.api.middleware import _content_length

    result = _content_length({"headers": [(b"content-length", value.encode("latin-1"))]})
    assert result is None or result > LIMIT


def test_the_message_is_plain_english() -> None:
    lowered = REQUEST_TOO_LARGE_MESSAGE.lower()
    for word in ("byte", "413", "payload", "content-length"):
        assert word not in lowered


# ── Wired into the real app ─────────────────────────────────────────────────
@asynccontextmanager
async def _noop_lifespan(app):  # noqa: ANN001, ANN202
    yield


def test_the_real_app_refuses_an_oversized_body_with_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.main import create_app

    monkeypatch.setattr(settings, "max_request_body_bytes", 4096)
    app = create_app()
    app.router.lifespan_context = _noop_lifespan
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post(
            f"{PREFIX}/auth/request-code", json={"email": "a" * 8000 + "@example.com"}
        )
    _assert_413(response)
    # Still wrapped by the request-id and security-header layers.
    assert response.headers["X-Request-ID"]
    assert response.headers["X-Content-Type-Options"] == "nosniff"
