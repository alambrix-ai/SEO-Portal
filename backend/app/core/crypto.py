"""Cryptography for data at rest.

The scheme is envelope encryption with two tiers, both AES-256-GCM:

**Tier 1 — global column keys.** Derived from the master key-encrypting key
(KEK) with HKDF, one key per logical column (``users.email``,
``invitations.email``, ...). Used for fields the platform must read or match
*before* an organisation context exists — logging in, accepting an invite.

**Tier 2 — per-organisation data keys.** Every organisation gets a random
32-byte DEK at signup. The DEK is stored only in wrapped form (AES-GCM under
the KEK) and is unwrapped in memory for the life of a request. Everything that
belongs to one customer — connector credentials, page bodies, outreach copy —
is encrypted under that customer's own key, so a stolen database row is
useless without both the KEK and the wrapped DEK, and revoking one
organisation's key cannot touch another's data.

Every ciphertext is bound to where it lives with additional authenticated data
(AAD), so a row cannot be replayed into a different column, organisation, or
record. Tokens carry their key version, which is what makes rotation possible
without a flag-day migration.

Deterministic equality lookups on encrypted columns go through
:func:`blind_index` — an HMAC under a key that is deliberately *not* the KEK,
so leaking the index key reveals no plaintext.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import unicodedata

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from app.core.config import settings

_TOKEN_PREFIX = "v1"
_NONCE_BYTES = 12
_KEY_BYTES = 32
_SEP = ":"


class CryptoError(Exception):
    """Raised when a payload cannot be decrypted or a key is unusable."""


# ── Base64 helpers (url-safe, unpadded, so tokens stay copy-pasteable) ─────
def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


# ── Key ring ───────────────────────────────────────────────────────────────
class KeyRing:
    """The master keys, indexed by version.

    In development an absent ``MASTER_ENCRYPTION_KEY`` is derived from
    ``SECRET_KEY`` so the platform runs after a clone; production refuses to
    start without an explicit key (see ``Settings._production_guardrails``).
    """

    def __init__(self) -> None:
        self._keys: dict[int, bytes] = {}
        self._active_version: int = settings.master_key_version

        primary = settings.master_encryption_key.strip()
        if primary:
            key = self._decode_key(primary, "MASTER_ENCRYPTION_KEY")
        else:
            key = hashlib.sha256(
                b"automarket:derived-kek:" + settings.secret_key.encode()
            ).digest()
        self._keys[self._active_version] = key

        for entry in settings.master_encryption_key_retired:
            version_str, _, encoded = entry.partition(_SEP)
            if not encoded:
                raise CryptoError(
                    "MASTER_ENCRYPTION_KEY_RETIRED entries must look like '<version>:<base64key>'"
                )
            self._keys[int(version_str)] = self._decode_key(
                encoded, f"retired key v{version_str}"
            )

    @staticmethod
    def _decode_key(encoded: str, label: str) -> bytes:
        try:
            raw = base64.b64decode(encoded, validate=True)
        except Exception as exc:  # noqa: BLE001 - surfaced as a config error
            raise CryptoError(f"{label} is not valid base64") from exc
        if len(raw) != _KEY_BYTES:
            raise CryptoError(f"{label} must decode to exactly {_KEY_BYTES} bytes")
        return raw

    @property
    def active_version(self) -> int:
        return self._active_version

    def key(self, version: int) -> bytes:
        try:
            return self._keys[version]
        except KeyError as exc:
            raise CryptoError(
                f"No master key for version {version}; add it to "
                "MASTER_ENCRYPTION_KEY_RETIRED to read rows written under it"
            ) from exc

    def column_key(self, label: str, version: int | None = None) -> bytes:
        """HKDF-derive the tier-1 key for one logical column."""
        v = self.active_version if version is None else version
        return HKDF(
            algorithm=hashes.SHA256(),
            length=_KEY_BYTES,
            salt=None,
            info=f"automarket:column:{label}".encode(),
        ).derive(self.key(v))


_keyring: KeyRing | None = None


def keyring() -> KeyRing:
    global _keyring
    if _keyring is None:
        _keyring = KeyRing()
    return _keyring


def reset_keyring() -> None:
    """Drop the cached key ring — used by tests and after key rotation."""
    global _keyring
    _keyring = None


# ── AEAD primitives ────────────────────────────────────────────────────────
def _encrypt(key: bytes, plaintext: bytes, aad: bytes, version: int) -> str:
    nonce = os.urandom(_NONCE_BYTES)
    ct = AESGCM(key).encrypt(nonce, plaintext, aad)
    return _SEP.join([_TOKEN_PREFIX, str(version), _b64e(nonce), _b64e(ct)])


def _parse(token: str) -> tuple[int, bytes, bytes]:
    try:
        prefix, version, nonce, ct = token.split(_SEP, 3)
    except ValueError as exc:
        raise CryptoError("Malformed ciphertext token") from exc
    if prefix != _TOKEN_PREFIX:
        raise CryptoError(f"Unsupported ciphertext format {prefix!r}")
    return int(version), _b64d(nonce), _b64d(ct)


def _decrypt(key_for_version, token: str, aad: bytes) -> bytes:  # noqa: ANN001
    version, nonce, ct = _parse(token)
    try:
        return AESGCM(key_for_version(version)).decrypt(nonce, ct, aad)
    except InvalidTag as exc:
        raise CryptoError(
            "Ciphertext failed authentication — wrong key, or the row was "
            "moved or tampered with"
        ) from exc


def is_ciphertext(value: str | None) -> bool:
    return bool(value) and value.startswith(_TOKEN_PREFIX + _SEP)


# ── Tier 1: global column encryption ───────────────────────────────────────
def encrypt_column(label: str, plaintext: str) -> str:
    """Encrypt a value under the derived key for column ``label``."""
    if plaintext is None:
        raise CryptoError("Cannot encrypt None")
    ring = keyring()
    return _encrypt(
        ring.column_key(label),
        plaintext.encode(),
        aad=f"col:{label}".encode(),
        version=ring.active_version,
    )


def decrypt_column(label: str, token: str) -> str:
    ring = keyring()
    return _decrypt(
        lambda v: ring.column_key(label, v), token, aad=f"col:{label}".encode()
    ).decode()


# ── Tier 2: per-organisation data keys ─────────────────────────────────────
def generate_dek() -> bytes:
    return os.urandom(_KEY_BYTES)


def wrap_dek(dek: bytes, org_id: str) -> str:
    """Encrypt an organisation's data key under the active master key."""
    if len(dek) != _KEY_BYTES:
        raise CryptoError("A data key must be exactly 32 bytes")
    ring = keyring()
    return _encrypt(
        ring.key(ring.active_version),
        dek,
        aad=f"dek:{org_id}".encode(),
        version=ring.active_version,
    )


