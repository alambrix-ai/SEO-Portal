"""Credential field declarations and the values behind them.

Each connector declares the fields its console dialog should render. The same
declaration drives three things, so they cannot fall out of step:

* the **form** the Connectors marketplace shows,
* **validation** of what the operator submitted,
* which values are **secret** — secrets are encrypted under the organisation's
  key and never returned to the browser, while non-secret values (account ids,
  hostnames) are kept as readable hints so the console can show what is wired
  up.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class FieldKind(StrEnum):
    TEXT = "text"
    PASSWORD = "password"
    NUMBER = "number"
    URL = "url"
    OAUTH = "oauth"
    #: An AI model, picked from the vendor's live list (see app.llm.model_catalog).
    MODEL = "model"


@dataclass(frozen=True, slots=True)
class CredentialField:
    key: str
    label: str = ""
    kind: FieldKind = FieldKind.TEXT
    placeholder: str = ""
    required: bool = True
    help_text: str = ""
    # OAuth "buttons" are rendered instead of an input.
    oauth_label: str = ""
    #: Shown in the connect form when the workspace has no saved hint yet.
    default: str = ""
    #: For MODEL fields: which models the list offers, ``text`` or ``image``.
    model_kind: str = ""

    @property
    def is_oauth(self) -> bool:
        return self.kind is FieldKind.OAUTH

    @property
    def is_secret(self) -> bool:
        return self.kind is FieldKind.PASSWORD

    def to_public(self) -> dict[str, Any]:
        """Shape the console renders. Never carries a value."""
        return {
            "key": self.key,
            "label": self.label,
            "type": self.kind.value,
            "placeholder": self.placeholder,
            "required": self.required,
            "help_text": self.help_text,
            "is_oauth": self.is_oauth,
            "oauth_label": self.oauth_label,
            "default": self.default,
            "model_kind": self.model_kind,
        }


def oauth(label: str, key: str = "oauth") -> CredentialField:
    return CredentialField(key=key, kind=FieldKind.OAUTH, oauth_label=label, required=True)


def secret(key: str, label: str, placeholder: str = "", **kw: Any) -> CredentialField:  # noqa: ANN401
    return CredentialField(
        key=key, label=label, kind=FieldKind.PASSWORD, placeholder=placeholder, **kw
    )


def text(key: str, label: str, placeholder: str = "", **kw: Any) -> CredentialField:  # noqa: ANN401
    return CredentialField(key=key, label=label, placeholder=placeholder, **kw)


def number(key: str, label: str, placeholder: str = "", **kw: Any) -> CredentialField:  # noqa: ANN401
    return CredentialField(
        key=key, label=label, kind=FieldKind.NUMBER, placeholder=placeholder, **kw
    )


def model(key: str, label: str, *, model_kind: str = "text", **kw: Any) -> CredentialField:  # noqa: ANN401
    """An AI model chosen from the vendor's own list. Never has a default."""
    return CredentialField(
        key=key, label=label, kind=FieldKind.MODEL, model_kind=model_kind, **kw
    )


def apply_defaults(
    fields: tuple[CredentialField, ...], values: dict[str, str]
) -> dict[str, str]:
    """Fill blank optional (and empty) fields with their declared defaults."""
    out = dict(values)
    for declared in fields:
        if declared.is_oauth or declared.is_secret:
            continue
        current = (out.get(declared.key) or "").strip()
        if not current and declared.default:
            out[declared.key] = declared.default
    return out


def monthly_budget(key: str = "monthlyBudget") -> CredentialField:
    """The planned monthly spend on an ad platform.

    Labelled in the deployment's own currency rather than "(USD)", which is
    what six of these said while every figure the console renders beside them
    was in rupees. None of the platforms report a plan-level monthly budget,
    so this is the operator telling us what theirs is — mislabel the currency
    and the pacing maths is out by a factor of eighty-eight.
    """
    from app.core.config import settings

    return CredentialField(
        key=key,
        label=f"Monthly budget ({settings.currency_symbol})",
        kind=FieldKind.NUMBER,
        placeholder="1000000",
        required=False,
        help_text=(
            "What this platform is budgeted for each month. Used for pacing "
            "and for the share-of-spend split; leave it blank if you set "
            "budgets in the platform itself."
        ),
    )


@dataclass(slots=True)
class Credentials:
    """Decrypted credential values for one connector installation."""

    values: dict[str, str] = field(default_factory=dict)
    oauth_completed: bool = False

    def get(self, key: str, default: str = "") -> str:
        return self.values.get(key, default) or default

    def require(self, key: str) -> str:
        """A credential that must be present. Raises rather than guessing.

        The error is a :class:`ConnectorConfigError`, which the per-item
        error handling around the codebase deliberately does not swallow: a
        missing credential fails every item the same way, and absorbing it
        once per item turns a setup problem into an empty result that looks
        like a finding.
        """
        value = self.values.get(key)
        if not value:
            from app.core.exceptions import ConnectorConfigError

            raise ConnectorConfigError(
                f"Missing credential {key!r} — reconnect this integration"
            )
        return value

    def __contains__(self, key: str) -> bool:
        return bool(self.values.get(key))

    def __repr__(self) -> str:  # pragma: no cover
        # Values are secrets; only the key names are safe to print.
        return f"Credentials(keys={sorted(self.values)}, oauth={self.oauth_completed})"

    __str__ = __repr__


def split_hints(fields: tuple[CredentialField, ...], values: dict[str, str]) -> dict[str, str]:
    """The non-secret subset, safe to store in the clear and show back."""
    secret_keys = {f.key for f in fields if f.is_secret}
    return {k: v for k, v in values.items() if k not in secret_keys and v}


#: Non-secret marker stored with a connection made through the platform's own
#: sign-in ("Sign in with Google"). Its presence means the OAuth client id and
#: secret are the platform's — read from settings, never stored per workspace.
OAUTH_SOURCE_KEY = "oauthSource"
PLATFORM_SOURCE = "platform"
#: The account that signed in (an email address), shown back as
#: "Signed in as …" and used to pre-select the same account next time.
OAUTH_ACCOUNT_KEY = "oauthAccount"
#: Secret: the expiry (unix seconds) of the stored access token.
ACCESS_TOKEN_EXPIRY_KEY = "accessTokenExpiry"
#: Non-secret: when a sign-in that cannot renew itself ends (unix seconds) —
#: a Meta or LinkedIn token with no refresh token. Absent when it renews.
SIGN_IN_EXPIRY_KEY = "oauthExpiresAt"


def from_platform_sign_in(values: dict[str, str]) -> bool:
    """True when these credentials came from the platform sign-in flow."""
    return (values.get(OAUTH_SOURCE_KEY) or "") == PLATFORM_SOURCE


def validate(
    fields: tuple[CredentialField, ...],
    values: dict[str, str],
    *,
    oauth_done: bool,
    platform_supplied: frozenset[str] = frozenset(),
) -> None:
    """Raise when a required field or OAuth step is missing.

    ``platform_supplied`` names fields the platform fills in itself — the
    OAuth client id and secret (and, for Google Ads, the developer token)
    when the connection came from the platform's sign-in.
    """
    from app.core.exceptions import InvalidInputError

    missing: list[str] = []
    for f in fields:
        if not f.required or f.key in platform_supplied:
            continue
        if f.is_oauth:
            if not oauth_done:
                missing.append(f.oauth_label or "authorisation")
            continue
        if not (values.get(f.key) or "").strip():
            missing.append(f.label or f.key)
    if missing:
        raise InvalidInputError("Complete these first: " + ", ".join(missing))
