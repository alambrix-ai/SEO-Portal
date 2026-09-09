"""Email / SMTP connector — the channel outreach pitches actually go out on.

Unlike every other connector this one speaks SMTP rather than HTTP, so it
implements delivery directly instead of going through ``request``. Mail is
sent over STARTTLS (or implicit TLS) with certificate verification on; the
password is decrypted per send and never logged.
"""
from __future__ import annotations

import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr, make_msgid

from app.connectors.base.connector import Capability, ConnectorSpec, HealthReport
from app.connectors.base.credentials import secret, text
from app.connectors.base.interfaces import NotificationConnector
from app.core.exceptions import ConnectorError
from app.core.logging import get_logger
from app.db.base import utcnow

log = get_logger(__name__)

TIMEOUT_SECONDS = 20


class SmtpEmailConnector(NotificationConnector):
    spec = ConnectorSpec(
        slug="smtp_email",
        name="Email / SMTP",
        category="Collaboration",
        description="Send outreach pitches and notifications from your own mail server.",
        fields=(
            text("host", "SMTP host", "smtp.yourdomain.com"),
            text("port", "Port", "587"),
            text("user", "Username", "notifications@yourdomain.com"),
            secret("password", "Password", "••••••••"),
            text("fromName", "From name", "Your Brand", required=False),
            text(
                "notifyTo",
                "Send notifications to",
                "seo-team@yourdomain.com",
                help_text="Where agent notifications are delivered.",
            ),
        ),
        capabilities=frozenset({Capability.SEND_EMAIL, Capability.SEND_NOTIFICATION}),
    )

    @property
    def port(self) -> int:
        try:
            return int(self.credentials.require("port"))
        except ValueError:
            return 587

    @property
    def sender(self) -> str:
        return self.credentials.require("user")

    def notify(self, *, subject: str, message: str, channel: str = "") -> bool:
        """Send one message. ``channel`` is the recipient address."""
        # No fallback recipient. Sending a customer's agent notification to
        # whatever address happened to be configured as the sender is a
        # delivery nobody asked for, and it looks like success.
        recipient = channel or self.credentials.require("notifyTo")
        if "@" not in recipient:
            raise ConnectorError(f"{recipient!r} is not a deliverable email address")

        email = EmailMessage()
        email["From"] = formataddr(
            (self.credentials.get("fromName") or "AutoMarket AI", self.sender)
        )
        email["To"] = recipient
        email["Subject"] = subject
        # A stable Message-ID improves deliverability and makes replies
        # threadable, which matters for outreach that expects an answer.
        email["Message-ID"] = make_msgid(domain=self.sender.split("@")[-1])
        email.set_content(message)

        context = ssl.create_default_context()
        host = self.credentials.require("host")
        password = self.credentials.require("password")

        try:
            if self.port == 465:
                with smtplib.SMTP_SSL(
                    host, self.port, context=context, timeout=TIMEOUT_SECONDS
                ) as smtp:
                    smtp.login(self.sender, password)
                    smtp.send_message(email)
            else:
                with smtplib.SMTP(host, self.port, timeout=TIMEOUT_SECONDS) as smtp:
                    smtp.starttls(context=context)
                    smtp.login(self.sender, password)
                    smtp.send_message(email)
        except smtplib.SMTPAuthenticationError as exc:
            raise ConnectorError("SMTP rejected the credentials") from exc
        except smtplib.SMTPRecipientsRefused as exc:
            raise ConnectorError(f"Recipient {recipient} was refused") from exc
        except (smtplib.SMTPException, OSError, ssl.SSLError) as exc:
            raise ConnectorError(f"SMTP delivery failed: {exc}") from exc

        log.info("Sent %r to %s", subject, recipient)
        return True

    def check_health(self) -> HealthReport:
        host = self.credentials.get("host")
        if not host:
            return HealthReport(ok=False, detail="No SMTP host configured", checked_at=utcnow())

        context = ssl.create_default_context()
        try:
            if self.port == 465:
                with smtplib.SMTP_SSL(
                    host, self.port, context=context, timeout=TIMEOUT_SECONDS
                ) as smtp:
                    smtp.login(self.sender, self.credentials.require("password"))
            else:
                with smtplib.SMTP(host, self.port, timeout=TIMEOUT_SECONDS) as smtp:
                    smtp.starttls(context=context)
                    smtp.login(self.sender, self.credentials.require("password"))
        except Exception as exc:  # noqa: BLE001 - a probe must not raise
            return HealthReport(ok=False, detail=str(exc), checked_at=utcnow())
        return HealthReport(
            ok=True, detail=f"Authenticated to {host}:{self.port}", checked_at=utcnow()
        )


CONNECTOR_CLASS = SmtpEmailConnector
