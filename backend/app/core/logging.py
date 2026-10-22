"""Single logging entry point so agent runs are traceable in one stream."""
from __future__ import annotations

import hashlib
import logging
import sys
from contextvars import ContextVar, Token

from app.core.config import settings

_CONFIGURED = False
_FORMAT = "%(asctime)s | %(levelname)-7s | %(name)-38s | req=%(request_id)s | %(message)s"

#: The id of the HTTP request being served, or ``"-"`` outside one. Echoed to
#: the client as ``X-Request-ID`` so a reported problem can be found in logs.
request_id_var: ContextVar[str] = ContextVar("request_id", default="-")


def bind_request_id(request_id: str) -> Token[str]:
    """Set the current request id; pass the token to :func:`reset_request_id`."""
    return request_id_var.set(request_id or "-")


def reset_request_id(token: Token[str]) -> None:
    request_id_var.reset(token)


def current_request_id() -> str:
    return request_id_var.get()


class _RequestIdFilter(logging.Filter):
    """Stamp every record with the current request id."""

    def filter(self, record: logging.LogRecord) -> bool:
        if not getattr(record, "request_id", None):
            record.request_id = request_id_var.get()
        return True


class _RedactCallbackQuery(logging.Filter):
    """Strip the query string from sign-in callback lines in the access log.

    The callback URL carries a one-time authorization code — and, for
    WordPress, the application password itself — in its query string, and
    the server's access log would otherwise write it out verbatim.
    """

    MARKER = "/oauth/callback/"

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and args:
            cleaned = tuple(
                arg.split("?", 1)[0] + "?[redacted]"
                if isinstance(arg, str) and self.MARKER in arg and "?" in arg
                else arg
                for arg in args
            )
            record.args = cleaned
        elif isinstance(record.msg, str) and self.MARKER in record.msg and "?" in record.msg:
            record.msg = record.msg.split("?", 1)[0] + "?[redacted]"
        return True


def redact_callback_queries() -> None:
    """Install the redaction on the server's access logger. Idempotent."""
    access = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, _RedactCallbackQuery) for f in access.filters):
        access.addFilter(_RedactCallbackQuery())


def configure_logging() -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return
    # Log lines carry currency symbols, em-dashes and customer names. On a
    # console with a legacy code page that is an encode error raised inside a
    # log call — the least useful place for a program to fail. Unmappable
    # characters are replaced instead.
    stream = sys.stdout
    if hasattr(stream, "reconfigure"):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):  # pragma: no cover - detached stdout
            pass
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter(_FORMAT, datefmt="%H:%M:%S"))
    handler.addFilter(_RequestIdFilter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(settings.log_level.upper())
    # httpx logs every connector request at INFO; that is too chatty here.
    logging.getLogger("httpx").setLevel("WARNING")
    redact_callback_queries()
    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    configure_logging()
    return logging.getLogger(name)


def log_failure(
    logger: logging.Logger,
    event: str,
    exc: BaseException,
    *,
    level: int = logging.WARNING,
    **ids: object,
) -> None:
    """Log a caught failure without its raw text.

    Writes ``event``, the exception type, a redacted and length-capped form
    of its message, a short hash of the raw message (to correlate repeats
    without storing them) and any ``ids`` (keyword ids and counts only —
    never prompts, briefs, captions, keys or personal data). No traceback:
    its last line is the raw message.
    """
    from app.core.user_messages import bounded_redacted

    raw = str(exc)
    digest = hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()[:12]
    context = " ".join(f"{key}={bounded_redacted(value, limit=80)}" for key, value in ids.items())
    logger.log(
        level,
        "%s: %s (%s) [h=%s]%s",
        event,
        type(exc).__name__,
        bounded_redacted(raw),
        digest,
        f" {context}" if context else "",
    )
