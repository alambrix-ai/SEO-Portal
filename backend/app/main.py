"""FastAPI application factory and entry point.

Run with::

    uvicorn app.main:app --reload

Startup verifies the database is reachable over TLS and starts the agent
scheduler; shutdown stops it cleanly so an in-flight agent run is not killed
mid-transaction.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from collections.abc import AsyncIterator

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.api.middleware import (
    HttpsRedirectMiddleware,
    RateLimitMiddleware,
    RequestContextMiddleware,
    SecurityHeadersMiddleware,
)
from app.api.v1.router import api_router
from app.core.config import settings
from app.core.crypto import CryptoError
from app.core.exceptions import AppError
from app.core.logging import configure_logging, get_logger
from app.db.base import utcnow
from app.schemas.common import HealthStatus

log = get_logger(__name__)

VERSION = "1.0.0"

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
        log.error("Database unavailable: %s", exc)
        raise

    # ── Encryption ─────────────────────────────────────────────────────────
    # Build the key ring now rather than on the first request, so a bad key is
    # a startup failure instead of a runtime surprise.
    from app.core.crypto import keyring

    try:
        ring = keyring()
        log.info("Encryption ready (master key v%d)", ring.active_version)
    except CryptoError as exc:
        log.error("Encryption misconfigured: %s", exc)
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
    app = FastAPI(
        title=settings.app_name,
        description=DESCRIPTION,
        version=VERSION,
        lifespan=lifespan,
        docs_url="/docs" if not settings.is_production else None,
        redoc_url=None,
        openapi_url="/openapi.json" if not settings.is_production else None,
    )

    # ── Middleware (outermost first) ───────────────────────────────────────
    if settings.trusted_hosts and "*" not in settings.trusted_hosts:
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.trusted_hosts)
    if settings.force_https:
        app.add_middleware(HttpsRedirectMiddleware)

    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(RateLimitMiddleware)
    app.add_middleware(RequestContextMiddleware)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-Request-ID"],
        expose_headers=["X-Request-ID"],
        max_age=600,
    )

    # ── Error handling ─────────────────────────────────────────────────────
    @app.exception_handler(AppError)
    async def handle_app_error(request: Request, exc: AppError) -> JSONResponse:
        """Domain errors carry their own status and user-facing message."""
        from app.core.user_messages import soften_technical_message

        return JSONResponse(
            status_code=exc.status_code,
            content={
                "code": exc.code,
                "detail": soften_technical_message(exc.message),
                "fields": exc.details,
            },
        )

    @app.exception_handler(CryptoError)
    async def handle_crypto_error(request: Request, exc: CryptoError) -> JSONResponse:
        # Never surface key details to a client; the log has what is needed.
        log.error("Crypto failure on %s: %s", request.url.path, exc)
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
        """Flatten pydantic's errors into ``field -> message``."""
        fields: dict[str, str] = {}
        for error in exc.errors():
            location = [str(part) for part in error.get("loc", ()) if part != "body"]
            fields[".".join(location) or "body"] = error.get("msg", "Invalid value")
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
        log.exception("Unhandled error on %s (req %s)", request.url.path, request_id)
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
    def health() -> HealthStatus:
        """Liveness and configuration probe."""
        from sqlalchemy import text

        from app.agents.base.registry import all_agents
        from app.connectors.base.registry import all_specs
        from app.db.session import engine
        from app.orchestration.scheduler import scheduler

        database = "up"
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
        except Exception:  # noqa: BLE001 - reported, not raised
            database = "down"

        return HealthStatus(
            status="ok" if database == "up" else "degraded",
            app=settings.app_name,
            environment=settings.app_env,
            version=VERSION,
            database=database,
            agents_registered=len(all_agents()),
            connectors_registered=len(all_specs()),
            model="per-agent connector",
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
        return {
            "app": settings.app_name,
            "version": VERSION,
            "api": settings.api_v1_prefix,
            "docs": "/docs" if not settings.is_production else None,
        }

    return app


app = create_app()
