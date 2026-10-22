"""Sign-in provider apps saved in Portal Admin (table ``platform_oauth_apps``).

The platform's own app with each vendor (its client ID and secret, and the
few extra values some vendors need) can be entered in Portal Admin instead
of the server environment. This module stores them, serves them to the
sign-in code, and checks them with the vendor where that is cheap and
reliable.

Resolution
    A value saved here wins; otherwise the server setting of the same name
    (``OAUTH_GOOGLE_CLIENT_ID``...) is used. See
    :func:`app.connectors.base.oauth_providers.platform_setting`.

Encryption
    The client secret and the extra values are encrypted with AES-256-GCM
    under a key derived from the platform master key (tier 1,
    :func:`app.core.crypto.encrypt_column`), with the provider in the
    authenticated label, so a ciphertext copied onto another provider's row
    does not decrypt. Nothing secret is ever returned to the browser: the
    admin page sees "set" and the last four characters.

Caching
    Every worker keeps a snapshot for :data:`CACHE_TTL_SECONDS`. A save
    clears the snapshot of the worker that served it at once; the other
    workers pick the change up within the TTL. When the table cannot be read
    (the migration has not run yet, the database is away) the snapshot is
    empty, so the server settings still apply.
"""
from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.crypto import CryptoError, decrypt_column, encrypt_column
from app.core.exceptions import InvalidInputError
from app.core.logging import get_logger
from app.models.portal import PlatformOAuthApp
from app.services.provider_setup import AppField, setup_for

log = get_logger(__name__)

#: How long a worker trusts its snapshot. A save on another worker shows up
#: here within this many seconds.
CACHE_TTL_SECONDS = 30.0


def _secret_label(provider: str) -> str:
    return f"platform_oauth_apps.client_secret:{provider}"


def _extras_label(provider: str) -> str:
    return f"platform_oauth_apps.extras:{provider}"


# ── Snapshot and cache ─────────────────────────────────────────────────────
@dataclass(frozen=True, slots=True)
class Snapshot:
    #: ``{setting name: value}`` for every value saved in Portal Admin.
    values: dict[str, str] = field(default_factory=dict)
    #: ``{provider: (updated_by, updated_at)}``.
    meta: dict[str, tuple[str, datetime | None]] = field(default_factory=dict)


def decode_row(row: PlatformOAuthApp) -> dict[str, str]:
    """The plaintext values of one row, keyed by setting name. A value that
    does not decrypt (a changed master key) is left out, so it reads as not
    set rather than as garbage."""
    provider = row.provider
    out: dict[str, str] = {}
    if row.client_id:
        out[f"oauth_{provider}_client_id"] = row.client_id
    if row.client_secret_encrypted:
        try:
            out[f"oauth_{provider}_client_secret"] = decrypt_column(
                _secret_label(provider), row.client_secret_encrypted
            )
        except CryptoError:
            log.warning("Saved %s client secret could not be decrypted; ignoring it", provider)
    if row.extras_encrypted:
        try:
            extras = json.loads(decrypt_column(_extras_label(provider), row.extras_encrypted))
        except (CryptoError, ValueError):
            log.warning("Saved %s extra values could not be decrypted; ignoring them", provider)
            extras = {}
        allowed = {f.key for f in _fields(provider)}
        out.update({k: str(v) for k, v in extras.items() if k in allowed and v})
    return out


def snapshot_from_rows(rows: list[PlatformOAuthApp]) -> Snapshot:
    values: dict[str, str] = {}
    meta: dict[str, tuple[str, datetime | None]] = {}
    for row in rows:
        values.update(decode_row(row))
        meta[row.provider] = (row.updated_by or "", row.updated_at)
    return Snapshot(values=values, meta=meta)


def read_snapshot(db: Session) -> Snapshot:
    return snapshot_from_rows(list(db.execute(select(PlatformOAuthApp)).scalars()))


def _load_from_database() -> Snapshot:
    """Read the table on a session of its own, so a failure here can never
    spoil the transaction of the request that asked."""
    from app.db.session import SessionLocal

    with SessionLocal() as db:
        return read_snapshot(db)


_lock = threading.Lock()
_cached: Snapshot | None = None
_cached_at = 0.0
_warned = False
#: Replaced in tests (see tests/conftest.py), which have no database.
_loader: Callable[[], Snapshot] = _load_from_database


def set_loader(loader: Callable[[], Snapshot] | None) -> None:
    """Swap where snapshots come from (tests). ``None`` restores the database."""
    global _loader
    _loader = loader or _load_from_database
    invalidate_cache()


def invalidate_cache() -> None:
    global _cached, _cached_at
    with _lock:
        _cached, _cached_at = None, 0.0


def prime_cache(snap: Snapshot) -> None:
    """Use a snapshot just read on the request's own session."""
    global _cached, _cached_at
    with _lock:
        _cached, _cached_at = snap, time.monotonic()


