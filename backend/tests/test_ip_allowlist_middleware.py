"""IP allowlist middleware + lockout-safety unit coverage."""
from __future__ import annotations

from app.api.middleware import IpAllowlistMiddleware
from app.services import ip_allowlist


def test_skip_suffixes_cover_login_and_mfa() -> None:
    skips = IpAllowlistMiddleware.SKIP_SUFFIXES
    assert "/auth/login" in skips
    assert "/mfa/verify" in skips


def test_validate_cidr_accepts_v4_and_v6() -> None:
    assert ip_allowlist.validate_cidr("203.0.113.10") == "203.0.113.10/32"
    assert "2001:db8" in ip_allowlist.validate_cidr("2001:db8::/64")
