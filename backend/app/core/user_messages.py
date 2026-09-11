"""User-facing wording for errors.

Technical detail (status codes, exception types, vendor bodies, retry counts)
belongs in logs. What the console, emails and agent cards show must make sense
to someone who does not write software.
"""
from __future__ import annotations

import re

# Status → plain sentence. Keep these short enough for a toast.
_HTTP_STATUS_MESSAGES: dict[int, str] = {
    400: "That request could not be understood. Check the details and try again.",
    401: "Those credentials were rejected. Check the key or password and try again.",
    403: "Access was refused. Check that this account has permission.",
    404: "That service or model could not be found. Check the name and try again.",
    408: "The service took too long to respond. Try again in a moment.",
    409: "That change conflicts with something already saved.",
    422: "Some of the details look incomplete or invalid. Check them and try again.",
    429: "This service is temporarily limiting requests. Wait a minute and try again.",
    500: "The other service had a problem. Try again in a few minutes.",
    502: "The other service could not be reached. Try again in a few minutes.",
    503: "The other service is temporarily unavailable. Try again shortly.",
    504: "The other service took too long to respond. Try again shortly.",
}

_TECHNICAL_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\btraceback\b", re.I),
    re.compile(r"\bexception\b", re.I),
    re.compile(r"\bstatus[_ ]?code\b", re.I),
    re.compile(r"\bHTTP/\d", re.I),
    re.compile(r"\bECONN|ETIMEDOUT|ENETUNREACH\b", re.I),
    re.compile(r"\bNoneType\b"),
    re.compile(r"\bat 0x[0-9a-f]+\b", re.I),
    re.compile(r"\bafter \d+ attempts\b", re.I),
    re.compile(r"\breturned \d{3}\b", re.I),
    re.compile(r"^\w+Error:"),
)


def message_for_http_status(status: int, *, service: str = "") -> str:
    """Plain-language copy for an HTTP failure from an external service."""
    base = _HTTP_STATUS_MESSAGES.get(status)
    if base is None:
        if status >= 500:
            base = "The other service had a problem. Try again in a few minutes."
        elif status >= 400:
            base = "That request was rejected. Check the details and try again."
        else:
            base = "Something went wrong talking to the other service."
    if service:
        return f"{service}: {base}"
    return base


def message_for_unreachable(service: str = "") -> str:
    if service:
        return f"Could not reach {service}. Check your connection and try again."
    return "Could not reach the other service. Check your connection and try again."


def public_error_message(exc: BaseException, *, fallback: str | None = None) -> str:
    """Turn any exception into copy safe to show an operator.

    Known domain errors already carry a public ``message``. Everything else is
    replaced with a generic sentence so stack-shaped text never reaches the UI.
    """
    message = getattr(exc, "message", None)
    if isinstance(message, str) and message.strip():
        return soften_technical_message(message.strip())

    text = str(exc).strip()
    if text and not looks_technical(text):
        return text

    return fallback or "Something went wrong. Please try again."


def looks_technical(message: str) -> bool:
    """Heuristic for strings that should not be shown as-is."""
    if not message:
        return True
    if len(message) > 280:
        return True
    if any(pattern.search(message) for pattern in _TECHNICAL_PATTERNS):
        return True
    if message.startswith("{") or message.startswith("["):
        return True
    return False


def soften_technical_message(message: str) -> str:
    """Rewrite common technical phrases when they slip through."""
    lowered = message.lower()
    if "429" in message or "rate limit" in lowered or "quota" in lowered:
        return "This service is temporarily limiting requests. Wait a minute and try again."
    if "401" in message or "unauthorized" in lowered or "invalid api key" in lowered:
        return "Those credentials were rejected. Check the key or password and try again."
    if "403" in message or "forbidden" in lowered or "permission" in lowered:
        return "Access was refused. Check that this account has permission."
    if "404" in message or "not found" in lowered:
        return "That service or model could not be found. Check the name and try again."
    if "timeout" in lowered or "timed out" in lowered:
        return "The service took too long to respond. Try again in a moment."
    if "network is unreachable" in lowered or "connection refused" in lowered:
        return "Could not reach the other service. Try again shortly."
    if looks_technical(message):
        return "Something went wrong. Please try again."
    return message