def unwrap_dek(wrapped: str, org_id: str) -> bytes:
    ring = keyring()
    return _decrypt(ring.key, wrapped, aad=f"dek:{org_id}".encode())


def rewrap_dek(wrapped: str, org_id: str) -> str:
    """Re-encrypt a data key under the current master key version."""
    return wrap_dek(unwrap_dek(wrapped, org_id), org_id)


def encrypt_for_org(dek: bytes, plaintext: str, *, context: str, org_id: str) -> str:
    """Encrypt tenant data. ``context`` names the field, e.g. ``connector.credentials``."""
    ring = keyring()
    return _encrypt(
        dek,
        plaintext.encode(),
        aad=f"org:{org_id}|ctx:{context}".encode(),
        version=ring.active_version,
    )


def decrypt_for_org(dek: bytes, token: str, *, context: str, org_id: str) -> str:
    return _decrypt(
        lambda _v: dek, token, aad=f"org:{org_id}|ctx:{context}".encode()
    ).decode()


# ── Deterministic lookup of encrypted columns ──────────────────────────────
def _blind_index_key() -> bytes:
    configured = settings.blind_index_key.strip()
    if configured:
        try:
            raw = base64.b64decode(configured, validate=True)
        except Exception:  # noqa: BLE001 - accept a raw passphrase too
            raw = configured.encode()
        return hashlib.sha256(raw).digest()
    return hashlib.sha256(
        b"automarket:derived-blind-index:" + settings.secret_key.encode()
    ).digest()


def normalize_email(email: str) -> str:
    return unicodedata.normalize("NFKC", email).strip().lower()


def blind_index(value: str, *, domain: str) -> str:
    """Deterministic, non-reversible index for equality search.

    ``domain`` separates namespaces so the same string in two columns does not
    produce the same index.
    """
    digest = hmac.new(
        _blind_index_key(), f"{domain}|{value}".encode(), hashlib.sha256
    ).hexdigest()
    return digest[:44]


def email_index(email: str) -> str:
    return blind_index(normalize_email(email), domain="email")


# ── Opaque tokens (verification links, refresh tokens, invites) ────────────
def new_url_token(nbytes: int = 32) -> str:
    """A high-entropy token safe to put in a URL. Returned to the user once."""
    return _b64e(os.urandom(nbytes))


def hash_token(token: str) -> str:
    """Store only this. Comparison is constant-time via :func:`verify_token`."""
    return hmac.new(_blind_index_key(), token.encode(), hashlib.sha256).hexdigest()


def verify_token(token: str, stored_hash: str) -> bool:
    return hmac.compare_digest(hash_token(token), stored_hash)


def fingerprint(value: str, *, length: int = 12) -> str:
    """Short non-reversible label for logs and audit rows."""
    return hashlib.sha256(value.encode()).hexdigest()[:length]
