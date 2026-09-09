"""Encryption tests.

These are the ones worth being strict about: if the AAD binding or the key
separation is wrong, data is still "encrypted" and nothing looks broken, but
the guarantees the product claims are gone.
"""
from __future__ import annotations

import base64
import os

import pytest

from app.core import crypto
from app.core.crypto import (
    CryptoError,
    blind_index,
    decrypt_column,
    decrypt_for_org,
    email_index,
    encrypt_column,
    encrypt_for_org,
    generate_dek,
    hash_token,
    is_ciphertext,
    new_url_token,
    normalize_email,
    unwrap_dek,
    verify_token,
    wrap_dek,
)


# ── Column encryption (tier 1) ─────────────────────────────────────────────
def test_column_roundtrip():
    token = encrypt_column("users.email", "someone@example.com")
    assert is_ciphertext(token)
    assert "someone@example.com" not in token
    assert decrypt_column("users.email", token) == "someone@example.com"


def test_ciphertext_is_non_deterministic():
    """The same plaintext must not produce the same ciphertext.

    Otherwise equality of ciphertexts would leak equality of plaintexts, which
    is exactly what the blind index exists to provide deliberately.
    """
    a = encrypt_column("users.email", "same@example.com")
    b = encrypt_column("users.email", "same@example.com")
    assert a != b
    assert decrypt_column("users.email", a) == decrypt_column("users.email", b)


def test_column_label_is_authenticated():
    """A ciphertext must not decrypt under a different column's label.

    This is what stops a row being moved from one column to another to change
    what the application reads.
    """
    token = encrypt_column("users.email", "victim@example.com")
    with pytest.raises(CryptoError):
        decrypt_column("invitations.email", token)


def test_tampered_ciphertext_is_rejected():
    token = encrypt_column("users.name", "Original Name")
    prefix, version, nonce, ct = token.split(":", 3)
    # Flip a byte in the ciphertext body.
    flipped = ct[:-4] + ("A" if ct[-4] != "A" else "B") + ct[-3:]
    with pytest.raises(CryptoError):
        decrypt_column("users.name", ":".join([prefix, version, nonce, flipped]))


def test_malformed_token_is_rejected():
    with pytest.raises(CryptoError):
        decrypt_column("users.email", "not-a-token")


# ── Organisation keys (tier 2) ─────────────────────────────────────────────
def test_dek_wrap_roundtrip():
    dek = generate_dek()
    org_id = "11111111-1111-1111-1111-111111111111"
    wrapped = wrap_dek(dek, org_id)
    assert dek not in wrapped.encode()
    assert unwrap_dek(wrapped, org_id) == dek


def test_wrapped_dek_is_bound_to_its_organisation():
    """A stolen wrapped key must be useless under another organisation's id."""
    dek = generate_dek()
    wrapped = wrap_dek(dek, "org-a")
    with pytest.raises(CryptoError):
        unwrap_dek(wrapped, "org-b")


def test_org_payload_roundtrip():
    dek = generate_dek()
    body = "<h1>Confidential page copy</h1>"
    token = encrypt_for_org(dek, body, context="seo_page.body", org_id="org-a")
    assert body not in token
    assert (
        decrypt_for_org(dek, token, context="seo_page.body", org_id="org-a") == body
    )


def test_org_payload_context_is_authenticated():
    dek = generate_dek()
    token = encrypt_for_org(dek, "secret", context="seo_page.body", org_id="org-a")
    with pytest.raises(CryptoError):
        decrypt_for_org(
            dek, token, context="connector.credentials", org_id="org-a"
        )


def test_org_payload_is_isolated_between_organisations():
    """One organisation's key must not read another's data."""
    dek_a, dek_b = generate_dek(), generate_dek()
    token = encrypt_for_org(dek_a, "tenant A data", context="x.y", org_id="org-a")
    with pytest.raises(CryptoError):
        decrypt_for_org(dek_b, token, context="x.y", org_id="org-a")


