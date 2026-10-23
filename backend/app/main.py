"""FastAPI application factory and entry point.

Run with::

    uvicorn app.main:app --reload

Startup verifies the database is reachable over TLS and starts the agent
scheduler; shutdown stops it cleanly so an in-flight agent run is not killed
mid-transaction.
"""
from __future__ import annotations

import logging
import typing
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic_core import core_schema

from app.api.middleware import (
    BodySizeLimitMiddleware,
    HostCheckMiddleware,
    HttpsRedirectMiddleware,
    IpAllowlistMiddleware,
    RateLimitMiddleware,
    RequestContextMiddleware,
    SecurityHeadersMiddleware,
)
from app.api.v1.router import api_router
from app.core.config import settings
from app.core.crypto import CryptoError
from app.core.exceptions import AppError
from app.core.logging import configure_logging, get_logger, log_failure
from app.db.base import utcnow
from app.schemas.common import HealthStatus

log = get_logger(__name__)

VERSION = "1.0.0"


def _is_missing_schema(exc: BaseException) -> bool:
    """True when Postgres/SQLAlchemy says a table or column is absent.

    Typical on a deployment whose image includes new routers but whose
    database has not yet run Alembic heads 0040+.
    """
    try:
        from sqlalchemy.exc import ProgrammingError
    except ImportError:  # pragma: no cover - SQLAlchemy is a hard dependency
        ProgrammingError = ()  # type: ignore[misc, assignment]

    cur: BaseException | None = exc
    seen = 0
    while cur is not None and seen < 6:
        if ProgrammingError and isinstance(cur, ProgrammingError):
            return True
        text = str(cur).lower()
        if (
            "undefinedtable" in text
            or "undefinedcolumn" in text
            or "does not exist" in text
            and ("relation" in text or "column" in text)
        ):
            return True
        # psycopg / asyncpg error codes for undefined_table / undefined_column
        code = getattr(cur, "sqlstate", None) or getattr(cur, "pgcode", None)
        if code in {"42P01", "42703"}:
            return True
        cur = cur.__cause__ or cur.__context__
        seen += 1
    return False

# ── Validation messages ────────────────────────────────────────────────────
#: Pydantic's own error types. A type outside this set was raised by one of
#: our validators (PydanticCustomError) with a message written for users.
_PYDANTIC_ERROR_TYPES = frozenset(typing.get_args(core_schema.ErrorType))
_VALIDATION_FALLBACK = "Please check this value."
_MAX_FIELD_MESSAGE = 200
#: Plain-English text for the pydantic errors people actually hit. ``{n}``
#: is filled from the error's numeric limit, when it has one.
_VALIDATION_MESSAGES: dict[str, str] = {
    "missing": "Please fill this in.",
    "string_too_short": "Please enter at least {n} characters.",
    "string_too_long": "Please keep this to {n} characters or fewer.",
    "too_short": "Please add at least {n}.",
    "too_long": "Please keep this to {n} or fewer.",
    "string_type": "Please enter text.",
    "string_pattern_mismatch": "This doesn't look right. Please check the format.",
    "int_parsing": "Please enter a whole number.",
    "int_type": "Please enter a whole number.",
    "int_from_float": "Please enter a whole number.",
    "float_parsing": "Please enter a number.",
    "float_type": "Please enter a number.",
    "decimal_parsing": "Please enter a number.",
    "bool_parsing": "Please choose yes or no.",
    "bool_type": "Please choose yes or no.",
    "greater_than": "Please enter a number above {n}.",
    "greater_than_equal": "Please enter {n} or more.",
    "less_than": "Please enter a number below {n}.",
    "less_than_equal": "Please enter {n} or less.",
    "literal_error": "Please choose one of the options offered.",
    "enum": "Please choose one of the options offered.",
    "url_parsing": "Please enter a valid web address.",
    "url_scheme": "Please enter a web address starting with https://",
    "url_type": "Please enter a valid web address.",
    "date_parsing": "Please enter a valid date.",
    "date_from_datetime_parsing": "Please enter a valid date.",
    "datetime_parsing": "Please enter a valid date and time.",
    "datetime_from_date_parsing": "Please enter a valid date and time.",
    "uuid_parsing": "That reference isn't valid.",
    "list_type": "Please provide a list.",
    "dict_type": "That isn't in the expected shape.",
    "model_type": "That isn't in the expected shape.",
    "model_attributes_type": "That isn't in the expected shape.",
    "json_invalid": "We couldn't read that request. Please try again.",
    "json_type": "We couldn't read that request. Please try again.",
    "extra_forbidden": "This field isn't accepted here.",
}
_LIMIT_KEYS = ("min_length", "max_length", "gt", "ge", "lt", "le")


