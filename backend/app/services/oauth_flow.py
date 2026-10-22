"""The platform sign-in flows: "Sign in with Google", and WordPress approval.

Both follow the same shape:

1. **start** (authenticated) — sign a short-lived state naming the
   workspace, the person, the connector and where to come back to; record
   it server-side (with the PKCE verifier, encrypted) so it can be redeemed
   once; hand the browser the vendor's URL.
2. **callback** (public — the redirect URI registered with the vendor) —
   check the signature and expiry, redeem the state (deleting it, so a replay
   finds nothing), exchange the code, store the tokens in the connector's
   encrypted credentials, and send the browser back to the console.
3. **choose** — list what the signed-in account can reach (GA4 properties,
   Search Console sites, ad accounts, business locations), save the pick
   into the connector's own fields and run its health check. One choice is
   picked automatically.

Nothing here raises to the browser on the callback: every failure becomes a
fixed code from :data:`OAUTH_ERROR_CODES` on the console's ``oauth_error``
parameter, which the console turns into a plain sentence. Exception text is
never put in the address. Tokens are never logged, and the browser is only
ever sent to the console's own address.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from urllib.parse import urlencode, urlsplit, urlunsplit

import httpx
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.connectors.base import oauth_providers, registry
from app.connectors.base.connector import ConnectorSpec, OAuthChoice
from app.connectors.base.credentials import (
    ACCESS_TOKEN_EXPIRY_KEY,
    OAUTH_ACCOUNT_KEY,
    OAUTH_SOURCE_KEY,
    PLATFORM_SOURCE,
    SIGN_IN_EXPIRY_KEY,
    Credentials,
    from_platform_sign_in,
)
from app.connectors.base.oauth_providers import OAuthProvider
from app.core.config import settings
from app.core.exceptions import AppError, ConnectorError, InvalidInputError
from app.core.logging import get_logger, log_failure
from app.db.base import utcnow
from app.models.connector import ConnectorRecord
from app.models.oauth import OAuthState
from app.models.workspace import Organization, User
from app.services.encryption import Ctx, OrgCipher

log = get_logger(__name__)

#: Where the console lands when the state cannot say where it came from.
DEFAULT_RETURN = "/connectors"
#: Non-secret hint: the label of what was chosen after signing in.
OAUTH_CHOICE_KEY = "oauthChoice"
_STATE_VERSION = 1
_PKCE_CONTEXT = "oauth_state.code_verifier"


#: Every value the callback may put on ``oauth_error``. The console maps each
#: one to a fixed sentence (frontend ``OAuthReturn.tsx``); anything else it
#: receives is shown as the generic message. Keep the two lists in step.
CONNECT_FAILED = "connect_failed"
LINK_INVALID = "link_invalid"
LINK_EXPIRED = "link_expired"
CANCELLED = "cancelled"
PROVIDER_REFUSED = "provider_refused"
PROVIDER_UNREACHABLE = "provider_unreachable"
NOT_SET_UP = "not_set_up"
PERMISSIONS_MISSING = "permissions_missing"
NO_LASTING_ACCESS = "no_lasting_access"
SESSION_ENDED = "session_ended"
ROLE_CHANGED = "role_changed"
SITE_NOT_SECURE = "site_not_secure"
SITE_MISMATCH = "site_mismatch"
OAUTH_ERROR_CODES: frozenset[str] = frozenset(
    {
        CONNECT_FAILED,
        LINK_INVALID,
        LINK_EXPIRED,
        CANCELLED,
        PROVIDER_REFUSED,
        PROVIDER_UNREACHABLE,
        NOT_SET_UP,
        PERMISSIONS_MISSING,
        NO_LASTING_ACCESS,
        SESSION_ENDED,
        ROLE_CHANGED,
        SITE_NOT_SECURE,
        SITE_MISMATCH,
    }
)


def error_code(code: str) -> str:
    """``code`` when it is one of :data:`OAUTH_ERROR_CODES`, else the generic one."""
    return code if code in OAUTH_ERROR_CODES else CONNECT_FAILED


class OAuthFlowError(Exception):
    """A sign-in that cannot finish.

    ``code`` is what reaches the browser; the message is for callers and
    tests only and is never put in the redirect. Raised without a code (as
    the vendor helpers do), it is the generic :data:`CONNECT_FAILED`.
    """

    def __init__(self, message: str = "", *, code: str = CONNECT_FAILED) -> None:
        super().__init__(message)
        self.code = error_code(code)


# ── State ──────────────────────────────────────────────────────────────────
@dataclass(frozen=True, slots=True)
class StatePayload:
    tenant_id: str
    user_id: str
    slug: str
    provider: str
    nonce: str
    return_path: str
    expires_at: int


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _signature(body: str) -> str:
    key = settings.secret_key.encode()
    return _b64(hmac.new(key, b"oauth-state:" + body.encode(), hashlib.sha256).digest())


def sign_state(payload: StatePayload) -> str:
    body = _b64(
        json.dumps(
            {
                "v": _STATE_VERSION,
                "t": payload.tenant_id,
                "u": payload.user_id,
                "s": payload.slug,
                "p": payload.provider,
                "n": payload.nonce,
                "r": payload.return_path,
                "e": payload.expires_at,
            },
            separators=(",", ":"),
        ).encode()
    )
    return f"{body}.{_signature(body)}"


def verify_state(token: str, *, now: float | None = None) -> StatePayload:
    """Check a state's signature and expiry. Raises :class:`OAuthFlowError`.

    Single use is enforced separately, by redeeming the server-side row.
    """
    expired = OAuthFlowError(
        "That sign-in took too long or was already used. Start again from the "
        "connect screen.",
        code=LINK_EXPIRED,
    )
    try:
        body, sig = (token or "").split(".", 1)
    except ValueError:
        raise expired from None
    if not hmac.compare_digest(_signature(body), sig):
        raise expired
    try:
        data = json.loads(_unb64(body))
        payload = StatePayload(
            tenant_id=str(data["t"]),
            user_id=str(data["u"]),
            slug=str(data["s"]),
            provider=str(data["p"]),
            nonce=str(data["n"]),
            return_path=safe_return_path(str(data.get("r") or "")),
            expires_at=int(data["e"]),
        )
    except (ValueError, KeyError, TypeError):
        raise expired from None
    if data.get("v") != _STATE_VERSION or (now or time.time()) > payload.expires_at:
        raise expired
    return payload


def nonce_key(nonce: str) -> str:
    """The row id for a state: a hash, so the table holds nothing redeemable."""
    return hashlib.sha256(nonce.encode()).hexdigest()


def safe_return_path(raw: str) -> str:
    """A path on the console, or the default. Never another origin.

    Only a single leading slash, no scheme, no host, no backslashes: the
    browser is redirected to ``public_base_url + path``, and anything that
    could turn that into a different origin is refused.
    """
    path = (raw or "").strip()
    if (
        not path.startswith("/")
        or path.startswith("//")
        or "\\" in path
        or any(ord(c) < 32 for c in path)
        or len(path) > 512
    ):
        return DEFAULT_RETURN
    parts = urlsplit(path)
    if parts.scheme or parts.netloc:
        return DEFAULT_RETURN
    return path


def console_url(return_path: str, **params: str) -> str:
    """The console address to send the browser back to, with our params.

    Any of our own result parameters already on the path are replaced, so a
    second attempt does not carry the first one's error.
    """
    path = safe_return_path(return_path)
    parts = urlsplit(path)
    from urllib.parse import parse_qsl

    ours = {"connected", "oauth_error", "oauth_choose", "oauth_slug"}
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k not in ours]
    query.extend((k, v) for k, v in params.items() if v)
    base = settings.public_base_url.rstrip("/")
    return base + urlunsplit(("", "", parts.path or "/", urlencode(query), ""))


# ── PKCE and the authorize URL ─────────────────────────────────────────────
def pkce_pair() -> tuple[str, str]:
    """``(verifier, S256 challenge)`` per RFC 7636."""
    verifier = _b64(secrets.token_bytes(48))
    challenge = _b64(hashlib.sha256(verifier.encode()).digest())
    return verifier, challenge


def build_authorize_url(
    provider: OAuthProvider,
    spec: ConnectorSpec,
    *,
    state: str,
    code_challenge: str = "",
    login_hint: str = "",
    inputs: dict[str, str] | None = None,
) -> str:
    """The vendor's sign-in address. ``inputs`` are the checked answers to
    the connector's sign-in inputs (a sandbox toggle can change the scopes).
    """
    if provider.flavor:
        from app.services import oauth_vendors

        custom = oauth_vendors.authorize_url(
            provider, spec, state=state, code_challenge=code_challenge, login_hint=login_hint
        )
        if custom:
            return custom
    params: dict[str, str] = {
        "client_id": provider.client_id,
        "redirect_uri": provider.redirect_uri,
        "response_type": "code",
        "scope": provider.scope_separator.join(oauth_providers.scopes_for(spec, inputs)),
        "state": state,
        **provider.extra_params,
    }
    if provider.pkce and code_challenge:
        params["code_challenge"] = code_challenge
        params["code_challenge_method"] = "S256"
    if login_hint:
        params["login_hint"] = login_hint
    return f"{provider.authorize_url}?{urlencode(params)}"


# ── Token exchange ─────────────────────────────────────────────────────────
@dataclass(slots=True)
class TokenSet:
    access_token: str
    refresh_token: str = ""
    expires_at: float = 0.0
    scopes: tuple[str, ...] = ()
    account: str = ""
    extra: dict = field(default_factory=dict)


def _email_from_id_token(id_token: str) -> str:
    """The signed-in address from an OpenID id_token.

    Read without verifying the signature, which OpenID Connect permits for
    a token received directly from the token endpoint over TLS (Core §3.1.3.7)
    — it is used for display and as a login hint, never for authorisation.
    """
    try:
        claims = json.loads(_unb64(id_token.split(".")[1]))
    except (IndexError, ValueError):
        return ""
    # Microsoft puts the sign-in address in preferred_username when the
    # account has no email claim.
    email = str(claims.get("email") or claims.get("preferred_username") or "")
    return email if "@" in email else ""


def exchange_code(
    provider: OAuthProvider, *, code: str, verifier: str, now: float | None = None
) -> TokenSet:
    """Trade the authorization code for tokens. Raises :class:`OAuthFlowError`."""
    issued = now or time.time()
    if provider.flavor:
        from app.services import oauth_vendors

        custom = oauth_vendors.exchange(provider, code=code, verifier=verifier, now=issued)
        if custom is not None:
            if not custom.account:
                custom.account = oauth_vendors.identify(provider, custom.access_token)
            return custom
    payload = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": provider.redirect_uri,
        "client_id": provider.client_id,
        "client_secret": provider.client_secret,
    }
    headers = {"Accept": "application/json"}
    if provider.token_auth == "basic":
        # The client authenticates with a Basic header instead (RFC 6749 §2.3.1).
        payload.pop("client_secret")
        headers["Authorization"] = basic_auth(provider.client_id, provider.client_secret)
    if provider.pkce and verifier:
        payload["code_verifier"] = verifier
    try:
        with httpx.Client(timeout=settings.connector_timeout_seconds) as client:
            response = client.post(provider.token_url, data=payload, headers=headers)
    except httpx.HTTPError as exc:
        log.warning("%s token exchange unreachable: %s", provider.name, type(exc).__name__)
        raise OAuthFlowError(
            f"{provider.label} could not be reached to finish signing in. Try again.",
            code=PROVIDER_UNREACHABLE,
        ) from None

    try:
        data = response.json()
    except ValueError:
        data = {}
    if response.status_code >= 400 or not data.get("access_token"):
        # The error code only — the body of a failed exchange is not logged.
        log.warning(
            "%s token exchange refused (%s): %s",
            provider.name,
            response.status_code,
            str(data.get("error") or "no access_token")[:80],
        )
        raise OAuthFlowError(
            f"{provider.label} did not accept the sign-in. Start again from the "
            "connect screen.",
            code=PROVIDER_REFUSED,
        )

    refresh = str(data.get("refresh_token") or "")
    if data.get("expires_in"):
        expires_at = issued + float(data["expires_in"])
    else:
        # Unstated: an hour when there is a refresh token to renew it with,
        # otherwise unknown (a long-lived token) rather than invented.
        expires_at = issued + 3600 if refresh else 0.0
    tokens = TokenSet(
        access_token=str(data["access_token"]),
        refresh_token=refresh,
        expires_at=expires_at,
        # Space-separated per RFC 6749; LinkedIn is reported to use commas.
        scopes=tuple(s for s in re.split(r"[\s,]+", str(data.get("scope") or "")) if s),
        account=_email_from_id_token(str(data.get("id_token") or "")),
    )
    if provider.flavor and not tokens.account:
        from app.services import oauth_vendors

        tokens.account = oauth_vendors.identify(provider, tokens.access_token)
    return tokens


def basic_auth(client_id: str, client_secret: str) -> str:
    """An HTTP Basic ``Authorization`` value for an OAuth client."""
    raw = f"{client_id}:{client_secret}".encode()
    return "Basic " + base64.b64encode(raw).decode()


def missing_scopes(
    spec: ConnectorSpec, granted: tuple[str, ...], wanted: tuple[str, ...] | None = None
) -> list[str]:
    """Scopes the connector needs that the person did not grant.

    Google shows each permission as its own tick box, so a sign-in can
    succeed with the one that matters left unticked. ``wanted`` is what this
    sign-in asked for when its inputs changed the scopes (a sandbox).
    """
    if not granted or spec.oauth is None:
        # A vendor that does not echo scopes granted what was asked.
        return []
    have = set(granted)
    # Microsoft may echo a resource scope in full or by its last segment.
    short = {g.rsplit("/", 1)[-1] for g in granted if "/" in g}
    return [
        s for s in (spec.oauth.scopes if wanted is None else wanted)
        if s not in have and not ("/" in s and s.rsplit("/", 1)[-1] in have | short)
    ]


# ── The server-side half of the state ──────────────────────────────────────
def _put_state(
    db: Session, payload: StatePayload, *, verifier_encrypted: str, context: dict
) -> None:
    # Expired rows are cleared opportunistically; nothing else needs them.
    db.execute(delete(OAuthState).where(OAuthState.expires_at < utcnow()))
    db.add(
        OAuthState(
            id=nonce_key(payload.nonce),
            tenant_id=payload.tenant_id,
            user_id=payload.user_id,
            slug=payload.slug,
            provider=payload.provider,
            verifier_encrypted=verifier_encrypted,
            context=context,
            expires_at=datetime.fromtimestamp(payload.expires_at, UTC),
        )
    )
    db.flush()


@dataclass(slots=True)
class RedeemedState:
    verifier_encrypted: str
    context: dict


def _redeem_state(db: Session, payload: StatePayload) -> RedeemedState | None:
    """Delete the state's row and return it — once. A replay gets None.

    ``DELETE … RETURNING`` is atomic, so two workers handed the same state
    cannot both redeem it.
    """
    row = db.execute(
        delete(OAuthState)
        .where(
            OAuthState.id == nonce_key(payload.nonce),
            OAuthState.tenant_id == payload.tenant_id,
            OAuthState.slug == payload.slug,
            OAuthState.provider == payload.provider,
            OAuthState.expires_at > utcnow(),
        )
        .returning(OAuthState.verifier_encrypted, OAuthState.context)
    ).first()
    if row is None:
        return None
    return RedeemedState(verifier_encrypted=row[0] or "", context=dict(row[1] or {}))


# ── Start ──────────────────────────────────────────────────────────────────
@dataclass(slots=True)
class StartResult:
    #: Where to send the browser. Empty when no redirect is needed.
    authorize_url: str = ""
    #: ``connected`` or ``choose`` when an existing sign-in was reused.
    status: str = "redirect"


def _new_state(
    db: Session,
    *,
    org: Organization,
    user: User,
    slug: str,
    provider: OAuthProvider,
    return_path: str,
    verifier: str = "",
    context: dict | None = None,
) -> str:
    payload = StatePayload(
        tenant_id=str(org.id),
        user_id=str(user.id),
        slug=slug,
        provider=provider.name,
        nonce=secrets.token_urlsafe(24),
        return_path=safe_return_path(return_path),
        expires_at=int(time.time() + settings.oauth_state_ttl_minutes * 60),
    )
    cipher = OrgCipher.for_org(org)
    _put_state(
        db,
        payload,
        verifier_encrypted=cipher.encrypt(verifier, context=_PKCE_CONTEXT) if verifier else "",
        context=context or {},
    )
    return sign_state(payload)


def _spec_or_404(slug: str) -> ConnectorSpec:
    spec = registry.get_spec(slug)
    if spec is None or spec.oauth is None:
        raise InvalidInputError("This integration does not offer sign-in.")
    return spec


def start(
    db: Session,
    *,
    org: Organization,
    user: User,
    slug: str,
    return_path: str = DEFAULT_RETURN,
    site_url: str = "",
    reuse: bool = False,
    inputs: dict[str, str] | None = None,
) -> StartResult:
    """Begin a sign-in for one connector. Raises :class:`AppError` subclasses
    with plain messages (the console shows them as they are).

    ``inputs`` are the answers to the connector's ``sign_in_inputs`` (a
    Shopify store address, the Salesforce sandbox switch)."""
    from app.services import connectors as connector_service

    record = connector_service.get_record(db, tenant_id=org.id, slug=slug)
    spec = _spec_or_404(slug)
    provider = oauth_providers.provider_for(spec)
    if provider is None:
        raise InvalidInputError("This integration does not offer sign-in.")

    if provider.kind == "wordpress_app_password":
        return _start_wordpress(
            db, org=org, user=user, spec=spec, provider=provider,
            return_path=return_path, site_url=site_url,
        )

    if not oauth_providers.sign_in_available(spec):
        raise InvalidInputError(
            f"Sign in with {provider.label} is not set up on this platform yet. "
            "Use the steps below to connect instead."
        )

    if provider.extension:
        # Phase 2B vendors: checked inputs and vendor-shaped authorize URLs.
        return _start_extension(
            db, org=org, user=user, spec=spec, provider=provider,
            return_path=return_path, inputs=inputs or {},
        )

    # Answers to the connector's own sign-in inputs (the Microsoft Ads
    # sandbox toggle), kept with the state so the callback uses exactly these.
    checked = spec.oauth.checked_inputs(inputs) if spec.oauth.sign_in_inputs else {}  # type: ignore[union-attr]

    login_hint = ""
    # A ticked input changes what is asked for, so a sibling's sign-in is
    # not reused for it.
    if reuse and not any(checked.values()):
        donor = _reusable_sign_in(db, org=org, spec=spec, exclude=record.slug)
        if donor is not None:
            donor_record, donor_values = donor
            tokens = TokenSet(
                access_token=donor_values.get("accessToken", ""),
                refresh_token=donor_values.get("refreshToken", ""),
                expires_at=float(donor_values.get(ACCESS_TOKEN_EXPIRY_KEY) or 0),
                scopes=tuple(donor_record.scopes or ()),
                account=donor_values.get(OAUTH_ACCOUNT_KEY, ""),
            )
            write_platform_tokens(db, org=org, record=record, spec=spec, tokens=tokens)
            status = _auto_choose(db, org=org, user=user, record=record, spec=spec)
            return StartResult(status=status)
        login_hint = _sibling_account(db, org=org, spec=spec, exclude=record.slug)

    verifier, challenge = pkce_pair() if provider.pkce else ("", "")
    state = _new_state(
        db, org=org, user=user, slug=slug, provider=provider,
        return_path=return_path, verifier=verifier,
        context={"inputs": checked} if spec.oauth.sign_in_inputs else None,  # type: ignore[union-attr]
    )
    return StartResult(
        authorize_url=build_authorize_url(
            provider, spec, state=state, code_challenge=challenge, login_hint=login_hint,
            inputs=checked,
        )
    )


def _platform_siblings(
    db: Session, *, org: Organization, spec: ConnectorSpec, exclude: str
) -> list[tuple[ConnectorRecord, dict[str, str]]]:
    """Other connectors of the same provider connected by signing in."""
    from app.services import connectors as connector_service

    out: list[tuple[ConnectorRecord, dict[str, str]]] = []
    cipher = OrgCipher.for_org(org)
    for other in connector_service.list_records(db, tenant_id=org.id):
        if other.slug == exclude:
            continue
        other_spec = registry.get_spec(other.slug)
        if (
            other_spec is None
            or other_spec.oauth is None
            or other_spec.oauth.provider != spec.oauth.provider  # type: ignore[union-attr]
            or not from_platform_sign_in(other.credential_hints or {})
        ):
            continue
        values = connector_service.load_credentials(other, cipher).values
        if values.get("refreshToken") or values.get("accessToken"):
            out.append((other, values))
    return out


def _reusable_sign_in(
    db: Session, *, org: Organization, spec: ConnectorSpec, exclude: str
) -> tuple[ConnectorRecord, dict[str, str]] | None:
    """A sibling sign-in whose granted scopes already cover this connector.

    Never for a vendor whose refresh tokens are single-use (X): two
    connections holding one would invalidate each other on first refresh.
    """
    provider = oauth_providers.provider_for(spec)
    if provider is not None and provider.rotates_refresh_tokens:
        return None
    for other, values in _platform_siblings(db, org=org, spec=spec, exclude=exclude):
        if not missing_scopes(spec, tuple(other.scopes or ())) and other.scopes:
            return other, values
    return None


def _sibling_account(db: Session, *, org: Organization, spec: ConnectorSpec, exclude: str) -> str:
    for _, values in _platform_siblings(db, org=org, spec=spec, exclude=exclude):
        if values.get(OAUTH_ACCOUNT_KEY):
            return values[OAUTH_ACCOUNT_KEY]
    return ""


def sibling_accounts(db: Session, *, org: Organization) -> dict[str, str]:
    """``{provider: signed-in address}`` for this workspace's sign-ins, so
    the console can offer "Continue as …" on the next connector."""
    out: dict[str, str] = {}
    records = db.execute(
        select(ConnectorRecord).where(ConnectorRecord.tenant_id == org.id)
    ).scalars()
    for record in records:
        hints = record.credential_hints or {}
        spec = registry.get_spec(record.slug)
        account = hints.get(OAUTH_ACCOUNT_KEY, "")
        if spec and spec.oauth and account and from_platform_sign_in(hints):
            out.setdefault(spec.oauth.provider, account)
    return out


# ── Storing tokens ─────────────────────────────────────────────────────────
def write_platform_tokens(
    db: Session,
    *,
    org: Organization,
    record: ConnectorRecord,
    spec: ConnectorSpec,
    tokens: TokenSet,
) -> None:
    """Store a sign-in's tokens on the connector, ahead of the choose step.

    The record's ``connected`` flag is left as it is: a new connection is not
    live until something has been chosen and checked. What was already
    chosen (a property, a site) is kept, so signing in again keeps working
    without picking again; the paste-it-yourself client id and secret are
    dropped, since the platform's app is now the client.
    """
    from app.services import connectors as connector_service

    cipher = OrgCipher.for_org(org)
    previous = connector_service.load_credentials(record, cipher).values
    binding = spec.oauth
    assert binding is not None
    dropped = binding.token_fields | binding.platform_fields
    kept = {
        k: v
        for k, v in (record.credential_hints or {}).items()
        if k not in dropped
        and k not in (OAUTH_SOURCE_KEY, OAUTH_ACCOUNT_KEY, SIGN_IN_EXPIRY_KEY)
        # What this sign-in says replaces what was there; an empty value
        # (an unticked sandbox toggle) clears it.
        and k not in tokens.extra
    }
    provider = oauth_providers.provider_for(spec)
    refresh = tokens.refresh_token or (
        previous.get("refreshToken", "")
        # Phase 2B sign-ins can change store or org between attempts, so an
        # earlier refresh token is never carried over to a new one.
        if from_platform_sign_in(previous) and not (provider and provider.extension)
        else ""
    )
    if not refresh and (provider is None or provider.refresh_required):
        label = provider.label if provider else "The vendor"
        raise OAuthFlowError(
            f"{label} did not grant lasting access, so the connection would stop "
            "working within the hour. Start again and allow access.",
            code=NO_LASTING_ACCESS,
        )
    if not tokens.access_token:
        raise OAuthFlowError(
            "The sign-in did not return access. Start again.", code=PROVIDER_REFUSED
        )
    secret_values = {
        "refreshToken": refresh,
        "accessToken": tokens.access_token,
        ACCESS_TOKEN_EXPIRY_KEY: str(int(tokens.expires_at)) if tokens.expires_at else "",
    }
    # Other secrets that belong to the customer, not to the sign-in, stay.
    secret_keys = {f.key for f in spec.fields if f.is_secret}
    for key in secret_keys - dropped:
        if previous.get(key):
            secret_values[key] = previous[key]

    record.credentials_encrypted = cipher.encrypt_json(
        {k: v for k, v in secret_values.items() if v}, context=Ctx.CONNECTOR_CREDENTIALS
    )
    record.credential_hints = {
        **kept,
        # Non-secret facts that came with the token (Phase 2B: the Salesforce
        # instance address, the Shopify store, the GitHub installation id).
        **{k: str(v) for k, v in tokens.extra.items() if isinstance(v, str) and v},
        OAUTH_SOURCE_KEY: PLATFORM_SOURCE,
        **({OAUTH_ACCOUNT_KEY: tokens.account} if tokens.account else {}),
        # A sign-in that cannot renew itself (Meta, LinkedIn without refresh)
        # says when it ends, so the console can ask for a new one in time.
        **(
            {SIGN_IN_EXPIRY_KEY: str(int(tokens.expires_at))}
            if not refresh and tokens.expires_at
            else {}
        ),
    }
    record.credentials_version += 1
    record.scopes = list(tokens.scopes)
    record.token_expires_at = (
        datetime.fromtimestamp(tokens.expires_at, UTC) if tokens.expires_at else None
    )
    db.flush()


# ── Choose ─────────────────────────────────────────────────────────────────
def _signed_in_instance(db: Session, *, org: Organization, record: ConnectorRecord):  # noqa: ANN202
    from app.services import connectors as connector_service

    credentials = connector_service.load_credentials(record, OrgCipher.for_org(org))
    if not (
        from_platform_sign_in(credentials.values)
        and (credentials.get("refreshToken") or credentials.get("accessToken"))
    ):
        raise InvalidInputError("Sign in first, then choose what to connect.")
    klass = registry.get_class(record.slug)
    if klass is None:
        raise ConnectorError("This connection is not available right now.")
    return klass(Credentials(values=dict(credentials.values)), org_id=str(org.id)), credentials


def list_choices(db: Session, *, org: Organization, slug: str) -> list[OAuthChoice]:
    from app.services import connectors as connector_service

    record = connector_service.get_record(db, tenant_id=org.id, slug=slug)
    _spec_or_404(slug)
    instance, _ = _signed_in_instance(db, org=org, record=record)
    try:
        with instance:
            return instance.list_oauth_choices()
    except ConnectorError as exc:
        from app.core.user_messages import soften_technical_message

        raise ConnectorError(
            soften_technical_message(instance.diagnose(exc) or str(exc))
        ) from exc


def choose(
    db: Session,
    *,
    org: Organization,
    actor: User,
    slug: str,
    choice_id: str,
    ip_address: str | None = None,
) -> ConnectorRecord:
    """Save one of :func:`list_choices` and verify the connection works.

    The choice is looked up again here rather than taken from the browser,
    so only something the signed-in account can actually reach is saved.
    """
    from app.services import connectors as connector_service

    record = connector_service.get_record(db, tenant_id=org.id, slug=slug)
    spec = _spec_or_404(slug)
    choices = list_choices(db, org=org, slug=slug)
    picked = next((c for c in choices if c.id == choice_id), None)
    if picked is None:
        raise InvalidInputError("That option is no longer available. Pick another.")
    return _finish(db, org=org, actor=actor, record=record, spec=spec, choice=picked, ip=ip_address)


def _finish(
    db: Session,
    *,
    org: Organization,
    actor: User,
    record: ConnectorRecord,
    spec: ConnectorSpec,
    choice: OAuthChoice,
    ip: str | None,
) -> ConnectorRecord:
    from app.connectors.base.credentials import apply_defaults, validate
    from app.services import audit
    from app.services import connectors as connector_service

    credentials = connector_service.load_credentials(record, OrgCipher.for_org(org))
    values = apply_defaults(spec.fields, {**credentials.values, **choice.values})
    # What was picked, in words, for "Connected · channel: #marketing" (the
    # stored value may be an opaque id). Non-secret.
    values[OAUTH_CHOICE_KEY] = choice.label[:200]
    validate(
        spec.fields,
        values,
        oauth_done=True,
        platform_supplied=connector_service.platform_supplied(spec, values),
    )
    report = connector_service.verify_credentials(
        slug=record.slug, values=values, oauth_completed=True, org_id=str(org.id)
    )
    connector_service.store_verified(
        db, org=org, actor=actor, record=record, values=values, report=report, oauth_completed=True
    )
    audit.record_user_action(
        db,
        user=actor,
        action=f"connected {record.name}",
        module="connectors",
        ip_address=ip,
        context={"connector": record.slug, "via": "sign-in", "choice": choice.label[:120]},
    )
    return record


def _auto_choose(
    db: Session, *, org: Organization, user: User, record: ConnectorRecord, spec: ConnectorSpec
) -> str:
    """Pick the only choice there is. ``connected`` or ``choose``.

    Anything that goes wrong here leaves the person on the choose step,
    where the same listing runs again and its error is shown in place.
    """
    try:
        choices = list_choices(db, org=org, slug=record.slug)
        if len(choices) == 1:
            _finish(db, org=org, actor=user, record=record, spec=spec, choice=choices[0], ip=None)
            return "connected"
    except Exception as exc:  # noqa: BLE001 - the choose step shows the real error
        log.info("%s: automatic choice not made (%s)", record.slug, type(exc).__name__)
    return "choose"


# ── Callback ───────────────────────────────────────────────────────────────
def _friendly_denial(provider: OAuthProvider, error: str) -> OAuthFlowError:
    if error == "access_denied":
        return OAuthFlowError(
            f"You cancelled the {provider.label} sign-in, so nothing was connected. "
            "You can try again whenever you are ready.",
            code=CANCELLED,
        )
    return OAuthFlowError(
        f"{provider.label} could not complete the sign-in. Try again.", code=PROVIDER_REFUSED
    )


def handle_callback(
    db: Session,
    provider_name: str,
    params: dict[str, str],
    raw: list[tuple[str, str]] | None = None,
) -> str:
    """Finish a sign-in and return the console URL to redirect to.

    Never raises: every failure is an ``oauth_error`` code from
    :data:`OAUTH_ERROR_CODES` on the console URL (never exception text), and
    the database work is committed only when it all succeeded.
    ``raw`` is the callback's full query, for vendors that sign it (Shopify).
    """
    provider = oauth_providers.get(provider_name)
    state_token = params.get("state", "")
    return_path = DEFAULT_RETURN
    slug = ""
    try:
        if provider is None:
            raise OAuthFlowError("That sign-in link is not recognised.", code=LINK_INVALID)
        payload = verify_state(state_token)
        return_path, slug = payload.return_path, payload.slug
        if payload.provider != provider.name:
            raise OAuthFlowError("That sign-in link is not recognised.", code=LINK_INVALID)

        from app.db.session import bind_tenant

        bind_tenant(db, payload.tenant_id)
        redeemed = _redeem_state(db, payload)
        # Commit the redemption even when what follows fails: a state is
        # spent the moment it is presented.
        db.commit()
        if redeemed is None:
            raise OAuthFlowError(
                "That sign-in link was already used or has expired. Start again "
                "from the connect screen.",
                code=LINK_EXPIRED,
            )

        org, user = _load_actor(db, payload)
        if provider.kind == "wordpress_app_password":
            status = _complete_wordpress(db, org=org, user=user, payload=payload,
                                         redeemed=redeemed, params=params)
        else:
            status = _complete_oauth(db, org=org, user=user, payload=payload,
                                     provider=provider, redeemed=redeemed, params=params,
                                     raw=raw)
        db.commit()
        if status == "connected":
            return console_url(return_path, connected=slug)
        return console_url(return_path, oauth_choose=slug)
    except OAuthFlowError as exc:
        db.rollback()
        log.info("Sign-in callback for %s ended: %s", slug or "-", exc.code)
        return console_url(return_path, oauth_error=exc.code, oauth_slug=slug)
    except AppError as exc:
        # Connector and validation errors: their wording is not sent on, only
        # the generic code; the connect screen shows the detail on retry.
        db.rollback()
        log_failure(log, "Sign-in callback could not finish", exc, slug=slug or "-")
        return console_url(return_path, oauth_error=CONNECT_FAILED, oauth_slug=slug)
    except Exception as exc:  # noqa: BLE001 - the browser gets a code, never a trace
        db.rollback()
        log.error("Sign-in callback for %s failed: %s", slug or "-", type(exc).__name__)
        return console_url(return_path, oauth_error=CONNECT_FAILED, oauth_slug=slug)


def _load_actor(db: Session, payload: StatePayload) -> tuple[Organization, User]:
    from app.core.rbac import Access, Module, access_for

    org = db.get(Organization, payload.tenant_id)
    user = db.get(User, payload.user_id)
    if (
        org is None
        or not org.is_active
        or user is None
        or not user.is_active
        or str(user.tenant_id) != payload.tenant_id
    ):
        raise OAuthFlowError(
            "Your session for this workspace has ended. Sign in again.", code=SESSION_ENDED
        )
    if access_for(user.role, Module.CONNECTORS) is not Access.FULL:
        raise OAuthFlowError(
            "Your role can no longer manage integrations in this workspace.", code=ROLE_CHANGED
        )
    return org, user


def _complete_oauth(
    db: Session,
    *,
    org: Organization,
    user: User,
    payload: StatePayload,
    provider: OAuthProvider,
    redeemed: RedeemedState,
    params: dict[str, str],
    raw: list[tuple[str, str]] | None = None,
) -> str:
    from app.services import connectors as connector_service

    if params.get("error"):
        raise _friendly_denial(provider, params["error"])
    # TikTok for Business sends the code as auth_code (and also as code).
    code = params.get("auth_code", "") or params.get("code", "")
    if not code:
        raise OAuthFlowError(
            f"{provider.label} did not send the sign-in back. Try again.", code=PROVIDER_REFUSED
        )

    spec = _spec_or_404(payload.slug)
    if not oauth_providers.sign_in_available(spec):
        raise OAuthFlowError(
            f"Sign in with {provider.label} is not set up on this platform.", code=NOT_SET_UP
        )
    verifier = ""
    if redeemed.verifier_encrypted:
        verifier = OrgCipher.for_org(org).decrypt(
            redeemed.verifier_encrypted, context=_PKCE_CONTEXT
        )
    wanted: tuple[str, ...] | None = None
    if provider.extension:
        # Phase 2B: the values checked at start come from the state row,
        # never from the callback; a signed callback is verified first.
        from app.services import oauth_business

        checked = {str(k): str(v) for k, v in (redeemed.context.get("vars") or {}).items()}
        oauth_business.verify_callback(
            provider, raw if raw is not None else list(params.items()), checked
        )
        tokens = oauth_business.exchange(
            provider, code=code, verifier=verifier, vars_=checked, params=params
        )
    else:
        tokens = exchange_code(provider, code=code, verifier=verifier)
        # The connector's own sign-in inputs, as checked at start (never
        # from the callback): what they asked for, and what they set on
        # the connection (Microsoft Ads: accountEnvironment=sandbox).
        checked_inputs = redeemed.context.get("inputs")
        if isinstance(checked_inputs, dict) and spec.oauth is not None:
            checked_inputs = {str(k): str(v) for k, v in checked_inputs.items()}
            wanted = spec.oauth.scopes_for_inputs(checked_inputs)
            tokens.extra.update(spec.oauth.values_for_inputs(checked_inputs))

    # Optional scopes (post insights, X images) may be declined: the
    # connection works without them and says to reconnect for that feature.
    absent = oauth_providers.required_only(spec, missing_scopes(spec, tokens.scopes, wanted))
    if absent:
        raise OAuthFlowError(
            f"{spec.name} needs the permission you left unticked on the {provider.label} "
            "screen. Sign in again and tick every box.",
            code=PERMISSIONS_MISSING,
        )

    record = connector_service.get_record(db, tenant_id=org.id, slug=payload.slug)
    write_platform_tokens(db, org=org, record=record, spec=spec, tokens=tokens)
    return _auto_choose(db, org=org, user=user, record=record, spec=spec)


# ── Phase 2B: business-app sign-ins (see app.services.oauth_business) ──────
def _start_extension(
    db: Session,
    *,
    org: Organization,
    user: User,
    spec: ConnectorSpec,
    provider: OAuthProvider,
    return_path: str,
    inputs: dict[str, str],
) -> StartResult:
    """Start a sign-in whose vendor needs inputs or its own authorize URL.

    The inputs are checked here (a malformed Shopify address is refused
    before anything leaves) and the checked values are stored with the
    state, so the callback uses them rather than anything sent back.
    """
    from app.services import oauth_business

    checked = oauth_business.prepare(provider, inputs)
    verifier, challenge = pkce_pair() if provider.pkce else ("", "")
    state = _new_state(
        db, org=org, user=user, slug=spec.slug, provider=provider,
        return_path=return_path, verifier=verifier, context={"vars": checked},
    )
    return StartResult(
        authorize_url=oauth_business.authorize_url(
            provider, spec, state=state, code_challenge=challenge, vars_=checked
        )
    )


# ── WordPress: application-password approval ───────────────────────────────
def normalise_site(raw: str) -> str:
    """``https://host[/path]`` from whatever was typed. Refuses plain http."""
    site = (raw or "").strip().rstrip("/")
    if not site:
        raise InvalidInputError("Enter your website address.")
    if "://" not in site:
        site = "https://" + site
    parts = urlsplit(site)
    if parts.scheme.lower() == "http":
        raise InvalidInputError(
            "WordPress only allows this kind of connection on a secure address "
            "(one starting with https://). Turn on HTTPS for the site — most "
            "hosts offer it free — and try again."
        )
    if parts.scheme.lower() != "https" or not parts.hostname:
        raise InvalidInputError("Enter your website address, like https://yourcompany.com")
    return urlunsplit(("https", parts.netloc.lower(), parts.path.rstrip("/"), "", ""))


