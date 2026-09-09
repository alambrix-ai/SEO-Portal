"""HTTP middleware: security headers, request ids, and rate limiting.

An internet-facing multi-tenant API needs these regardless of what the
endpoints do, so they live here rather than being sprinkled through routes.
"""
from __future__ import annotations

import time
import uuid
from collections import defaultdict, deque
from threading import Lock

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app.core.config import settings
from app.core.logging import get_logger

log = get_logger(__name__)

REQUEST_ID_HEADER = "X-Request-ID"


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Attach a request id and log timing.

    The id is echoed to the client so a user-reported problem can be found in
    the logs without guessing.
    """

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        request_id = request.headers.get(REQUEST_ID_HEADER) or uuid.uuid4().hex[:16]
        request.state.request_id = request_id

        started = time.perf_counter()
        response = await call_next(request)
        duration_ms = int((time.perf_counter() - started) * 1000)

        response.headers[REQUEST_ID_HEADER] = request_id
        response.headers["Server-Timing"] = f"app;dur={duration_ms}"

        # Only the slow or failing requests, so the log stays readable.
        if duration_ms > 1000 or response.status_code >= 500:
            log.warning(
                "%s %s -> %d in %dms (req %s)",
                request.method,
                request.url.path,
                response.status_code,
                duration_ms,
                request_id,
            )
        return response


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Headers that harden every response.

    HSTS is only sent when TLS is actually in front (``FORCE_HTTPS``), because
    sending it over plain HTTP in development would pin a browser to https for
    localhost and be a nuisance to undo.
    """

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault(
            "Permissions-Policy", "geolocation=(), camera=(), microphone=()"
        )
        # This is a JSON API: nothing it returns should ever be executed or
        # framed, so the policy is as tight as it can be.
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'none'; frame-ancestors 'none'; base-uri 'none'",
        )
        response.headers.setdefault("Cache-Control", "no-store")

        if settings.force_https:
            response.headers.setdefault(
                "Strict-Transport-Security",
                f"max-age={settings.hsts_max_age_seconds}; includeSubDomains; preload",
            )
        return response


class HttpsRedirectMiddleware(BaseHTTPMiddleware):
    """Redirect plain HTTP to HTTPS when running behind a TLS terminator."""

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        forwarded = request.headers.get("x-forwarded-proto", "")
        scheme = forwarded.split(",")[0].strip() or request.url.scheme
        if scheme == "http":
            from starlette.responses import RedirectResponse

            return RedirectResponse(
                str(request.url.replace(scheme="https")), status_code=308
            )
        return await call_next(request)


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Fixed-window rate limiting per client, with a tighter auth budget.

    In-process and therefore per-worker: it is a guard against scripted abuse
    of the sign-in endpoints, not a distributed quota. A multi-worker
    deployment should put a shared limiter at the edge as well.
    """

    # Paths where codes are guessed, addresses are enumerated, or a mailbox
    # could be flooded. The two request-code endpoints belong here as much as
    # the redeeming ones: they are the ones that send mail on demand.
    AUTH_PREFIXES = (
        "/auth/request-code",
        "/auth/request-signup-code",
        "/auth/login",
        "/auth/register",
        "/auth/refresh",
        "/auth/accept-invitation",
    )

    def __init__(self, app) -> None:  # noqa: ANN001
        super().__init__(app)
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = Lock()

    def _limit_for(self, path: str) -> int:
        if any(prefix in path for prefix in self.AUTH_PREFIXES):
            return settings.auth_rate_limit_per_minute
        return settings.api_rate_limit_per_minute

    def _client_key(self, request: Request) -> str:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            return forwarded.split(",")[0].strip()
        return request.client.host if request.client else "unknown"

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        if not settings.rate_limit_enabled or request.method == "OPTIONS":
            return await call_next(request)

        path = request.url.path
        limit = self._limit_for(path)
        # Auth paths get their own bucket, so ordinary API traffic cannot
        # exhaust the (much smaller) login budget or vice versa.
        bucket = "auth" if limit == settings.auth_rate_limit_per_minute else "api"
        key = f"{bucket}:{self._client_key(request)}"
        now = time.monotonic()

        with self._lock:
            hits = self._hits[key]
            while hits and now - hits[0] > 60:
                hits.popleft()
            if len(hits) >= limit:
                retry_after = max(1, int(60 - (now - hits[0])))
                log.warning("Rate limit hit for %s on %s", key, path)
                return JSONResponse(
                    status_code=429,
                    content={
                        "code": "rate_limited",
                        "detail": "Too many requests — slow down and try again shortly",
                        "fields": {},
                    },
                    headers={"Retry-After": str(retry_after)},
                )
            hits.append(now)

            # Keep the dictionary from growing without bound across many IPs.
            if len(self._hits) > 10_000:
                stale = [k for k, v in self._hits.items() if not v or now - v[-1] > 300]
                for k in stale:
                    del self._hits[k]

        return await call_next(request)
