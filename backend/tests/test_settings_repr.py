"""The settings object never prints secrets (it lands in tracebacks and test output)."""
from __future__ import annotations

from app.core.config import Settings


def test_secrets_are_masked_in_repr(monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:SuperSecretPw@db.example/app")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-REALKEY")
    text = repr(Settings())
    assert "SuperSecretPw" not in text
    assert "REALKEY" not in text
    # Vendor base URLs are not secrets and stay readable.
    assert "api.anthropic.com" in text