def _same_site(a: str, b: str) -> bool:
    def host(url: str) -> str:
        name = (urlsplit(url).hostname or "").lower()
        return name[4:] if name.startswith("www.") else name

    return bool(host(a)) and host(a) == host(b)


def discover_wordpress_authorize(site: str) -> str:
    """The site's "Authorize application" page.

    Read from the REST API index (``authentication.application-passwords``)
    where the site publishes it, which also tells us when a site has
    application passwords switched off. Falls back to the standard path when
    the index cannot be read — some hosts block it, and the approval page
    still works.
    """
    from app.core.netguard import check_public_url, safe_get

    check_public_url(site)
    fallback = f"{site}/wp-admin/authorize-application.php"
    try:
        response = safe_get(f"{site}/wp-json/", timeout=10.0, max_bytes=1_500_000)
        index = response.json() if response.status_code == 200 else None
    except (httpx.HTTPError, ValueError, InvalidInputError):
        return fallback
    if not isinstance(index, dict) or "namespaces" not in index:
        return fallback
    endpoint = (
        ((index.get("authentication") or {}).get("application-passwords") or {})
        .get("endpoints", {})
        .get("authorization")
    )
    if not endpoint:
        raise InvalidInputError(
            "This WordPress site has application passwords switched off, so it "
            "cannot be approved from here. They need WordPress 5.6 or newer, and "
            "some security plugins turn them off — ask whoever manages the site."
        )
    endpoint = str(endpoint)
    if not endpoint.startswith("https://") or not _same_site(endpoint, site):
        return fallback
    return endpoint


