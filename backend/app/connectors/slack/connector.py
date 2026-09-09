"""Slack connector — where agents tell a human what they just did.

This is the channel behind the Configure dialog's "Notify channel on action",
and the practical difference between autonomous agents people trust and
autonomous agents people switch off: every consequential action can announce
itself where the team already works.
"""
from __future__ import annotations

from app.connectors.base.connector import Capability, ConnectorSpec, HealthReport
from app.connectors.base.credentials import oauth, text
from app.connectors.base.oauth import OAuthTokenMixin, token_fields
from app.connectors.base.interfaces import NotificationConnector
from app.core.exceptions import ConnectorError
from app.core.logging import get_logger
from app.db.base import utcnow

log = get_logger(__name__)


class SlackConnector(OAuthTokenMixin, NotificationConnector):
    spec = ConnectorSpec(
        slug="slack",
        name="Slack",
        category="Collaboration",
        description="Post agent actions and approval requests into a Slack channel.",
        fields=(
            text(
                "defaultChannel",
                "Default channel",
                "#marketing-automation",
                help_text=(
                    "Where agent notifications go. Required: guessing a "
                    "channel means posting somewhere nobody chose."
                ),
            ),
            # The credential this connector has always required. It used
            # to be an "Authorize with Slack" button that set a
            # boolean in the browser and obtained nothing.
            *token_fields(
                refreshable=False,
                vendor='Slack',
                docs="Your Slack app's Bot User OAuth Token, starting xoxb-.",
            ),
        ),
        capabilities=frozenset({Capability.SEND_NOTIFICATION}),
        docs_url="https://api.slack.com/methods/chat.postMessage",
        base_url="https://slack.com/api",
    )

    def auth_headers(self) -> dict[str, str]:
        return {
            "Authorization": "Bearer " + self.access_token,
            "Content-Type": "application/json; charset=utf-8",
        }

    def notify(self, *, subject: str, message: str, channel: str = "") -> bool:
        target = channel or self.credentials.require("defaultChannel")
        # An email address arrives here when the PR agent has no mail
        # connector; Slack cannot deliver to one, so fall back to the channel.
        if "@" in target:
            target = self.credentials.require("defaultChannel")

        response = self.request(
            "POST",
            "/chat.postMessage",
            json_body={
                "channel": target,
                "text": subject,
                # Blocks render the body readably; `text` remains the
                # notification preview and the accessibility fallback.
                "blocks": [
                    {
                        "type": "header",
                        "text": {"type": "plain_text", "text": subject[:150]},
                    },
                    {
                        "type": "section",
                        "text": {"type": "mrkdwn", "text": message[:2900]},
                    },
                    {
                        "type": "context",
                        "elements": [
                            {"type": "mrkdwn", "text": "Posted by *AutoMarket AI*"}
                        ],
                    },
                ],
            },
        )
        payload = response if isinstance(response, dict) else {}
        if not payload.get("ok"):
            # Slack returns HTTP 200 with ok:false, so the status code alone
            # would not have caught this.
            raise ConnectorError(f"Slack rejected the message: {payload.get('error')}")
        return True

    def check_health(self) -> HealthReport:
        try:
            response = self.request("POST", "/auth.test")
        except ConnectorError as exc:
            return HealthReport(ok=False, detail=str(exc), checked_at=utcnow())
        payload = response if isinstance(response, dict) else {}
        if not payload.get("ok"):
            return HealthReport(
                ok=False, detail=str(payload.get("error") or "auth.test failed"), checked_at=utcnow()
            )
        return HealthReport(
            ok=True,
            detail=f"Connected to {payload.get('team') or 'workspace'}",
            checked_at=utcnow(),
        )


CONNECTOR_CLASS = SlackConnector
