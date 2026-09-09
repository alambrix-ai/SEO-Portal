"""One-time passcodes and access tokens.

This platform has no passwords. Signing in means proving control of a mailbox:
the server mails a short numeric code and the caller sends it back. That
removes a whole class of problem — no hashes to leak, no reuse across sites,
no reset flow to phish, nothing for a user to choose badly — and concentrates
the risk in one place instead: the code must be unguessable, short-lived, and
usable exactly once.

This module owns the two primitives that follow from that:

* :func:`new_login_code` — the code itself, from a CSPRNG, in a form a person
  can read off a screen and retype without ambiguity.
* :func:`codes_match` — a constant-time comparison, so a wrong code leaks
  nothing about how wrong it was.

The *rules* around a code — its lifetime, its attempt cap, its single use —
belong to the row that stores it (``app.models.identity.LoginCode``) and the
service that issues it. Storage is an HMAC via :func:`app.core.crypto.hash_token`;
a plaintext code never reaches the database.

Encryption of stored data lives in :mod:`app.core.crypto`; this module only
handles credentials and tokens.
"""
from __future__ import annotations

import hmac
import re
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt

from app.core.config import settings
from app.core.exceptions import AuthError, InvalidInputError

# ── One-time passcodes ─────────────────────────────────────────────────────
# Digits only. An alphanumeric code of the same entropy would be shorter, but
# it also has to survive being read aloud, retyped from a phone, and passed
# through a mail client that may capitalise it — and 0/O and 1/l/I are where
# that goes wrong. Digits are unambiguous, and the security margin comes from
# the code's lifetime and attempt cap rather than its alphabet.
_MIN_CODE_LENGTH = 6
_MAX_CODE_LENGTH = 10
_CODE_RE = re.compile(r"\D")


def new_login_code(length: int | None = None) -> str:
    """Return a fresh numeric passcode, uniformly distributed.

    Built digit by digit from :func:`secrets.choice` rather than by formatting
    one random integer: the naive ``randbelow(10**n)`` is uniform too, but only
    if the result is zero-padded, and forgetting that quietly biases every code
    towards fewer digits. This form cannot be got wrong that way.
    """
    size = max(_MIN_CODE_LENGTH, min(length or settings.login_code_length, _MAX_CODE_LENGTH))
    return "".join(secrets.choice("0123456789") for _ in range(size))


def normalize_code(submitted: str) -> str:
    """Strip everything a person might paste around a code.

    Mail clients and phone keyboards add spaces, dashes and non-breaking
    spaces; a code that works when typed but fails when pasted reads as a
    broken product rather than a formatting rule.
    """
    if len(submitted) > 64:
        # Nothing legitimate is this long, and the regex should not be handed
        # an unbounded string.
        raise InvalidInputError("That is not a valid code")
    return _CODE_RE.sub("", submitted)


def codes_match(submitted: str, expected: str) -> bool:
    """Constant-time comparison of two codes.

    ``==`` on strings returns as soon as it finds a difference, which times
    differently for a code that is wrong in the first digit than in the last.
    Over enough attempts that is a side channel; ``compare_digest`` is not.
    """
    if not submitted or not expected:
        return False
    return hmac.compare_digest(submitted, expected)


# ── Access tokens ──────────────────────────────────────────────────────────
_ALGORITHM = "HS256"
_ISSUER = "automarket-ai"
_AUDIENCE = "automarket-api"


def create_access_token(
    *,
    user_id: str,
    tenant_id: str,
    role: str,
    session_id: str,
    extra: dict[str, Any] | None = None,
) -> tuple[str, int]:
    """Return ``(token, expires_in_seconds)``.

    Access tokens are short-lived and carry the organisation id, so a token
    minted for one workspace cannot be replayed against another. The refresh
    token (see :mod:`app.services.auth`) is what carries longer-term identity,
    and it is revocable.
    """
    now = datetime.now(UTC)
    ttl = timedelta(minutes=settings.access_token_ttl_minutes)
    payload: dict[str, Any] = {
        "sub": user_id,
        "tid": tenant_id,
        "role": role,
        "sid": session_id,
        "typ": "access",
        "iat": int(now.timestamp()),
        "nbf": int(now.timestamp()),
        "exp": int((now + ttl).timestamp()),
        "iss": _ISSUER,
        "aud": _AUDIENCE,
        "jti": secrets.token_urlsafe(12),
    }
    if extra:
        payload.update(extra)
    token = jwt.encode(payload, settings.secret_key, algorithm=_ALGORITHM)
    return token, int(ttl.total_seconds())


def decode_access_token(token: str) -> dict[str, Any]:
    try:
        claims = jwt.decode(
            token,
            settings.secret_key,
            algorithms=[_ALGORITHM],
            issuer=_ISSUER,
            audience=_AUDIENCE,
            options={"require": ["exp", "iat", "sub", "tid"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise AuthError("Session expired - refresh or sign in again") from exc
    except jwt.PyJWTError as exc:
        raise AuthError("Invalid or malformed token") from exc
    if claims.get("typ") != "access":
        raise AuthError("Wrong token type")
    return claims
