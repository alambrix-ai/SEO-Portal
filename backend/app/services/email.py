"""Transactional email: the sign-in codes, and invitations.

This module is not a nicety on this platform — it *is* the authentication
channel. Sign-in is a one-time code mailed to the address, so a deployment
without working mail is a deployment nobody can log in to, which is why
production refuses to start with neither ``SMTP_HOST`` nor ``RESEND_API_KEY``.

Prefer ``RESEND_API_KEY`` on Render and similar hosts: they commonly block
outbound SMTP ports (587/465), while HTTPS to ``api.resend.com`` works.

In development, with both unset, the message is written to the log instead of
sent, so a local install needs no mail server — the code is right there in the
console.
"""
from __future__ import annotations

import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formataddr

import httpx

from app.core.config import settings
from app.core.logging import get_logger

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

    @property
    def label(self) -> str:
        return self.log_label or self.subject


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
        log.error("Resend delivery to %s failed: %s", message.to, exc)
        return False
    if response.status_code >= 400:
        log.error(
            "Resend delivery to %s failed: HTTP %s %s",
            message.to,
            response.status_code,
            response.text[:300],
        )
        return False
    log.info("Sent %r to %s via Resend", message.label, message.to)
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
        log.error("SMTP delivery to %s failed: %s", message.to, exc)
        return False
    log.info("Sent %r to %s", message.label, message.to)
    return True


def _send(message: Message) -> bool:
    """Return True when handed to a mail provider, False when only logged."""
    if settings.resend_api_key:
        return _send_via_resend(message)
    if not settings.smtp_host:
        log.info(
            "[email:not-configured] to=%s subject=%s\n%s",
            message.to,
            message.subject,
            message.text,
        )
        return False
    return _send_via_smtp(message)


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
