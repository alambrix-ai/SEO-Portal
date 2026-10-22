"""Transactional email: the sign-in codes, and invitations.

This module is not a nicety on this platform — it *is* the authentication
channel. Sign-in is a one-time code mailed to the address, so a deployment
without working mail is a deployment nobody can log in to, which is why
production refuses to start without ``RESEND_API_KEY``.

Provider is chosen from ``APP_ENV`` (not “whichever key is set”):

* ``development`` → SMTP (local / Gmail app password, Mailhog, etc.)
* ``test`` → Resend HTTPS (integration / hosted testing)
* ``staging`` / ``production`` → Resend HTTPS (Render blocks SMTP)

In development, with ``SMTP_HOST`` unset, nothing is sent and the log says
so. The log never holds a recipient address, a subject or a body (a subject
or body can carry a sign-in code): a recipient appears only as
:func:`recipient_ref`, a short hash that correlates repeats. For local work
without a mail server, ``DEV_LOG_SIGN_IN_CODES=true`` additionally prints the
one-time code itself to this process's log — only with ``APP_ENV=development``,
never in test, staging or production.
"""
from __future__ import annotations

import hashlib
import logging
import smtplib
import ssl
from dataclasses import dataclass, field
from email.message import EmailMessage
from email.utils import formataddr

import httpx

from app.core.config import settings
from app.core.logging import get_logger, log_failure

log = get_logger(__name__)


@dataclass(slots=True)
class Message:
    to: str
    subject: str
    text: str
    # What the log is allowed to say about this message. The subject of a
    # sign-in mail carries the code itself — good for the recipient, who sees
    # it in a notification without opening anything, and unacceptable in a log
    # file, which is read by more people than the mailbox is and kept longer.
    # So logging uses this label and never the subject or the body.
    log_label: str = ""
    # The one-time code alone, for the development-only log in :func:`_send`.
    # Kept out of repr so it cannot reach a log by accident.
    dev_code: str = field(default="", repr=False)

    @property
    def label(self) -> str:
        # Never the subject: an unlabelled message is logged generically.
        return self.log_label or "message"


def recipient_ref(address: str) -> str:
    """A 12-character hash of an address, for logs: the same mailbox always
    gives the same value, and the address cannot be read back from it."""
    normalised = (address or "").strip().lower()
    return hashlib.sha256(normalised.encode("utf-8", "replace")).hexdigest()[:12]


def _send_via_resend(message: Message) -> bool:
    """Deliver over HTTPS. Used when SMTP ports are blocked (e.g. Render free)."""
    payload = {
        "from": formataddr((settings.email_from_name, settings.email_from)),
        "to": [message.to],
        "subject": message.subject,
        "text": message.text,
    }
    try:
        response = httpx.post(
            f"{settings.resend_base_url.rstrip('/')}/emails",
            headers={
                "Authorization": f"Bearer {settings.resend_api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=20.0,
        )
    except httpx.HTTPError as exc:
        log_failure(
            log, "Resend delivery failed", exc, level=logging.ERROR, to=recipient_ref(message.to)
        )
        return False
    if response.status_code >= 400:
        # Status only: the response body can echo the recipient back.
        log.error(
            "Resend delivery failed: HTTP %s to=%s",
            response.status_code,
            recipient_ref(message.to),
        )
        return False
    log.info("Sent %r to=%s via Resend", message.label, recipient_ref(message.to))
    return True


def _send_via_smtp(message: Message) -> bool:
    msg = EmailMessage()
    msg["From"] = formataddr((settings.email_from_name, settings.email_from))
    msg["To"] = message.to
    msg["Subject"] = message.subject
    msg.set_content(message.text)

    context = ssl.create_default_context()
    try:
        if settings.smtp_use_tls:
            with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=15) as smtp:
                smtp.starttls(context=context)
                if settings.smtp_user:
                    smtp.login(settings.smtp_user, settings.smtp_password)
                smtp.send_message(msg)
        else:
            with smtplib.SMTP_SSL(
                settings.smtp_host, settings.smtp_port, context=context, timeout=15
            ) as smtp:
                if settings.smtp_user:
                    smtp.login(settings.smtp_user, settings.smtp_password)
                smtp.send_message(msg)
    except (smtplib.SMTPException, OSError) as exc:
        # A failed notification must not fail the request that triggered it;
        # the user can always ask for another link.
        # log_failure masks addresses in the error text (a refused
        # recipient is named in it).
        log_failure(
            log, "SMTP delivery failed", exc, level=logging.ERROR, to=recipient_ref(message.to)
        )
        return False
    log.info("Sent %r to=%s", message.label, recipient_ref(message.to))
    return True