def snapshot() -> Snapshot:
    global _cached, _cached_at, _warned
    with _lock:
        if _cached is not None and time.monotonic() - _cached_at < CACHE_TTL_SECONDS:
            return _cached
    try:
        fresh = _loader()
        _warned = False
    except Exception as exc:  # noqa: BLE001 - the server settings still apply
        if not _warned:
            log.warning(
                "Sign-in provider apps could not be read (%s); using server settings only. "
                "Run the database migrations if this persists.",
                type(exc).__name__,
            )
            _warned = True
        fresh = Snapshot()
    prime_cache(fresh)
    return fresh


def stored_value(key: str) -> str:
    """The value saved in Portal Admin for one setting, or ``""``."""
    return snapshot().values.get(key, "")


# ── Writing ────────────────────────────────────────────────────────────────
def _fields(provider: str) -> tuple[AppField, ...]:
    setup = setup_for(provider)
    return setup.fields if setup else ()


def normalise(field_: AppField, value: str) -> str:
    text = (value or "").strip()
    if field_.multiline and "\\n" in text and "\n" not in text:
        # A PEM pasted as one line with escaped newlines.
        text = text.replace("\\n", "\n")
    return text


def _find(db: Session, provider: str) -> PlatformOAuthApp | None:
    return db.execute(
        select(PlatformOAuthApp).where(PlatformOAuthApp.provider == provider)
    ).scalar_one_or_none()


def save(db: Session, *, provider: str, values: dict[str, str], actor: str) -> list[str]:
    """Save what was typed for one provider. A blank box keeps what is
    already saved, so one value can be changed without typing the others
    again. Returns the labels of the values that changed (never the values).
    The caller commits, then calls :func:`invalidate_cache`."""
    fields = {f.key: f for f in _fields(provider)}
    if not fields:
        raise InvalidInputError("That sign-in provider has nothing to set up.")
    unknown = sorted(set(values) - set(fields))
    if unknown:
        raise InvalidInputError("Some of those values do not belong to this provider.")

    row = _find(db, provider)
    current = decode_row(row) if row is not None else {}
    merged = dict(current)
    changed: list[str] = []
    for key, raw in values.items():
        value = normalise(fields[key], raw)
        if len(value) > 20_000:
            raise InvalidInputError(f"{fields[key].label} is too long.")
        if value and value != current.get(key, ""):
            merged[key] = value
            changed.append(fields[key].label)
    if not changed:
        return []

    if row is None:
        row = PlatformOAuthApp(provider=provider)
        db.add(row)
    id_key, secret_key = f"oauth_{provider}_client_id", f"oauth_{provider}_client_secret"
    row.client_id = merged.get(id_key, "")
    secret = merged.get(secret_key, "")
    row.client_secret_encrypted = encrypt_column(_secret_label(provider), secret) if secret else ""
    extras = {k: v for k, v in merged.items() if k not in (id_key, secret_key) and v}
    row.extras_encrypted = (
        encrypt_column(_extras_label(provider), json.dumps(extras, separators=(",", ":")))
        if extras
        else ""
    )
    row.updated_by = actor[:255]
    db.flush()
    return changed


def clear(db: Session, *, provider: str) -> bool:
    """Remove everything saved here for one provider. Server settings, if
    any, apply again. True when there was something to remove."""
    row = _find(db, provider)
    if row is None:
        return False
    db.delete(row)
    db.flush()
    return True


# ── What the admin page may see ────────────────────────────────────────────
def preview(field_: AppField, value: str) -> str:
    """What the browser is shown for a value. The client ID, App ID and
    link name are not secrets (they appear in every sign-in link); a secret
    shows only its last four characters, and a private key nothing."""
    if not value:
        return ""
    if not field_.secret:
        return value
    if field_.multiline or len(value) < 12:
        return "Set"
    return f"Set · ends in {value[-4:]}"


def field_status(field_: AppField, snap: Snapshot) -> dict:
    saved = snap.values.get(field_.key, "")
    env = str(getattr(settings, field_.key, "") or "").strip()
    value = saved or env
    return {
        "key": field_.key,
        "label": field_.label,
        "secret": field_.secret,
        "multiline": field_.multiline,
        "needed_for": field_.needed_for,
        "help": field_.help,
        "placeholder": field_.placeholder,
        "is_set": bool(value),
        "source": "portal" if saved else "server" if env else "",
        "preview": preview(field_, value),
    }


# ── Checking credentials with the vendor ───────────────────────────────────
CheckStatus = Literal["ok", "failed", "unchecked"]


@dataclass(frozen=True, slots=True)
class CheckResult:
    status: CheckStatus
    message: str


#: Sent as the authorization code when probing a token endpoint. Never a
#: real code, so nothing can be granted by it.
PROBE_CODE = "automarket-credential-check"


def classify_token_probe(status_code: int, body: object) -> Literal["accepted", "rejected", "unknown"]:
    """Read a token endpoint's answer to a made-up authorization code.

    RFC 6749 §5.2: a server that cannot authenticate the client answers
    ``invalid_client``; one that can, but finds the code bad, answers
    ``invalid_grant``. GitHub uses its own names for the same two cases
    (``incorrect_client_credentials`` / ``bad_verification_code``). Anything
    else is not trusted either way.
    """
    if not isinstance(body, dict):
        return "unknown"
    error = str(body.get("error") or "").strip().lower()
    if error in {"invalid_client", "unauthorized_client", "incorrect_client_credentials"}:
        return "rejected"
    if error in {"invalid_grant", "bad_verification_code"}:
        return "accepted"
    return "unknown"


