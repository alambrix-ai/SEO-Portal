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
from app.core.exceptions import ConnectorConfigError, ConnectorError
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
            text("fromName", "From name", "Your Brand"),
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
        raw = self.credentials.require("port")
        try:
            return int(raw)
        except ValueError as exc:
            raise ConnectorConfigError(
                "Port must be a number such as 587 or 465"
            ) from exc

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
            raise ConnectorError("That email address does not look valid")

        email = EmailMessage()
        email["From"] = formataddr(
            (self.credentials.require("fromName"), self.sender)
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
            raise ConnectorError(
                "The mail server rejected those credentials. Check the username and password."
            ) from exc
        except smtplib.SMTPRecipientsRefused as exc:
            raise ConnectorError(
                "The mail server refused that recipient address."
            ) from exc
        except (smtplib.SMTPException, OSError, ssl.SSLError) as exc:
            log.warning("SMTP delivery failed: %s", exc)
            raise ConnectorError(
                "Could not send email through that mail server. Try again shortly."
            ) from exc

        log.info("Sent %r to %s", subject, recipient)
        return True

    def check_health(self) -> HealthReport:
        try:
            host = self.credentials.require("host")
        except Exception:  # noqa: BLE001
            return HealthReport(
                ok=False, detail="Enter the SMTP host before connecting.", checked_at=utcnow()
            )

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
        except ConnectorError as exc:
            return HealthReport(ok=False, detail=str(exc), checked_at=utcnow())
        except Exception as exc:  # noqa: BLE001 - a probe must not raise
            log.warning("SMTP health probe failed: %s", exc)
            return HealthReport(
                ok=False,
                detail="Could not sign in to that mail server. Check the host, port and password.",
                checked_at=utcnow(),
            )
        return HealthReport(
            ok=True, detail="Connection looks good", checked_at=utcnow()
        )


CONNECTOR_CLASS = SmtpEmailConnector
