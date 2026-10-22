"""Email / SMTP connector — the channel outreach pitches actually go out on.

Unlike every other connector this one speaks SMTP rather than HTTP, so it
implements delivery directly instead of going through ``request``. Mail is
sent over STARTTLS (or implicit TLS) with certificate verification on; the
password is decrypted per send and never logged.
"""
from __future__ import annotations

import smtplib
import ssl
from collections.abc import Iterator
from contextlib import contextmanager
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
#: The message submission port (RFC 6409, section 3.1), used when Port is blank.
DEFAULT_PORT = "587"


class SmtpEmailConnector(NotificationConnector):
    spec = ConnectorSpec(
        slug="smtp_email",
        advanced=True,
        name="Email / SMTP",
        category="Collaboration",
        description="Send outreach pitches and notifications from your own mail server.",
        fields=(
            text("host", "SMTP host", "smtp.yourdomain.com"),
            text(
                "port",
                "Port",
                DEFAULT_PORT,
                required=False,
                default=DEFAULT_PORT,
                help_text=(
                    "Leave blank to use 587 (mail submission with STARTTLS). "
                    "Enter 465 if your provider uses implicit TLS."
                ),
            ),
            text("user", "Username", "notifications@yourdomain.com"),
            secret("password", "Password", "••••••••"),
            text(
                "fromName",
                "From name",
                "Your Brand",
                required=False,
                help_text="Leave blank to send from the address alone, with no display name.",
            ),
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
        raw = (self.credentials.get("port") or DEFAULT_PORT).strip()
        try:
            return int(raw)
        except ValueError as exc:
            raise ConnectorConfigError(
                "Port must be a number such as 587 or 465"
            ) from exc

    @property
    def sender(self) -> str:
        return self.credentials.require("user")

    @contextmanager
    def _session(self, *, host: str = "") -> Iterator[smtplib.SMTP]:
        """A signed-in SMTP session: implicit TLS on 465, otherwise STARTTLS.

        Shared by sending, the health probe and the live check, so the check
        proves the exact connection mail goes out on. Certificate
        verification is always on. Raises smtplib/ssl/OS errors as they come.
        """
        host = host or self.credentials.require("host")
        password = self.credentials.require("password")
        context = ssl.create_default_context()
        if self.port == 465:
            with smtplib.SMTP_SSL(host, self.port, context=context, timeout=TIMEOUT_SECONDS) as smtp:
                smtp.login(self.sender, password)
                yield smtp
        else:
            with smtplib.SMTP(host, self.port, timeout=TIMEOUT_SECONDS) as smtp:
                smtp.starttls(context=context)
                smtp.login(self.sender, password)
                yield smtp

    def verify_steps(self) -> list:
        """Connect, secure the connection, sign in, NOOP, quit. Never sends.

        There is no write step: the only write SMTP has is sending mail, and
        a message cannot be "undone" once it is handed to the server.
        """
        from app.connectors.base import verify_helpers as vh

        def sign_in() -> tuple[str, dict]:
            try:
                with self._session() as smtp:
                    code, _ = smtp.noop()
                    tls = "implicit TLS" if self.port == 465 else "STARTTLS"
            except smtplib.SMTPAuthenticationError as exc:
                raise ConnectorError(
                    "The mail server rejected those credentials. Check the username and password."
                ) from exc
            except (smtplib.SMTPException, OSError, ssl.SSLError) as exc:
                raise ConnectorError(
                    f"Could not open a secure, signed-in session on port {self.port}: {type(exc).__name__}."
                ) from exc
            if code != 250:
                raise ConnectorError(f"The mail server signed in but answered NOOP with {code}.")
            return (
                f"Signed in to the mail server over {tls} on port {self.port}; nothing was sent.",
                {"port": self.port, "tls": tls, "noop_code": code},
            )

        return [vh.read_step("smtp_sign_in", "Sign in to the mail server without sending", sign_in)]

    def notify(self, *, subject: str, message: str, channel: str = "") -> bool:
        """Send one message. ``channel`` is the recipient address."""
        # No fallback recipient. Sending a customer's agent notification to
        # whatever address happened to be configured as the sender is a
        # delivery nobody asked for, and it looks like success.
        recipient = channel or self.credentials.require("notifyTo")
        if "@" not in recipient:
            raise ConnectorError("That email address does not look valid")

        email = EmailMessage()
        # A display name is optional in an address (RFC 5322, section 3.4);
        # formataddr writes the bare address when the name is empty.
        email["From"] = formataddr(
            ((self.credentials.get("fromName") or "").strip(), self.sender)
        )
        email["To"] = recipient
        email["Subject"] = subject
        # A stable Message-ID improves deliverability and makes replies
        # threadable, which matters for outreach that expects an answer.
        email["Message-ID"] = make_msgid(domain=self.sender.split("@")[-1])
        email.set_content(message)

        try:
            with self._session() as smtp:
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

        try:
            with self._session(host=host):
                pass
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