def _wordpress_callback_url(state: str, **extra: str) -> str:
    provider = oauth_providers.WORDPRESS
    return f"{provider.redirect_uri}?{urlencode({'state': state, **extra})}"


def _start_wordpress(
    db: Session,
    *,
    org: Organization,
    user: User,
    spec: ConnectorSpec,
    provider: OAuthProvider,
    return_path: str,
    site_url: str,
) -> StartResult:
    site = normalise_site(site_url)
    authorize = discover_wordpress_authorize(site)
    state = _new_state(
        db, org=org, user=user, slug=spec.slug, provider=provider,
        return_path=return_path, context={"site": site},
    )
    params = {
        "app_name": settings.app_name,
        "app_id": settings.wordpress_app_id,
        "success_url": _wordpress_callback_url(state),
        "reject_url": _wordpress_callback_url(state, rejected="1"),
    }
    return StartResult(authorize_url=f"{authorize}?{urlencode(params)}")


def _complete_wordpress(
    db: Session,
    *,
    org: Organization,
    user: User,
    payload: StatePayload,
    redeemed: RedeemedState,
    params: dict[str, str],
) -> str:
    from app.core.netguard import check_public_url
    from app.services import connectors as connector_service

    if params.get("rejected") == "1" or params.get("success") == "false":
        raise OAuthFlowError(
            "You chose not to approve the connection in WordPress, so nothing "
            "was connected.",
            code=CANCELLED,
        )
    expected = str(redeemed.context.get("site") or "")
    returned = (params.get("site_url") or "").strip().rstrip("/")
    login = (params.get("user_login") or "").strip()
    password = (params.get("password") or "").strip()
    if not (returned and login and password):
        raise OAuthFlowError(
            "WordPress did not send the approval back. Try again.", code=PROVIDER_REFUSED
        )
    if not returned.lower().startswith("https://"):
        raise OAuthFlowError(
            "WordPress reported a non-secure (http://) address for the site. Set "
            "the WordPress Address to https:// under Settings → General, then try again.",
            code=SITE_NOT_SECURE,
        )
    if not expected or not _same_site(returned, expected):
        raise OAuthFlowError(
            "The approval came back from a different website than the one you "
            "entered. Start again with the address of the site you approved.",
            code=SITE_MISMATCH,
        )
    check_public_url(returned)

    # Verified with the site, encrypted and audited exactly as a typed-in
    # application password would be.
    connector_service.connect(
        db,
        org=org,
        actor=user,
        slug=payload.slug,
        values={"siteUrl": returned, "username": login, "applicationPassword": password},
    )
    return "connected"


