"""Webhooks / Zapier connector — the generic outbound escape hatch.

Whatever a customer wants to happen when an agent acts — a row in a sheet, a
ticket, a message in a tool nobody has built a connector for — this covers it.
Payloads are HMAC-signed so the receiver can verify they came from this
platform, and the signature is over a timestamped body so a captured payload
cannot be replayed.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time

from app.connectors.base.connector import Capability, ConnectorSpec, HealthReport, SetupGuide
from app.connectors.base.credentials import secret, text
from app.connectors.base.interfaces import NotificationConnector
from app.core.exceptions import ConnectorError
from app.core.logging import get_logger
from app.db.base import utcnow

log = get_logger(__name__)

#: Zapier's own wording (help.zapier.com, "Trigger Zaps from webhooks").
#: Webhooks by Zapier is a premium app, so it needs a paid Zapier plan.
GUIDE = SetupGuide(
    steps=(
        "In Zapier, click “Create” → “Zaps”. For the trigger, choose “Webhooks by "
        "Zapier” and the event “Catch Hook”, then click “Continue” (it needs a paid "
        "Zapier plan).",
        "On the “Test” tab, click “Copy” next to the webhook URL. It starts with "
        "https://hooks.zapier.com/hooks/catch/.",
        "Paste it here and connect. A test message is sent, so back in Zapier click "
        "“Test trigger” to see it, then add the action you want (a sheet row, an email…).",
    ),
    key_url="https://zapier.com/app/zaps",
    key_url_label="Open Zapier",
    key_field="webhookUrl",
    key_hint="Zapier webhook URLs start with https://hooks.zapier.com/hooks/catch/",
)


class ZapierWebhooksConnector(NotificationConnector):
    spec = ConnectorSpec(
        slug="zapier_webhooks",
        advanced=True,
        guide=GUIDE,
        name="Webhooks / Zapier",
        category="Collaboration",
        description=(
            "POST every agent action to a webhook — Zapier, Make, or your own "
            "endpoint. Payloads are HMAC-signed."
        ),
        fields=(
            text("webhookUrl", "Webhook URL", "https://hooks.zapier.com/hooks/catch/••••"),
            secret(
                "signingSecret",
                "Signing secret",
                "••••••••",
                required=False,
                help_text=(
                    "Optional, for your own endpoint: each message is then signed so it "
                    "can check the sender. Zapier does not need it."
                ),
            ),
        ),
        capabilities=frozenset({Capability.SEND_NOTIFICATION}),
        base_url_field="webhookUrl",
    )

    @property
    def base_url(self) -> str:
        # The webhook URL is the full endpoint, not a prefix, so requests are
        # posted to the root path against it.
        return self.credentials.get("webhookUrl", "").rstrip("/")

    def auth_headers(self) -> dict[str, str]:
        return {"Content-Type": "application/json"}

    def _signature_headers(self, body: str) -> dict[str, str]:
        secret_value = self.credentials.get("signingSecret")
        if not secret_value:
            # Signing is optional: plenty of Zapier catch hooks cannot verify
            # one. Where a secret is set, every payload carries a signature.
            return {}
        timestamp = str(int(time.time()))
        digest = hmac.new(
            secret_value.encode(), f"{timestamp}.{body}".encode(), hashlib.sha256
        ).hexdigest()
        return {
            "X-AutoMarket-Timestamp": timestamp,
            "X-AutoMarket-Signature": f"sha256={digest}",
        }

    def _post_signed(self, payload: dict) -> None:
        """Serialise once and send exactly the bytes that were signed.

        Signing ``json.dumps(payload)`` and then passing the dict as
        ``json_body`` meant httpx serialised it again, its own way (different
        separators, different escaping of non-ASCII), so the receiver's HMAC
        over the body it got never matched the signature.
        """
        body = json.dumps(payload, separators=(",", ":"), default=str)
        self.request(
            "POST",
            "",
            content=body.encode("utf-8"),
            headers={
                **self._signature_headers(body),
                "Content-Type": "application/json",
            },
            # Catch hooks commonly answer with a bare "ok" rather than JSON.
            expect_json=False,
        )

    def notify(self, *, subject: str, message: str, channel: str = "") -> bool:
        if not self.base_url:
            raise ConnectorError("No webhook URL configured")
        self._post_signed(
            {
                "event": "agent.action",
                "subject": subject,
                "message": message,
                "target": channel,
                "sent_at": utcnow().isoformat(),
                "source": "automarket-ai",
            }
        )
        return True

    def check_health(self) -> HealthReport:
        if not self.base_url:
            return HealthReport(ok=False, detail="No webhook URL configured", checked_at=utcnow())
        try:
            # A ping event, so a health check does not look like a real action
            # to whatever is on the other end.
            # Signed over the same bytes it sends — the probe used to sign a
            # different body from the one it posted.
            self._post_signed({"event": "connection.test", "source": "automarket-ai"})
        except ConnectorError as exc:
            return HealthReport(ok=False, detail=str(exc), checked_at=utcnow())
        return HealthReport(ok=True, detail="Webhook accepted a test payload", checked_at=utcnow())

    # ── Live checks ────────────────────────────────────────────────────────
    def verify_steps(self) -> list:
        """Check the webhook address offline; send one marked test event.

        A webhook only receives, so there is nothing to read: the read step
        checks the address itself (HTTPS, a host, Zapier's catch-hook path
        when it is Zapier) without sending anything. The write step posts one
        event marked ``"test": true`` through the same signed send as every
        notification; it lands in the customer's Zap or endpoint, so it needs
        a test account.
        """
        from urllib.parse import urlsplit

        from app.connectors.base import verify_helpers as vh
        from app.core.exceptions import ConnectorConfigError

        def address() -> tuple[str, dict]:
            raw = self.base_url
            if not raw:
                raise ConnectorConfigError("No webhook URL is set on this connection.")
            parts = urlsplit(raw)
            if parts.scheme != "https" or not parts.hostname:
                raise ConnectorConfigError("The webhook URL must start with https:// and name a host.")
            zapier = parts.hostname == "hooks.zapier.com"
            if zapier and not parts.path.startswith("/hooks/catch/"):
                raise ConnectorConfigError(
                    "This Zapier address is not a catch hook; it should start with "
                    "https://hooks.zapier.com/hooks/catch/."
                )
            signed = bool(self.credentials.get("signingSecret"))
            return (
                "Checked the webhook address (HTTPS"
                + (", a Zapier catch hook" if zapier else "")
                + ") without sending anything; a webhook has nothing to read.",
                {"zapier": zapier, "signed": signed},
            )

        def ping() -> tuple[str, dict]:
            if not self.base_url:
                raise ConnectorConfigError("No webhook URL is set on this connection.")
            self._post_signed(
                {
                    "event": "connection.test",
                    "subject": "Synaprank.ai verification ping",
                    "message": "A connection check. No agent acted; this can be ignored.",
                    "test": True,
                    "sent_at": utcnow().isoformat(),
                    "source": "automarket-ai",
                }
            )
            signed = bool(self.credentials.get("signingSecret"))
            return (
                "Sent one test event marked as a verification ping"
                + (", signed with the signing secret" if signed else "")
                + ", and the webhook accepted it.",
                {"signed": signed, "test": True},
            )

        return [
            vh.read_step("check_address", "Check the webhook address", address),
            vh.write_step(
                "test_event",
                "Send one marked test event to the webhook",
                ping,
                requires_test_account=True,
            ),
        ]


CONNECTOR_CLASS = ZapierWebhooksConnector
