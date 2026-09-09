"""Per-organisation encryption (tier 2).

Every organisation owns a 32-byte data-encryption key (DEK). The DEK is stored
only wrapped under the master key, and it is unwrapped into an
:class:`OrgCipher` for the life of a request or an agent run — never held
beyond it, never written anywhere.

Use this for customer *payloads*: page bodies, outreach copy, ad copy,
connector credentials, pending approval changes. Small identity fields use the
transparent column types in :mod:`app.db.types` instead.

    cipher = OrgCipher.for_org(org)
    row.body_encrypted = cipher.encrypt(body, context="seo_page.body")
    body = cipher.decrypt(row.body_encrypted, context="seo_page.body")

``context`` is authenticated, not just decorative: a ciphertext written for
``seo_page.body`` will not decrypt as ``connector.credentials``, so rows cannot
be shuffled between columns to change what the application reads.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from app.core.crypto import (
    CryptoError,
    decrypt_for_org,
    encrypt_for_org,
    generate_dek,
    is_ciphertext,
    rewrap_dek,
    unwrap_dek,
    wrap_dek,
)
from app.core.logging import get_logger

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class OrgCipher:
    """An unwrapped organisation key, bound to that organisation's id."""

    org_id: str
    _dek: bytes

    # ── Construction ───────────────────────────────────────────────────────
    @classmethod
    def for_org(cls, org: Any) -> OrgCipher:  # noqa: ANN401 - Organization model
        """Unwrap the DEK held on an ``Organization`` row."""
        if not org.wrapped_dek:
            raise CryptoError(
                f"Organisation {org.id} has no data key; it was not provisioned correctly"
            )
        return cls(org_id=str(org.id), _dek=unwrap_dek(org.wrapped_dek, str(org.id)))

    @staticmethod
    def provision(org_id: str) -> str:
        """Generate a fresh DEK for a new organisation and return it wrapped."""
        return wrap_dek(generate_dek(), org_id)

    @staticmethod
    def rewrap(wrapped: str, org_id: str) -> str:
        """Re-wrap an existing DEK under the current master key version."""
        return rewrap_dek(wrapped, org_id)

    # ── Text ───────────────────────────────────────────────────────────────
    def encrypt(self, plaintext: str | None, *, context: str) -> str:
        if plaintext is None or plaintext == "":
            return ""
        return encrypt_for_org(self._dek, plaintext, context=context, org_id=self.org_id)

    def decrypt(self, token: str | None, *, context: str, default: str = "") -> str:
        if not token:
            return default
        if not is_ciphertext(token):
            # Plaintext predating encryption, or a backfill in progress.
            return token
        try:
            return decrypt_for_org(self._dek, token, context=context, org_id=self.org_id)
        except CryptoError:
            log.warning(
                "Undecryptable %s payload for org %s — returning default", context, self.org_id
            )
            return default

    # ── JSON documents ─────────────────────────────────────────────────────
    def encrypt_json(self, document: Any, *, context: str) -> str:  # noqa: ANN401
        if document is None:
            return ""
        return self.encrypt(json.dumps(document, separators=(",", ":"), default=str), context=context)

    def decrypt_json(self, token: str | None, *, context: str, default: Any = None) -> Any:  # noqa: ANN401
        raw = self.decrypt(token, context=context)
        if not raw:
            return {} if default is None else default
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            log.warning("Malformed JSON in %s for org %s", context, self.org_id)
            return {} if default is None else default

    # ── Hygiene ────────────────────────────────────────────────────────────
    def __repr__(self) -> str:  # pragma: no cover
        # Never let a key reach a log line or a traceback.
        return f"OrgCipher(org_id={self.org_id!r}, key=<redacted>)"

    __str__ = __repr__


# ── Column context labels ──────────────────────────────────────────────────
# Kept in one place so an encrypt and its matching decrypt cannot drift apart.
class Ctx:
    PAGE_BODY = "seo_page.body"
    PAGE_PROPOSED = "seo_page.proposed_body"
    PAGE_RATIONALE = "seo_page.rewrite_rationale"
    QA_QUESTION = "aeo_pair.question"
    QA_ANSWER = "aeo_pair.answer"
    SCHEMA_JSONLD = "schema_patch.json_ld"
    PITCH_SUBJECT = "outreach_pitch.subject"
    PITCH_BODY = "outreach_pitch.body"
    CREATIVE_HEADLINE = "ad_creative.headline"
    CREATIVE_BODY = "ad_creative.body_copy"
    CONNECTOR_CREDENTIALS = "connector.credentials"
    APPROVAL_PAYLOAD = "approval_item.payload"