def _http() -> httpx.Client:
    """Replaced in tests."""
    return httpx.Client(timeout=10.0)


def _unchecked(label: str) -> CheckResult:
    return CheckResult(
        "unchecked",
        f"Saved. {label} could not be checked from here just now; test it by connecting an integration.",
    )


def _probe_token_endpoint(
    url: str, *, label: str, client_id: str, client_secret: str, redirect_uri: str
) -> CheckResult:
    data = {
        "grant_type": "authorization_code",
        "code": PROBE_CODE,
        "redirect_uri": redirect_uri,
        "client_id": client_id,
        "client_secret": client_secret,
    }
    try:
        with _http() as client:
            response = client.post(url, data=data, headers={"Accept": "application/json"})
        body = response.json()
    except (httpx.HTTPError, ValueError):
        return _unchecked(label)
    verdict = classify_token_probe(response.status_code, body)
    if verdict == "accepted":
        return CheckResult("ok", f"{label} accepted these details. Sign-in is ready.")
    if verdict == "rejected":
        return CheckResult(
            "failed",
            f"{label} did not accept this ID and secret. Copy both again from the same app and save.",
        )
    return _unchecked(label)


def _check_google(values: dict[str, str], redirect_uri: str) -> CheckResult:
    client_id = values.get("oauth_google_client_id", "")
    if not client_id.endswith(".apps.googleusercontent.com"):
        return CheckResult(
            "failed",
            "That does not look like a Google Client ID. It ends in .apps.googleusercontent.com.",
        )
    return _probe_token_endpoint(
        "https://oauth2.googleapis.com/token",
        label="Google",
        client_id=client_id,
        client_secret=values.get("oauth_google_client_secret", ""),
        redirect_uri=redirect_uri,
    )


def _check_github(values: dict[str, str], redirect_uri: str) -> CheckResult:
    """The client ID and secret at the token endpoint, then the App ID and
    private key by asking GitHub which app they sign for (``GET /app``)."""
    first = _probe_token_endpoint(
        "https://github.com/login/oauth/access_token",
        label="GitHub",
        client_id=values.get("oauth_github_client_id", ""),
        client_secret=values.get("oauth_github_client_secret", ""),
        redirect_uri=redirect_uri,
    )
    if first.status == "failed":
        return first
    app_id = values.get("github_app_id", "")
    key = values.get("github_app_private_key", "")
    if not (app_id and key):
        return first
    if not app_id.isdigit():
        return CheckResult("failed", "The App ID is a number, shown at the top of the app's General page.")
    import jwt

    issued = int(time.time())
    try:
        token = jwt.encode({"iat": issued - 60, "exp": issued + 540, "iss": app_id}, key, algorithm="RS256")
    except (ValueError, TypeError, jwt.PyJWTError):
        return CheckResult(
            "failed", "The private key could not be read. Paste the whole .pem file, including its first and last lines."
        )
    try:
        with _http() as client:
            response = client.get(
                "https://api.github.com/app",
                headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
            )
        body = response.json()
    except (httpx.HTTPError, ValueError):
        return _unchecked("GitHub")
    if response.status_code == 401:
        return CheckResult("failed", "GitHub did not accept the App ID and private key together.")
    if response.status_code != 200 or not isinstance(body, dict):
        return _unchecked("GitHub")
    slug = values.get("github_app_slug", "")
    if slug and str(body.get("slug") or "") != slug:
        return CheckResult(
            "failed", f"GitHub says this app's public link name is “{body.get('slug')}”, not “{slug}”."
        )
    if first.status == "ok":
        return CheckResult("ok", "GitHub accepted these details. Sign-in is ready.")
    return first


#: Providers whose credentials can be checked without a person signing in.
CHECKS: dict[str, Callable[[dict[str, str], str], CheckResult]] = {
    "google": _check_google,
    "github": _check_github,
}


def check(provider: str) -> CheckResult:
    """Check the values in force (saved here, else server settings)."""
    from app.connectors.base import oauth_providers

    prov = oauth_providers.get(provider)
    if prov is None:
        raise InvalidInputError("That sign-in provider is not recognised.")
    values = {f.key: oauth_providers.platform_setting(f.key) for f in _fields(provider)}
    if not (prov.client_id and prov.client_secret):
        return CheckResult("failed", "Enter both values from the vendor's console, then save.")
    runner = CHECKS.get(provider)
    if runner is None:
        return CheckResult(
            "unchecked",
            f"Saved. {prov.label} offers no quick check, so test it by connecting an integration.",
        )
    return runner(values, prov.redirect_uri)


__all__ = [
    "CACHE_TTL_SECONDS",
    "CheckResult",
    "Snapshot",
    "check",
    "classify_token_probe",
    "clear",
    "field_status",
    "invalidate_cache",
    "prime_cache",
    "read_snapshot",
    "save",
    "set_loader",
    "snapshot",
    "stored_value",
]