def plain_validation_message(error: dict) -> str:
    """A short, plain-English message for one pydantic error.

    Never echoes the submitted value. Messages from our own validators
    (``ValueError`` and custom error types) were written for users and are
    kept, minus pydantic's "Value error, " prefix.
    """
    kind = str(error.get("type", ""))
    template = _VALIDATION_MESSAGES.get(kind)
    if template is not None:
        if "{n}" not in template:
            return template
        context = error.get("ctx") or {}
        limit = next(
            (
                context[key]
                for key in _LIMIT_KEYS
                if isinstance(context.get(key), (int, float))
                and not isinstance(context.get(key), bool)
            ),
            None,
        )
        if limit is None:
            return _VALIDATION_FALLBACK
        return template.format(n=f"{limit:g}" if isinstance(limit, float) else limit)
    if kind in ("value_error", "assertion_error") or kind not in _PYDANTIC_ERROR_TYPES:
        message = str(error.get("msg", "")).strip()
        for prefix in ("Value error, ", "Assertion failed, "):
            if message.startswith(prefix):
                message = message[len(prefix) :].strip()
        if message:
            return message[:_MAX_FIELD_MESSAGE]
    return _VALIDATION_FALLBACK

DESCRIPTION = """
Autonomous SEO, AEO and programmatic ads for enterprise teams.

Eleven agents run the pipeline end to end — crawling a CMS, closing semantic
gaps, injecting answer-engine Q&A and JSON-LD, hunting placements, pitching
publishers, generating creative, reallocating spend, and blocking invalid
traffic. Every action either executes autonomously or queues for human review,
decided by one guardrail per workspace.

Multi-tenant by construction: each organisation's data is scoped by row-level
security and encrypted under its own key.
"""


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    configure_logging()
    log.info(
        "Starting %s %s (%s)", settings.app_name, VERSION, settings.app_env
    )

    # ── Database ───────────────────────────────────────────────────────────
    from app.db.session import check_connection

    try:
        check_connection()
    except Exception as exc:  # noqa: BLE001
        # Fail loudly: without Postgres nothing in this service works, and a
        # half-up API returning 500s is worse than one that refuses to start.
        log_failure(log, "Database unavailable", exc, level=logging.ERROR)
        raise

    # ── Encryption ─────────────────────────────────────────────────────────
    # Build the key ring now rather than on the first request, so a bad key is
    # a startup failure instead of a runtime surprise.
    from app.core.crypto import keyring

    try:
        ring = keyring()
        log.info("Encryption ready (master key v%d)", ring.active_version)
    except CryptoError as exc:
        log_failure(log, "Encryption misconfigured", exc, level=logging.ERROR)
        raise

    # ── Registries ─────────────────────────────────────────────────────────
    from app.agents.base.registry import all_agents
    from app.connectors.base.registry import all_specs

    agents = all_agents()
    connectors = all_specs()

    # ── Scheduler ──────────────────────────────────────────────────────────
    from app.orchestration.scheduler import scheduler

    if settings.scheduler_enabled and not settings.celery_enabled:
        scheduler.start()
    elif settings.celery_enabled:
        log.info("Celery enabled — agent runs are dispatched to workers")
    else:
        log.info("Scheduler disabled; agents run only when triggered manually")

    log.info(
        "Ready: %d agents, %d connectors (LLM from connected models per agent)",
        len(agents),
        len(connectors),
    )

    yield

    await scheduler.stop()
    log.info("Shutdown complete")