def _use_resend() -> bool:
    """``test`` / staging / production → Resend; ``development`` (local) → SMTP."""
    return settings.app_env in ("test", "staging", "production")


def _dev_log_flag() -> object:
    """``DEV_LOG_SIGN_IN_CODES``. Config refuses it outside development."""
    return settings.dev_log_sign_in_codes


def _dev_code_logging() -> bool:
    """True only in development with ``DEV_LOG_SIGN_IN_CODES`` set to true."""
    return settings.app_env == "development" and _dev_log_flag() is True


def _send(message: Message) -> bool:
    """Return True when handed to a mail provider, False when only logged."""
    if settings.app_env == "development":
        if not settings.smtp_host:
            log.info(
                "[email:not-configured] %r to=%s not sent (set SMTP_HOST to deliver)",
                message.label,
                recipient_ref(message.to),
            )
            if message.dev_code and _dev_code_logging():
                # Developer convenience, development only: the code, never
                # the address, subject or body.
                log.warning(
                    "[email:dev-code] %s code for to=%s: %s",
                    message.label,
                    recipient_ref(message.to),
                    message.dev_code,
                )
            return False
        return _send_via_smtp(message)

    # APP_ENV=test (and staging/production): Resend only
    if not settings.resend_api_key:
        log.error(
            "APP_ENV=%s requires RESEND_API_KEY to send mail (to=%s)",
            settings.app_env,
            recipient_ref(message.to),
        )
        return False
    return _send_via_resend(message)


def _link(path: str, token: str) -> str:
    return f"{settings.public_base_url.rstrip('/')}{path}?token={token}"


def _code_block(code: str) -> str:
    """Group the digits so they can be read off a screen without losing place."""
    if len(code) == 6:
        return f"{code[:3]} {code[3:]}"
    return code


def send_login_code(*, to: str, name: str, code: str, minutes: int) -> bool:
    greeting = f"Hi {name},\n\n" if name else ""
    return _send(
        Message(
            to=to,
            subject=f"Your {settings.app_name} sign-in code: {code}",
            log_label="sign-in code",
            dev_code=code,
            text=(
                f"{greeting}"
                f"Your sign-in code is:\n\n    {_code_block(code)}\n\n"
                f"It expires in {minutes} minutes and works once.\n\n"
                "If you did not try to sign in, someone else has your email "
                "address and is trying to use it. The code above is no use to "
                "them unless they can read this mailbox, so nothing has "
                "happened — but tell your workspace administrator, and check "
                "that this mailbox is secure.\n"
            ),
        )
    )


def send_signup_code(*, to: str, code: str, minutes: int) -> bool:
    return _send(
        Message(
            to=to,
            subject=f"Your {settings.app_name} verification code: {code}",
            log_label="sign-up code",
            dev_code=code,
            text=(
                "Use this code to confirm your email address and finish "
                f"creating your {settings.app_name} workspace:\n\n"
                f"    {_code_block(code)}\n\n"
                f"It expires in {minutes} minutes and works once.\n\n"
                "If you were not signing up, you can ignore this message — no "
                "account exists until the code is used.\n"
            ),
        )
    )


def send_invitation(*, to: str, org_name: str, inviter: str, role_label: str, token: str) -> bool:
    url = _link("/accept-invite", token)
    return _send(
        Message(
            to=to,
            subject=f"{inviter} invited you to {org_name} on {settings.app_name}",
            log_label="invitation",
            text=(
                f"{inviter} has invited you to join {org_name} as {role_label}.\n\n"
                f"Accept the invitation here:\n\n{url}\n\n"
                "There is no password to choose. Signing in to "
                f"{settings.app_name} sends a one-time code to this address, "
                "so keep access to this mailbox.\n\n"
                f"The invitation expires in {settings.invitation_ttl_days} days.\n"
            ),
        )
    )