# ── Admin ──────────────────────────────────────────────────────────────────
@dataclass(slots=True)
class ProviderStatus:
    name: str
    label: str
    configured: bool
    redirect_uri: str
    settings_keys: list[str]
    missing: list[str]
    connectors: list[dict]
    console_url: str
    note: str = ""
    # ── Setup wizard (Portal Admin) ───────────────────────────────────────
    #: ready | partial | not_set_up | built_in (WordPress: nothing to set up)
    status: str = "not_set_up"
    #: One sentence under the status when something is still to do.
    status_detail: str = ""
    console_label: str = ""
    steps: list[str] = field(default_factory=list)
    #: The permissions to switch on in the vendor's console.
    permissions: list[str] = field(default_factory=list)
    permissions_where: str = ""
    review_note: str = ""
    fields: list[dict] = field(default_factory=list)
    #: Something is saved in Portal Admin (so there is something to remove).
    saved_here: bool = False
    updated_by: str = ""
    updated_at: str = ""
    #: The vendor can be asked whether the credentials are right.
    checkable: bool = False


#: Not standard OAuth scopes worth listing to an admin.
_IDENTITY_SCOPES = {"openid", "email", "offline_access", "offline.access", "oauth", "refresh_token"}


def provider_statuses() -> list[ProviderStatus]:
    """Portal Admin's view: per provider, whether it is set up, the steps
    and exact redirect URI to register with the vendor, the values to enter
    (with a preview, never a secret) and which integrations it unlocks.

    Ordered by what matters most to a marketing team: Google, Meta,
    LinkedIn, Microsoft, then the rest; WordPress (nothing to set up) last.
    """
    from app.services import platform_oauth_apps, provider_setup

    snap = platform_oauth_apps.snapshot()
    labels = {key.upper(): f.label for key, (_, f) in provider_setup.field_index().items()}
    out: list[ProviderStatus] = []
    for provider in oauth_providers.PROVIDERS.values():
        setup = provider_setup.setup_for(provider.name)
        connectors = []
        scopes: list[str] = []
        for spec in registry.all_specs():
            if spec.oauth is None or spec.oauth.provider != provider.name:
                continue
            spec_scopes = list(oauth_providers.scopes_for(spec))
            scopes.extend(spec_scopes)
            missing = oauth_providers.missing_settings(spec)
            connectors.append(
                {
                    "slug": spec.slug,
                    "name": spec.name,
                    "available": oauth_providers.sign_in_available(spec),
                    "missing": missing,
                    # The same, as the setup form labels them.
                    "needs": list(dict.fromkeys(labels.get(k, k) for k in missing)),
                    "scopes": spec_scopes,
                }
            )
        note = ""
        status = "ready" if provider.configured else "not_set_up"
        detail = ""
        if provider.kind == "wordpress_app_password":
            status = "built_in"
            note = (
                "Nothing to register: each site owner approves access on their own "
                "site. WordPress requires this callback address to be https."
            )
        elif provider.configured:
            waiting = [c["name"] for c in connectors if not c["available"]]
            if waiting:
                status = "partial"
                needs = sorted(
                    {
                        f.label
                        for f in (setup.extras if setup else ())
                        if not oauth_providers.platform_setting(f.key)
                    }
                )
                detail = (
                    f"{', '.join(waiting)} also needs the {' and '.join(needs) or 'remaining values'}."
                )
        fields = (
            [platform_oauth_apps.field_status(f, snap) for f in setup.fields] if setup else []
        )
        updated_by, updated_at = snap.meta.get(provider.name, ("", None))
        out.append(
            ProviderStatus(
                name=provider.name,
                label=provider.label,
                configured=provider.configured,
                redirect_uri=provider.redirect_uri,
                settings_keys=list(provider.settings_keys),
                missing=[] if provider.configured else list(provider.settings_keys),
                connectors=connectors,
                console_url=provider.console_url,
                note=note,
                status=status,
                status_detail=detail,
                console_label=setup.console_label if setup else "",
                steps=list(setup.steps) if setup else [],
                permissions=(
                    list(setup.app_permissions)
                    if setup and setup.app_permissions
                    else [s for s in dict.fromkeys(scopes) if s not in _IDENTITY_SCOPES]
                ),
                permissions_where=setup.permissions_where if setup else "",
                review_note=setup.review_note if setup else "",
                fields=fields,
                saved_here=provider.name in snap.meta,
                updated_by=updated_by,
                updated_at=updated_at.isoformat() if updated_at else "",
                checkable=provider.name in platform_oauth_apps.CHECKS,
            )
        )
    rank = {name: s.rank for name, s in provider_setup.SETUPS.items()}
    out.sort(key=lambda p: rank.get(p.name, 10_000))
    return out


__all__ = [
    "CONNECT_FAILED",
    "DEFAULT_RETURN",
    "OAUTH_ERROR_CODES",
    "OAuthFlowError",
    "StatePayload",
    "TokenSet",
    "build_authorize_url",
    "choose",
    "console_url",
    "error_code",
    "exchange_code",
    "handle_callback",
    "list_choices",
    "pkce_pair",
    "provider_statuses",
    "safe_return_path",
    "sign_state",
    "start",
    "verify_state",
]