def create_app() -> FastAPI:
    # Sign-in callbacks carry one-time codes in their URL; keep them out of
    # the access log (the server may have reconfigured its loggers by now).
    from app.core.logging import redact_callback_queries

    redact_callback_queries()
    app = FastAPI(
        title=settings.app_name,
        description=DESCRIPTION,
        version=VERSION,
        lifespan=lifespan,
        docs_url="/docs" if not settings.is_production else None,
        redoc_url=None,
        openapi_url="/openapi.json" if not settings.is_production else None,
    )

    # ── Middleware ─────────────────────────────────────────────────────────
    # Starlette runs the LAST one added first, so they are added innermost
    # first. A request passes, outermost to innermost:
    #   host check -> https redirect -> CORS -> request id
    #   -> security headers -> rate limit -> body size -> the route
    # The host check and redirect come first, so nothing else runs for a
    # request that is refused or sent elsewhere. A 429 or 413 still gets a
    # request id, the security headers and CORS.
    # Innermost: tenant IP allowlist (needs bearer identity + DB).
    app.add_middleware(IpAllowlistMiddleware)
    app.add_middleware(BodySizeLimitMiddleware)
    app.add_middleware(RateLimitMiddleware)
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(RequestContextMiddleware)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(dict.fromkeys([*settings.cors_origins, *settings.demo_allowed_origins])),
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-Request-ID", "X-Device-Id", "X-Analysis-Token"],
        # X-Next-Cursor pages the audit export; Content-Disposition names
        # downloaded files.
        expose_headers=["X-Request-ID", "X-Next-Cursor", "Content-Disposition"],
        max_age=600,
    )

    # Behind a platform edge that terminates TLS (EDGE_TLS_TERMINATED) the
    # app never sees https, so redirecting would loop; the edge redirects.
    if settings.force_https and not settings.edge_tls_terminated:
        app.add_middleware(HttpsRedirectMiddleware)
    if settings.trusted_hosts and "*" not in settings.trusted_hosts:
        app.add_middleware(HostCheckMiddleware, allowed_hosts=settings.trusted_hosts)

    # ── Error handling ─────────────────────────────────────────────────────
    @app.exception_handler(AppError)
    async def handle_app_error(request: Request, exc: AppError) -> JSONResponse:
        """Domain errors carry their own status and user-facing message.

        Only connector, assistant and model errors are softened — they can
        echo a vendor's reply. Every other domain message was written for the
        user ("Workspace not found", "View-only access for your role") and is
        shown as written.
        """
        from app.core.user_messages import public_error_message

        detail = public_error_message(exc)
        if detail != exc.message.strip():
            log.info(
                "Replaced %s message on %s (code %s)",
                type(exc).__name__,
                request.url.path,
                exc.code,
            )
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "code": exc.code,
                "detail": detail,
                "fields": exc.details,
            },
        )

    @app.exception_handler(CryptoError)
    async def handle_crypto_error(request: Request, exc: CryptoError) -> JSONResponse:
        # Never surface key details to a client; the log has what is needed.
        log_failure(
            log, "Crypto failure", exc, level=logging.ERROR, path=request.url.path
        )
        return JSONResponse(
            status_code=500,
            content={
                "code": "encryption_error",
                "detail": "Could not read encrypted data for this workspace",
                "fields": {},
            },
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        """Flatten pydantic's errors into ``field -> plain-English message``."""
        fields: dict[str, str] = {}
        for error in exc.errors():
            location = [str(part) for part in error.get("loc", ()) if part != "body"]
            fields[".".join(location) or "body"] = plain_validation_message(error)
        return JSONResponse(
            status_code=422,
            content={
                "code": "validation_error",
                "detail": "Check the highlighted fields",
                "fields": fields,
            },
        )

    @app.exception_handler(Exception)
    async def handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        request_id = getattr(request.state, "request_id", "-")
        # Missing tables/columns (migrations not applied) → clear 503 so the
        # console can show "feature not configured" instead of a white screen
        # chasing a generic 500.
        if _is_missing_schema(exc):
            log_failure(
                log,
                "Feature schema missing",
                exc,
                level=logging.ERROR,
                path=request.url.path,
                request_id=request_id,
            )
            return JSONResponse(
                status_code=503,
                content={
                    "code": "feature_not_configured",
                    "detail": "This feature is not configured yet",
                    "fields": {"request_id": request_id},
                },
            )
        # No traceback or raw message: either can carry tokens, SQL values
        # or personal data. The request id and hash correlate repeats.
        log_failure(
            log,
            "Unhandled error",
            exc,
            level=logging.ERROR,
            path=request.url.path,
            request_id=request_id,
        )
        return JSONResponse(
            status_code=500,
            content={
                "code": "internal_error",
                "detail": "Something went wrong on our side",
                "fields": {"request_id": request_id},
            },
        )

    # ── Routes ─────────────────────────────────────────────────────────────
    app.include_router(api_router, prefix=settings.api_v1_prefix)

    @app.get("/health", response_model=HealthStatus, tags=["meta"])
    def health(response: Response) -> HealthStatus:
        """Readiness probe: 503 when a dependency is down.

        Public and unauthenticated, so it says nothing about the build — no
        app name, environment, version or model. ``renderer`` is
        ``degraded`` when Chromium is configured but fell back to Pillow;
        production reports that as 503, since designs would come out wrong.
        """
        from sqlalchemy import text

        from app.agents.base.registry import all_agents
        from app.connectors.base.registry import all_specs
        from app.db.session import engine
        from app.orchestration.scheduler import scheduler
        from app.services.creative.render import renderer_status

        database = "up"
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
        except Exception as exc:  # noqa: BLE001 - reported, not raised
            log_failure(log, "Health check: database unreachable", exc)
            database = "down"

        renderer = _renderer_health(renderer_status())
        degraded = database != "up" or (
            settings.is_production and renderer == "degraded"
        )
        if degraded:
            response.status_code = 503
        return HealthStatus(
            status="degraded" if degraded else "ok",
            database=database,
            renderer=renderer,
            agents_registered=len(all_agents()),
            connectors_registered=len(all_specs()),
            scheduler=(
                "celery"
                if settings.celery_enabled
                else "running"
                if scheduler.running
                else "stopped"
            ),
            checked_at=utcnow(),
        )

    @app.get("/", tags=["meta"], include_in_schema=False)
    def root() -> dict:
        # Public and unauthenticated, so no version: it only helps an
        # attacker match the build to known issues.
        return {
            "app": settings.app_name,
            "api": settings.api_v1_prefix,
            "docs": "/docs" if not settings.is_production else None,
        }

    return app


def _renderer_health(status: str) -> str:
    """``status``, unless it is a stale ``degraded``.

    The render module sets its degraded flag when Chromium fails, and only
    clears it when a later launch goes through its own start path. If the
    browser has come back by another route (a new browser thread launched
    for the next render), the flag would keep /health at 503 for good. So a
    degraded report is re-checked here: Chromium launched and not recently
    failed means it has recovered, and the flag is cleared.
    """
    if status != "degraded":
        return status
    from app.services.creative import render

    chromium = getattr(render, "_chromium", None)
    if chromium is not None and chromium.launched and not chromium.recently_failed:
        render._degraded = False  # noqa: SLF001 - recovered; see docstring
        log.info("Creative renderer: Chromium is back, clearing the degraded flag")
        return render.renderer_status()
    return status


app = create_app()
