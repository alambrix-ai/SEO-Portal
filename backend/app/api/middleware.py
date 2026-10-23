"""HTTP middleware: security headers, request ids, rate limiting, a
request-body size limit, and the host and https checks.

An internet-facing multi-tenant API needs these regardless of what the
endpoints do, so they live here rather than being sprinkled through routes.
"""
from __future__ import annotations

import hashlib
import re
import time
import uuid
from collections import OrderedDict, deque
from threading import Lock

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core import client_ip
from app.core.config import settings
from app.core.exceptions import AppError
from app.core.logging import bind_request_id, get_logger, reset_request_id

log = get_logger(__name__)

REQUEST_ID_HEADER = "X-Request-ID"
#: A caller-supplied request id is echoed and logged, so it must be short and
#: plain; anything else is replaced with one of ours.
_REQUEST_ID_PATTERN = re.compile(r"[A-Za-z0-9._-]{1,64}")
#: The readiness probe. Container and platform health checks call it on the
#: container's own address (Host: localhost, plain http), so it is exempt
#: from the host allow-list and the https redirect. Exact path only.
HEALTH_PATH = "/health"


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Attach a request id, bind it for logging, and log timing.

    The id is echoed to the client so a user-reported problem can be found in
    the logs without guessing, and bound to the logging context so every line
    written while serving the request carries it.
    """

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        supplied = request.headers.get(REQUEST_ID_HEADER, "")
        request_id = (
            supplied
            if _REQUEST_ID_PATTERN.fullmatch(supplied)
            else uuid.uuid4().hex[:16]
        )
        request.state.request_id = request_id

        token = bind_request_id(request_id)
        try:
            started = time.perf_counter()
            response = await call_next(request)
            duration_ms = int((time.perf_counter() - started) * 1000)
        finally:
            reset_request_id(token)

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


class HostCheckMiddleware(TrustedHostMiddleware):
    """Starlette's host allow-list, except for the health probe.

    A probe asks for ``/health`` with ``Host: localhost``, which is not a
    public hostname; refusing it would mark a working container unhealthy.
    The probe reveals nothing about the build, so the exemption is safe.
    """

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope.get("path") == HEALTH_PATH:
            await self.app(scope, receive, send)
            return
        await super().__call__(scope, receive, send)


class HttpsRedirectMiddleware(BaseHTTPMiddleware):
    """Redirect plain HTTP to HTTPS when running behind a TLS terminator.

    ``X-Forwarded-Proto`` is believed only from a trusted proxy; anything
    unclear counts as plain HTTP and is redirected. ``/health`` is answered
    as is: a probe on the container's own port speaks plain http and treats
    a redirect as success without ever reaching the app.
    """

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        if request.url.path == HEALTH_PATH:
            return await call_next(request)
        if client_ip.forwarded_proto(request) != "https":
            return RedirectResponse(
                str(request.url.replace(scheme="https")), status_code=308
            )
        return await call_next(request)


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Fixed-window rate limiting per client, per user and per workspace.

    Every budget a request counts against must have room:

    * per client address (IPv6 by /64), with a tighter auth budget;
    * per signed-in user, whatever address they come from;
    * per workspace, for the endpoints that call a model (a spend ceiling).

    In-process and therefore per-worker: it is a guard against scripted abuse,
    not a distributed quota. A multi-worker deployment should put a shared
    limiter at the edge as well.

    The counters live in a bounded LRU: every touch moves a key to the end,
    keys idle for a whole window fall off the front, and past
    :attr:`MAX_KEYS` the least recently used key is dropped. Memory and the
    work per request stay bounded however many addresses call.
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
        "/mfa/verify",
        "/mfa/webauthn/authenticate",
    )

    # POST endpoints that start model calls, relative to the API prefix. Each
    # one spends the workspace's money, so they share one per-workspace budget.
    LLM_PATTERNS = tuple(
        re.compile(pattern)
        for pattern in (
            r"/creative/jobs/?",
            r"/approvals/[^/]+/request-changes/?",
            r"/assistant/.+",
            r"/brand-profile/generate/?",
            r"/analyses/?",
            r"/social/schedule(/.*)?",
            r"/ads/creatives/generate/?",
        )
    )

    #: The public, unauthenticated CSP violation endpoint (relative to the API
    #: prefix). Browsers post to it on their own, so it gets a strict per-
    #: address budget of its own on top of the general one.
    CSP_REPORT_PATTERN = re.compile(r"/csp-report/?")
    CSP_REPORT_PER_MINUTE = 30

    WINDOW_SECONDS = 60
    #: Most budgets tracked at once. Dropping a live key only forgets its
    #: count, so this is sized well above normal traffic.
    MAX_KEYS = 50_000
    #: Expired keys removed per request, so cleanup cost stays constant.
    _EVICT_PER_CALL = 64

    def __init__(self, app, max_keys: int | None = None) -> None:  # noqa: ANN001
        super().__init__(app)
        self._hits: OrderedDict[str, deque[float]] = OrderedDict()
        self._lock = Lock()
        self._max_keys = max_keys or self.MAX_KEYS

    def _evict(self, now: float) -> None:
        """Drop idle keys from the LRU end, then any over the cap. Caller
        holds the lock."""
        for _ in range(self._EVICT_PER_CALL):
            if not self._hits:
                break
            key, hits = next(iter(self._hits.items()))
            if hits and now - hits[-1] <= self.WINDOW_SECONDS:
                break
            del self._hits[key]
        while len(self._hits) > self._max_keys:
            self._hits.popitem(last=False)

    def _limit_for(self, path: str) -> int:
        if any(prefix in path for prefix in self.AUTH_PREFIXES):
            return settings.auth_rate_limit_per_minute
        return settings.api_rate_limit_per_minute

    def _client_key(self, request: Request) -> str:
        return client_ip.rate_limit_key(client_ip.resolve(request))

    @classmethod
    def _is_llm_call(cls, method: str, path: str) -> bool:
        if method != "POST":
            return False
        prefix = settings.api_v1_prefix.rstrip("/")
        if prefix and not path.startswith(prefix + "/"):
            return False
        relative = path[len(prefix) :]
        return any(pattern.fullmatch(relative) for pattern in cls.LLM_PATTERNS)

    @classmethod
    def _is_csp_report(cls, method: str, path: str) -> bool:
        if method != "POST":
            return False
        prefix = settings.api_v1_prefix.rstrip("/")
        if prefix and not path.startswith(prefix + "/"):
            return False
        return cls.CSP_REPORT_PATTERN.fullmatch(path[len(prefix) :]) is not None

    @staticmethod
    def _identity(request: Request) -> tuple[str, str] | None:
        """``(user_id, tenant_id)`` from a validly signed bearer token.

        Only the signature and expiry are checked here; whether the session
        is still live is the auth dependency's job. A token that does not
        verify gets no per-user bucket: authentication refuses the request
        anyway, and the per-address budget still applies to it.
        """
        header = request.headers.get("authorization", "")
        scheme, _, token = header.partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            return None
        from app.core.security import decode_access_token

        try:
            claims = decode_access_token(token.strip())
        except AppError:
            return None
        user_id, tenant_id = claims.get("sub"), claims.get("tid")
        if not isinstance(user_id, str) or not isinstance(tenant_id, str):
            return None
        return user_id, tenant_id

    def _buckets(self, request: Request) -> list[tuple[str, str, int]]:
        """``(kind, key, limit)`` for every budget this request counts against."""
        path = request.url.path
        limit = self._limit_for(path)
        # Auth paths get their own bucket, so ordinary API traffic cannot
        # exhaust the (much smaller) login budget or vice versa.
        kind = "auth" if limit == settings.auth_rate_limit_per_minute else "api"
        client = self._client_key(request)
        buckets = [(kind, f"{kind}:{client}", limit)]
        if self._is_csp_report(request.method, path):
            buckets.append(("csp_report", f"csp:{client}", self.CSP_REPORT_PER_MINUTE))

        identity = self._identity(request)
        if identity is not None:
            user_id, tenant_id = identity
            buckets.append(
                ("user", f"user:{user_id}", settings.user_rate_limit_per_minute)
            )
            if self._is_llm_call(request.method, path):
                buckets.append(
                    (
                        "tenant_llm",
                        f"llm:{tenant_id}",
                        settings.llm_rate_limit_per_tenant_per_minute,
                    )
                )
        return buckets

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        if not settings.rate_limit_enabled or request.method == "OPTIONS":
            return await call_next(request)

        buckets = self._buckets(request)
        now = time.monotonic()

        with self._lock:
            # Check every budget before charging any, so a refused request
            # does not use up the budgets it was not refused by.
            for kind, key, limit in buckets:
                hits = self._hits.get(key)
                if hits is None:
                    continue
                while hits and now - hits[0] > self.WINDOW_SECONDS:
                    hits.popleft()
                if len(hits) >= limit:
                    retry_after = max(1, int(self.WINDOW_SECONDS - (now - hits[0])))
                    # The key holds an address or a user id: log a hash of it.
                    log.warning(
                        "Rate limit hit (%s bucket %s) on %s",
                        kind,
                        hashlib.sha256(key.encode("utf-8")).hexdigest()[:12],
                        request.url.path,
                    )
                    return _too_many_requests(kind, retry_after)
            for _, key, _ in buckets:
                hits = self._hits.get(key)
                if hits is None:
                    hits = self._hits[key] = deque()
                else:
                    self._hits.move_to_end(key)
                hits.append(now)
            self._evict(now)

        return await call_next(request)


_RATE_LIMIT_MESSAGES: dict[str, str] = {
    "tenant_llm": (
        "Your workspace has started a lot of AI tasks in the last minute. "
        "Please wait {seconds} seconds and try again."
    ),
}
_RATE_LIMIT_DEFAULT = (
    "You're going a little fast. Please wait {seconds} seconds and try again."
)


def _too_many_requests(kind: str, retry_after: int) -> JSONResponse:
    """A plain-English 429 that says how long to wait."""
    message = _RATE_LIMIT_MESSAGES.get(kind, _RATE_LIMIT_DEFAULT)
    return JSONResponse(
        status_code=429,
        content={
            "code": "rate_limited",
            "detail": message.format(seconds=retry_after),
            "fields": {},
        },
        headers={"Retry-After": str(retry_after)},
    )


# ── Request body size ──────────────────────────────────────────────────────
#: Routes that take files and enforce their own, larger cap (relative to the
#: API prefix). Exact paths only, so nothing else can borrow the exemption.
BODY_LIMIT_EXEMPT_PATHS: tuple[str, ...] = ("/brand-kit/uploads",)

REQUEST_TOO_LARGE_MESSAGE = (
    "That's more information than we can accept in one go. "
    "Please make it shorter and try again."
)


class _BodyTooLarge(Exception):
    """Raised from ``receive`` once a streamed body passes the limit."""


class BodySizeLimitMiddleware:
    """Refuse request bodies over ``MAX_REQUEST_BODY_BYTES`` with a 413.

    The declared ``Content-Length`` is checked before anything is read, and
    the bytes actually received are counted too, so a chunked body or one
    longer than it claimed is stopped as soon as it passes the limit.
    Upload routes (:data:`BODY_LIMIT_EXEMPT_PATHS`) have their own caps.

    Plain ASGI rather than ``BaseHTTPMiddleware``: it has to sit between the
    server and the app's reads of the body.
    """

    def __init__(self, app: ASGIApp, max_bytes: int | None = None) -> None:
        self.app = app
        self._max_bytes = max_bytes

    @property
    def max_bytes(self) -> int:
        return self._max_bytes or settings.max_request_body_bytes

    @staticmethod
    def _exempt(path: str) -> bool:
        prefix = settings.api_v1_prefix.rstrip("/")
        if prefix and not path.startswith(prefix + "/"):
            return False
        relative = path[len(prefix) :].rstrip("/")
        return relative in BODY_LIMIT_EXEMPT_PATHS

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or self._exempt(scope.get("path", "")):
            await self.app(scope, receive, send)
            return

        limit = self.max_bytes
        declared = _content_length(scope)
        if declared is not None and declared > limit:
            _log_refusal(scope, "declared")
            await _request_too_large(scope, receive, send)
            return

        received = 0
        too_large = False
        started = False

        async def limited_receive() -> Message:
            nonlocal received, too_large
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    too_large = True
                    raise _BodyTooLarge()
            return message

        async def guarded_send(message: Message) -> None:
            nonlocal started
            if too_large:
                # Whatever the app made of the interrupted read (often a 400
                # "could not parse"): ours is the true answer, sent below.
                return
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, guarded_send)
        except Exception:
            # The app may let our exception (or one it wrapped it in) escape;
            # that is the refusal below, anything else is a real failure.
            if not too_large:
                raise
        if too_large:
            _log_refusal(scope, "streamed")
            if not started:
                await _request_too_large(scope, receive, send)


def _content_length(scope: Scope) -> int | None:
    """The declared body size, or None when absent or unreadable (the
    streamed count still applies then)."""
    for name, value in scope.get("headers") or ():
        if name.lower() == b"content-length":
            text = value.decode("latin-1").strip()
            if not (text.isascii() and text.isdigit()):
                return None
            # Absurdly long numbers are simply too large (and int() refuses
            # very long digit strings).
            return int(text) if len(text) <= 18 else 10**18
    return None


def _log_refusal(scope: Scope, how: str) -> None:
    # Path and method only: never the body.
    log.warning(
        "Request body over the limit (%s) on %s %s",
        how,
        scope.get("method", ""),
        scope.get("path", ""),
    )


async def _request_too_large(scope: Scope, receive: Receive, send: Send) -> None:
    response = JSONResponse(
        status_code=413,
        content={
            "code": "request_too_large",
            "detail": REQUEST_TOO_LARGE_MESSAGE,
            "fields": {},
        },
        # The rest of the body is not read, so the connection is not reused.
        headers={"Connection": "close"},
    )
    await response(scope, receive, send)