def test_dek_must_be_32_bytes():
    with pytest.raises(CryptoError):
        wrap_dek(b"too-short", "org-a")


# ── Key rotation ───────────────────────────────────────────────────────────
def test_rotation_keeps_old_ciphertext_readable(monkeypatch):
    """After rotating the master key, rows written under the old one still read.

    That is the whole point of versioned tokens plus a retired-key list; without
    it, rotating the KEK would be a data-loss event.
    """
    from app.core.config import settings

    old_key = base64.b64encode(os.urandom(32)).decode()
    new_key = base64.b64encode(os.urandom(32)).decode()

    monkeypatch.setattr(settings, "master_encryption_key", old_key)
    monkeypatch.setattr(settings, "master_key_version", 1)
    monkeypatch.setattr(settings, "master_encryption_key_retired", [])
    crypto.reset_keyring()

    dek = generate_dek()
    org_id = "org-rotate"
    wrapped_v1 = wrap_dek(dek, org_id)
    payload = encrypt_for_org(dek, "written under v1", context="x.y", org_id=org_id)

    # Rotate: the new key is active, the old one is retained for reads.
    monkeypatch.setattr(settings, "master_encryption_key", new_key)
    monkeypatch.setattr(settings, "master_key_version", 2)
    monkeypatch.setattr(settings, "master_encryption_key_retired", [f"1:{old_key}"])
    crypto.reset_keyring()

    # The old wrapped key still unwraps, so the payload is still readable.
    assert unwrap_dek(wrapped_v1, org_id) == dek
    assert (
        decrypt_for_org(dek, payload, context="x.y", org_id=org_id)
        == "written under v1"
    )

    # And re-wrapping produces a token under the new key version.
    rewrapped = crypto.rewrap_dek(wrapped_v1, org_id)
    assert rewrapped.split(":")[1] == "2"
    assert unwrap_dek(rewrapped, org_id) == dek

    crypto.reset_keyring()


def test_missing_key_version_is_an_explicit_error(monkeypatch):
    from app.core.config import settings

    key_one = base64.b64encode(os.urandom(32)).decode()
    monkeypatch.setattr(settings, "master_encryption_key", key_one)
    monkeypatch.setattr(settings, "master_key_version", 1)
    monkeypatch.setattr(settings, "master_encryption_key_retired", [])
    crypto.reset_keyring()
    wrapped = wrap_dek(generate_dek(), "org-x")

    # Rotate without retaining the old key: reads must fail loudly, not
    # silently return empty data.
    monkeypatch.setattr(
        settings, "master_encryption_key", base64.b64encode(os.urandom(32)).decode()
    )
    monkeypatch.setattr(settings, "master_key_version", 2)
    crypto.reset_keyring()

    with pytest.raises(CryptoError, match="No master key for version 1"):
        unwrap_dek(wrapped, "org-x")

    crypto.reset_keyring()


# ── Blind index ────────────────────────────────────────────────────────────
def test_blind_index_is_deterministic_and_non_reversible():
    first = blind_index("someone@example.com", domain="email")
    second = blind_index("someone@example.com", domain="email")
    assert first == second
    assert "someone" not in first
    assert len(first) == 44


def test_blind_index_domains_are_separated():
    """The same value in two namespaces must not produce the same index."""
    assert blind_index("value", domain="email") != blind_index("value", domain="phone")


def test_email_index_normalises():
    assert email_index("  Someone@Example.COM ") == email_index("someone@example.com")


def test_normalize_email():
    assert normalize_email("  MiXeD@Case.Com  ") == "mixed@case.com"


# ── Opaque tokens ──────────────────────────────────────────────────────────
def test_url_token_is_high_entropy_and_url_safe():
    token = new_url_token(32)
    assert len(token) >= 40
    assert "=" not in token and "+" not in token and "/" not in token
    assert token != new_url_token(32)


def test_token_hash_verification():
    token = new_url_token()
    stored = hash_token(token)
    assert token not in stored
    assert verify_token(token, stored)
    assert not verify_token(new_url_token(), stored)
