"""Mail delivery logs never hold an address, a subject, a body or a code.

A sign-in code travels in the subject and the body, and the recipient's
address is personal data; the log only gets a label and a short hash.
"""
from __future__ import annotations

import logging
import smtplib

import httpx
import pytest

from app.core.config import settings
from app.services import email as email_service

# Taken at import, before the autouse test mailbox replaces ``_send``.
from app.services.email import _send as real_send

ADDRESS = "Jane.Doe@Example.com"
CODE = "482913"


def _message() -> email_service.Message:
    return email_service.Message(
        to=ADDRESS,
        subject=f"Your sign-in code: {CODE}",
        text=f"Your sign-in code is {CODE}",
        log_label="sign-in code",
        dev_code=CODE,
    )


def _assert_clean(caplog: pytest.LogCaptureFixture) -> None:
    assert caplog.records, "expected the delivery to be logged"
    for record in caplog.records:
        text = record.getMessage().lower()
        assert "jane" not in text and "example.com" not in text
        assert CODE not in text and "sign-in code:" not in text


def test_recipient_ref_is_a_stable_short_hash():
    ref = email_service.recipient_ref(ADDRESS)
    assert len(ref) == 12 and all(c in "0123456789abcdef" for c in ref)
    assert ref == email_service.recipient_ref("  jane.doe@example.com ")
    assert ref != email_service.recipient_ref("someone@example.com")


def test_an_unlabelled_message_never_logs_its_subject():
    message = email_service.Message(to=ADDRESS, subject=f"Code {CODE}", text="x")
    assert message.label == "message"


def test_smtp_success_and_refusal_log_no_address_or_code(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
):
    sent: list[object] = []

    class FakeSMTP:
        refuse = False

        def __init__(self, *a: object, **k: object) -> None:
            pass

        def __enter__(self) -> FakeSMTP:
            return self

        def __exit__(self, *a: object) -> None:
            return None

        def starttls(self, **k: object) -> None:
            return None

        def login(self, *a: object) -> None:
            return None

        def send_message(self, msg: object) -> None:
            if FakeSMTP.refuse:
                # The real exception names the refused address.
                raise smtplib.SMTPRecipientsRefused({ADDRESS: (550, b"no such user " + ADDRESS.encode())})
            sent.append(msg)

    monkeypatch.setattr(settings, "smtp_use_tls", True)
    monkeypatch.setattr(settings, "smtp_user", "")
    monkeypatch.setattr(email_service.smtplib, "SMTP", FakeSMTP)
    caplog.set_level(logging.DEBUG, logger=email_service.log.name)

    assert email_service._send_via_smtp(_message()) is True
    FakeSMTP.refuse = True
    assert email_service._send_via_smtp(_message()) is False

    assert len(sent) == 1
    _assert_clean(caplog)
    assert email_service.recipient_ref(ADDRESS) in caplog.text


def test_resend_failures_log_no_address_body_or_code(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
):
    responses = iter(
        [
            httpx.Response(422, text=f'{{"message": "invalid to: {ADDRESS}", "code": "{CODE}"}}'),
            httpx.Response(200, json={"id": "x"}),
        ]
    )

    def fake_post(*a: object, **k: object) -> httpx.Response:
        return next(responses)

    monkeypatch.setattr(email_service.httpx, "post", fake_post)
    caplog.set_level(logging.DEBUG, logger=email_service.log.name)

    assert email_service._send_via_resend(_message()) is False
    assert email_service._send_via_resend(_message()) is True

    def unreachable(*a: object, **k: object) -> httpx.Response:
        raise httpx.ConnectError(f"cannot reach for {ADDRESS}")

    monkeypatch.setattr(email_service.httpx, "post", unreachable)
    assert email_service._send_via_resend(_message()) is False
    _assert_clean(caplog)


def test_unconfigured_mail_does_not_log_the_message(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
):
    monkeypatch.setattr(settings, "app_env", "development")
    monkeypatch.setattr(email_service, "_dev_log_flag", lambda: False)
    monkeypatch.setattr(settings, "smtp_host", "")
    caplog.set_level(logging.DEBUG, logger=email_service.log.name)
    assert real_send(_message()) is False
    _assert_clean(caplog)


def test_missing_resend_key_does_not_log_the_address(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
):
    monkeypatch.setattr(settings, "app_env", "production")
    monkeypatch.setattr(settings, "resend_api_key", "")
    caplog.set_level(logging.DEBUG, logger=email_service.log.name)
    assert real_send(_message()) is False
    _assert_clean(caplog)


def _codes_logged(caplog: pytest.LogCaptureFixture) -> bool:
    return any(CODE in record.getMessage() for record in caplog.records)


@pytest.mark.parametrize("env", ["production", "staging", "test"])
def test_the_dev_code_flag_never_logs_a_code_outside_development(
    env: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
):
    monkeypatch.setattr(settings, "app_env", env)
    monkeypatch.setattr(email_service, "_dev_log_flag", lambda: True)
    monkeypatch.setattr(settings, "smtp_host", "")
    monkeypatch.setattr(settings, "resend_api_key", "")
    caplog.set_level(logging.DEBUG, logger=email_service.log.name)
    assert real_send(_message()) is False
    assert not _codes_logged(caplog)
    _assert_clean(caplog)


def test_development_without_the_flag_does_not_log_the_code(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
):
    monkeypatch.setattr(settings, "app_env", "development")
    monkeypatch.setattr(email_service, "_dev_log_flag", lambda: False)
    monkeypatch.setattr(settings, "smtp_host", "")
    caplog.set_level(logging.DEBUG, logger=email_service.log.name)
    assert real_send(_message()) is False
    assert not _codes_logged(caplog)


def test_a_truthy_non_bool_flag_does_not_count(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
):
    monkeypatch.setattr(settings, "app_env", "development")
    monkeypatch.setattr(email_service, "_dev_log_flag", lambda: "false")
    monkeypatch.setattr(settings, "smtp_host", "")
    caplog.set_level(logging.DEBUG, logger=email_service.log.name)
    assert real_send(_message()) is False
    assert not _codes_logged(caplog)


def test_development_with_the_flag_logs_only_the_code(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
):
    monkeypatch.setattr(settings, "app_env", "development")
    monkeypatch.setattr(email_service, "_dev_log_flag", lambda: True)
    monkeypatch.setattr(settings, "smtp_host", "")
    caplog.set_level(logging.DEBUG, logger=email_service.log.name)
    assert real_send(_message()) is False
    assert _codes_logged(caplog)
    text = caplog.text.lower()
    assert "jane" not in text and "example.com" not in text
    assert "sign-in code:" not in text  # the subject line
    assert "your sign-in code is" not in text  # the body
    assert email_service.recipient_ref(ADDRESS) in caplog.text


def test_the_code_is_not_in_the_message_repr():
    assert CODE not in repr(email_service.Message(to="a@b.co", subject="s", text="t", dev_code=CODE))
