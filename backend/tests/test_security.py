"""One-time passcode primitives and access-token tests.

The code-*rules* (expiry, single use, attempt caps, throttling) are exercised
against a real database in ``test_api_flows.py``, because they live in rows.
What is tested here is the arithmetic underneath them.
"""
from __future__ import annotations

from collections import Counter

import pytest

from app.core.exceptions import AuthError, InvalidInputError
from app.core.security import (
    codes_match,
    create_access_token,
    decode_access_token,
    new_login_code,
    normalize_code,
)


# ── Codes ──────────────────────────────────────────────────────────────────
def test_code_is_the_configured_length_and_all_digits():
    for _ in range(50):
        code = new_login_code()
        assert len(code) == 6
        assert code.isdigit()


def test_code_length_is_not_biased_by_leading_zeros():
    """The classic bug: formatting a random int without zero-padding.

    ``str(randbelow(10**6))`` is a *shorter* string one time in ten, which
    both leaks information and shrinks the space. Every code must be full
    length, including the one in a million that is all zeros.
    """
    lengths = Counter(len(new_login_code()) for _ in range(2000))
    assert set(lengths) == {6}


def test_codes_are_not_predictable():
    """A CSPRNG, not a seeded PRNG: 500 draws should not repeat much."""
    codes = [new_login_code() for _ in range(500)]
    assert len(set(codes)) > 490


def test_code_length_is_clamped_to_a_safe_range(monkeypatch):
    """A misconfigured length must not produce a two-digit credential."""
    from app.core.config import settings

    monkeypatch.setattr(settings, "login_code_length", 2)
    assert len(new_login_code()) == 6
    monkeypatch.setattr(settings, "login_code_length", 40)
    assert len(new_login_code()) == 10


@pytest.mark.parametrize(
    ("submitted", "expected"),
    [
        ("123 456", "123456"),
        ("123-456", "123456"),
        ("  123456  ", "123456"),
        ("123 456", "123456"),  # non-breaking space, from a mail client
        ("Code: 123456", "123456"),
    ],
)
def test_pasted_codes_are_normalised(submitted: str, expected: str):
    assert normalize_code(submitted) == expected


def test_absurdly_long_submission_is_refused():
    with pytest.raises(InvalidInputError):
        normalize_code("1" * 500)


def test_codes_match_is_exact():
    assert codes_match("123456", "123456")
    assert not codes_match("123456", "123457")
    assert not codes_match("123456", "1234567")
    assert not codes_match("", "")
    assert not codes_match("123456", "")


# ── Access tokens ──────────────────────────────────────────────────────────
def test_access_token_roundtrip():
    token, expires_in = create_access_token(
        user_id="user-1", tenant_id="org-1", role="admin", session_id="sess-1"
    )
    assert expires_in > 0

    claims = decode_access_token(token)
    assert claims["sub"] == "user-1"
    assert claims["tid"] == "org-1"
    assert claims["role"] == "admin"
    assert claims["typ"] == "access"


def test_tampered_token_is_rejected():
    token, _ = create_access_token(
        user_id="user-1", tenant_id="org-1", role="client", session_id="s"
    )
    header, payload, signature = token.split(".")
    with pytest.raises(AuthError):
        decode_access_token(f"{header}.{payload}.{signature[:-2]}xx")


def test_token_signed_with_another_secret_is_rejected():
    """A token from a different deployment must not be accepted."""
    import jwt

    from app.core.config import settings

    forged = jwt.encode(
        {
            "sub": "attacker",
            "tid": "org-1",
            "role": "admin",
            "typ": "access",
            "iss": "automarket-ai",
            "aud": "automarket-api",
            "exp": 9_999_999_999,
            "iat": 1,
        },
        "a-different-secret-entirely",
        algorithm="HS256",
    )
    assert settings.secret_key != "a-different-secret-entirely"
    with pytest.raises(AuthError):
        decode_access_token(forged)


def test_expired_token_is_rejected(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "access_token_ttl_minutes", -1)
    token, _ = create_access_token(
        user_id="u", tenant_id="o", role="admin", session_id="s"
    )
    with pytest.raises(AuthError, match="expired"):
        decode_access_token(token)


def test_unsigned_token_is_rejected():
    """The `alg: none` attack must not work."""
    import jwt

    forged = jwt.encode(
        {"sub": "attacker", "tid": "org-1", "exp": 9_999_999_999, "iat": 1},
        key="",
        algorithm="none",
    )
    with pytest.raises(AuthError):
        decode_access_token(forged)
