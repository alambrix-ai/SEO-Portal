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


#: ``AppError.code`` values whose message can carry a vendor's wording. Only
#: these (and :class:`ConnectorError` / ``LLMError`` subclasses) are softened.
#: Every other domain error was written for the user and is shown as written:
#: softening "Workspace not found" into a sentence about a missing service or
#: model would be wrong.
VENDOR_ERROR_CODES: frozenset[str] = frozenset(
    {
        "connector_error",
        "connector_misconfigured",
        "assistant_upstream",
        "assistant_not_configured",
    }
)

_GENERIC = "Something went wrong. Please try again."

# High-confidence credential shapes. A user-facing string containing one is
# dropped for the generic sentence; :func:`bounded_redacted` masks them.
_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\b(?:sk|pk|rk|xox[abpr])-[A-Za-z0-9_\-]{6,}", re.I),
    # Slack rotating tokens: xoxe-1-..., xoxe.xoxp-1-...
    re.compile(r"\bxoxe(?:\.xox[a-z])?-[A-Za-z0-9_\-]{6,}", re.I),
    # Google OAuth access (ya29.) and refresh (1//0) tokens.
    re.compile(r"\bya29\.[A-Za-z0-9_\-]{10,}"),
    re.compile(r"(?<![A-Za-z0-9])1//0[A-Za-z0-9_\-]{10,}"),
    # Shopify admin and custom-app access tokens.
    re.compile(r"\bshp(?:at|ca)_[A-Za-z0-9]{16,}"),
    # Meta (Facebook / Instagram) access tokens.
    re.compile(r"\bEAA[A-Za-z0-9]{20,}"),
    re.compile(r"\bAKIA[0-9A-Z]{12,}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_\-]{20,}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]*"),
    re.compile(r"\b(?:Bearer|Basic)\s+[A-Za-z0-9._~+/=\-]{20,}"),
    re.compile(r"\b[a-z][a-z0-9+.\-]*://[^\s/@:]*:[^\s/@]+@", re.I),
)

# The user-info part of a URL (``redis://:pw@host``, ``postgresql://u:p@h``,
# ``https://token@host``). Greedy up to the last "@" before the path, so a
# password with an unencoded "@" in it is masked whole. Scheme and host are
# kept: they are what makes the log line useful.
_URL_USERINFO = re.compile(r"\b([a-z][a-z0-9+.\-]*://)[^\s/?#]*@", re.I)

# A quoted secret-named key and its value, as JSON or a Python repr writes
# it: ``"access_token": "..."``, ``'password': '...'``, ``"code": 123``.
# The key is kept (it says what was hidden); the value is masked. Token
# counts such as ``"max_tokens"`` are left alone.
_QUOTED_KEY_VALUE = re.compile(
    r"""(?P<key>(?P<q>["'])(?:[A-Za-z0-9_\-]*(?:token(?!s)|secret|password|passwd"""
    r"""|api[_\-]?key|apikey|authorization|credential)[A-Za-z0-9_\-]*|code|pwd)(?P=q))"""
    r"""\s*:\s*(?:"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*'|[^\s,}\]]+)""",
    re.I,
)

# Broader shapes masked in logs only: they also match ordinary prose
# ("the token: paste it here", an address in a sentence), so they are not
# grounds for hiding an authored message.
_LOG_ONLY_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|token|secret|password"
        r"|passwd|pwd|client[_-]?secret|authorization|code)\b\s*[=:]\s*[^\s&,;'\"]+",
        re.I,
    ),
    re.compile(r"\b(?:bearer|basic)\s+[A-Za-z0-9._~+/=\-]{8,}", re.I),
    re.compile(r"(?<=\?)[^\s#]+"),
    re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b"),
)


def is_vendor_origin(exc: BaseException) -> bool:
    """True when the error came from a connector, the assistant or a model."""
    from app.core.exceptions import ConnectorError
    from app.llm.base import LLMError

    if isinstance(exc, ConnectorError | LLMError):
        return True
    return getattr(exc, "code", None) in VENDOR_ERROR_CODES


def public_error_message(exc: BaseException, *, fallback: str | None = None) -> str:
    """Turn any exception into copy safe to show an operator.

    Connector, assistant and model errors may echo a vendor's reply, so their
    wording is softened. Other domain errors carry an authored ``message``
    and are shown verbatim unless it is stack-shaped or secret-shaped.
    Anything else is replaced with a generic sentence when it looks technical.
    """
    generic = fallback or _GENERIC
    message = getattr(exc, "message", None)
    authored = isinstance(message, str) and bool(message.strip())
    text = message.strip() if authored else str(exc).strip()  # type: ignore[union-attr]
    if not text or _has_secret(text):
        return generic

    if is_vendor_origin(exc):
        softened = soften_technical_message(text)
        return generic if softened == _GENERIC else softened

    if authored:
        return generic if _has_technical_markers(text) else text
    return generic if looks_technical(text) else text


def bounded_redacted(value: object, *, limit: int = 300) -> str:
    """``value`` as one line, secret-shaped parts masked, at most ``limit`` chars.

    For server logs only. It is not a licence to show the text to a user:
    exception text can still carry document or customer content.
    """
    text = " ".join(str(value).split())
    text = _URL_USERINFO.sub(r"\1[redacted]@", text)
    text = _QUOTED_KEY_VALUE.sub(r'\g<key>: "[redacted]"', text)
    for pattern in (*_SECRET_PATTERNS, *_LOG_ONLY_PATTERNS):
        text = pattern.sub("[redacted]", text)
    limit = max(limit, 1)
    if len(text) > limit:
        text = text[: limit - 1] + "…"
    return text


def _has_secret(message: str) -> bool:
    return any(pattern.search(message) for pattern in _SECRET_PATTERNS)


def _has_technical_markers(message: str) -> bool:
    """Stack-shaped content, regardless of length."""
    if any(pattern.search(message) for pattern in _TECHNICAL_PATTERNS):
        return True
    return message.startswith("{") or message.startswith("[")


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
