"""Column types that encrypt on the way in and decrypt on the way out.

These cover *tier-1* fields — the ones the platform must read before an
organisation context exists (a login email, an invite address). They are
transparent to the rest of the code: a model declares
``mapped_column(EncryptedString("users.email"))`` and reads a plain ``str``.

Tenant payloads use tier-2 per-organisation keys instead and go through
``app.services.encryption.OrgCipher``, because a column type has no way to
know which organisation's key a row belongs to.

Equality search on an encrypted column is impossible by design (the same
plaintext encrypts differently every time), so pair one with a
:func:`app.core.crypto.blind_index` column and query on that.
"""
from __future__ import annotations

import json
from typing import Any

from sqlalchemy import String, Text
from sqlalchemy.types import TypeDecorator

from app.core.crypto import CryptoError, decrypt_column, encrypt_column, is_ciphertext


class EncryptedString(TypeDecorator):
    """AES-256-GCM encrypted text, bound to the column label given."""

    impl = Text
    cache_ok = True

    def __init__(self, label: str, **kwargs: Any) -> None:
        if not label or "." not in label:
            raise ValueError("EncryptedString needs a 'table.column' label for its AAD")
        self.label = label
        super().__init__(**kwargs)

    def process_bind_param(self, value: str | None, dialect: Any) -> str | None:  # noqa: ANN401
        if value is None:
            return None
        # Already-encrypted values pass through so a re-save is idempotent.
        if is_ciphertext(value):
            return value
        return encrypt_column(self.label, value)

    def process_result_value(self, value: str | None, dialect: Any) -> str | None:  # noqa: ANN401
        if value is None:
            return None
        if not is_ciphertext(value):
            # Plaintext left by a migration that has not been backfilled yet.
            return value
        return decrypt_column(self.label, value)

    def copy(self, **kw: Any) -> EncryptedString:
        return EncryptedString(self.label, **kw)


class EncryptedJSON(TypeDecorator):
    """A JSON document stored as one encrypted blob.

    Use where the *contents* are sensitive and never need to be queried in
    SQL; use ``JSONB`` where the shape matters to the database.
    """

    impl = Text
    cache_ok = True

    def __init__(self, label: str, **kwargs: Any) -> None:
        if not label or "." not in label:
            raise ValueError("EncryptedJSON needs a 'table.column' label for its AAD")
        self.label = label
        super().__init__(**kwargs)

    def process_bind_param(self, value: Any, dialect: Any) -> str | None:  # noqa: ANN401
        if value is None:
            return None
        return encrypt_column(self.label, json.dumps(value, separators=(",", ":")))

    def process_result_value(self, value: str | None, dialect: Any) -> Any:  # noqa: ANN401
        if value is None:
            return None
        try:
            raw = decrypt_column(self.label, value) if is_ciphertext(value) else value
            return json.loads(raw)
        except (CryptoError, json.JSONDecodeError):
            return None

    def copy(self, **kw: Any) -> EncryptedJSON:
        return EncryptedJSON(self.label, **kw)


class BlindIndex(TypeDecorator):
    """Storage for a deterministic HMAC index. Values are set by the caller."""

    impl = String(44)
    cache_ok = True
