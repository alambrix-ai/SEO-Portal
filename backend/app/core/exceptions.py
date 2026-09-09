"""Domain errors mapped to HTTP responses by the handlers in ``app.main``."""
from __future__ import annotations


class AppError(Exception):
    """Base class for errors the API knows how to render."""

    status_code = 400
    code = "app_error"

    def __init__(self, message: str, *, details: dict | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}


class NotFoundError(AppError):
    status_code = 404
    code = "not_found"


class ConflictError(AppError):
    status_code = 409
    code = "conflict"


class InvalidInputError(AppError):
    status_code = 422
    code = "validation_error"


class AuthError(AppError):
    status_code = 401
    code = "unauthenticated"


class ForbiddenError(AppError):
    """The caller's role lacks the access this action needs.

    The message carries what the console shows as a toast, e.g.
    "View-only access for your role".
    """

    status_code = 403
    code = "forbidden"


class ConnectorError(AppError):
    status_code = 502
    code = "connector_error"


class ConnectorConfigError(ConnectorError):
    """The integration is set up wrong, so retrying cannot help.

    Separated from :class:`ConnectorError` because the two want opposite
    handling. A transient failure of one item — one query, one page, one
    channel — is logged and skipped so the rest of the batch survives. A
    missing credential fails every item identically, and swallowing it five
    times turns a misconfiguration into a report of "0 citations found",
    which reads as a finding rather than a fault.

    So the per-item handlers catch ConnectorError and deliberately let this
    through.
    """

    status_code = 422
    code = "connector_misconfigured"


class AgentError(AppError):
    status_code = 500
    code = "agent_error"
