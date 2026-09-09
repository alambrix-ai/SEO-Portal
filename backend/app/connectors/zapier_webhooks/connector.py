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

from app.connectors.base.connector import Capability, ConnectorSpec, HealthReport
from app.connectors.base.credentials import secret, text
from app.connectors.base.interfaces import NotificationConnector
from app.core.exceptions import ConnectorError
from app.core.logging import get_logger
from app.db.base import utcnow

log = get_logger(__name__)


class ZapierWebhooksConnector(NotificationConnector):
    spec = ConnectorSpec(
        slug="zapier_webhooks",
        name="Webhooks / Zapier",
        category="Collaboration",
        description=(
            "POST every agent action to a webhook — Zapier, Make, or your own "
            "endpoint. Payloads are HMAC-signed."
        ),
        fields=(
            text("webhookUrl", "Webhook URL", "https://hooks.zapier.com/hooks/catch/••••"),
            secret("signingSecret", "Signing secret", "••••••••", required=False),
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

    def notify(self, *, subject: str, message: str, channel: str = "") -> bool:
        payload = {
            "event": "agent.action",
            "subject": subject,
            "message": message,
            "target": channel,
            "sent_at": utcnow().isoformat(),
            "source": "automarket-ai",
        }
        body = json.dumps(payload, separators=(",", ":"), default=str)

        if not self.base_url:
            raise ConnectorError("No webhook URL configured")

        self.request(
            "POST",
            "",
            json_body=payload,
            headers=self._signature_headers(body),
            # Catch hooks commonly answer with a bare "ok" rather than JSON.
            expect_json=False,
        )
        return True

    def check_health(self) -> HealthReport:
        if not self.base_url:
            return HealthReport(ok=False, detail="No webhook URL configured", checked_at=utcnow())
        try:
            # A ping event, so a health check does not look like a real action
            # to whatever is on the other end.
            self.request(
                "POST",
                "",
                json_body={"event": "connection.test", "source": "automarket-ai"},
                headers=self._signature_headers('{"event":"connection.test"}'),
                expect_json=False,
            )
        except ConnectorError as exc:
            return HealthReport(ok=False, detail=str(exc), checked_at=utcnow())
        return HealthReport(ok=True, detail="Webhook accepted a test payload", checked_at=utcnow())


CONNECTOR_CLASS = ZapierWebhooksConnector
