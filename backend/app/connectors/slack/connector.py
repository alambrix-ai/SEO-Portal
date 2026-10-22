"""Slack connector — where agents tell a human what they just did.

This is the channel behind the Configure dialog's "Notify channel on action",
and the practical difference between autonomous agents people trust and
autonomous agents people switch off: every consequential action can announce
itself where the team already works.
"""
from __future__ import annotations

from app.connectors.base.connector import (
    Capability,
    ConnectorSpec,
    HealthReport,
    OAuthBinding,
    OAuthChoice,
)
from app.connectors.base import verify_helpers as vh
from app.connectors.base.credentials import text
from app.connectors.base.oauth import OAuthTokenMixin, token_fields
from app.connectors.base.interfaces import NotificationConnector
from app.core.exceptions import ConnectorError
from app.core.logging import get_logger
from app.db.base import utcnow

log = get_logger(__name__)

#: Pages of conversations.list followed for the channel picker.
_MAX_CHANNEL_PAGES = 10


def parse_channels(data: dict) -> list[OAuthChoice]:
    """Public, unarchived channels out of a ``conversations.list`` page."""
    out: list[OAuthChoice] = []
    for row in data.get("channels") or []:
        channel_id = str(row.get("id") or "")
        name = str(row.get("name") or "")
        if not channel_id or not name or row.get("is_archived"):
            continue
        members = row.get("num_members")
        out.append(
            OAuthChoice(
                id=channel_id,
                label=f"#{name}",
                detail=f"{members} members" if isinstance(members, int) else "",
                values={"defaultChannel": channel_id},
            )
        )
    return out


class SlackConnector(vh.EnvironmentChoiceMixin, OAuthTokenMixin, NotificationConnector):
    #: Slack has no separate sandbox address; a developer-sandbox or spare
    #: workspace is marked "Test account" so the live check may post there.
    environments = (vh.TEST_ACCOUNT,)

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
            vh.environment_field(
                allowed=(vh.TEST_ACCOUNT,),
                help_text=(
                    "Leave blank for your real workspace. Type Test account only for a "
                    "workspace made for trying things out, such as a Slack developer "
                    "sandbox: connection checks may then post one message there and "
                    "delete it straight away."
                ),
            ),
        ),
        capabilities=frozenset({Capability.SEND_NOTIFICATION}),
        docs_url="https://api.slack.com/methods/chat.postMessage",
        base_url="https://slack.com/api",
        # chat:write posts; chat:write.public posts to a public channel the
        # app has not been invited to; channels:read lists public channels
        # for the picker. auth.test (the health check) needs no scope.
        oauth=OAuthBinding(
            provider="slack",
            scopes=("chat:write", "chat:write.public", "channels:read"),
            platform_fields=frozenset(),
            token_fields=frozenset({"accessToken"}),
            button_label="Add to Slack",
            access_summary=(
                "Lets Synaprank post updates and approval requests to the channel you "
                "pick. It can see your public channel names but cannot read messages."
            ),
            choice_noun="channel",
            choice_fields=("defaultChannel",),
        ),
    )

    # ── Sign-in ────────────────────────────────────────────────────────────
    def list_oauth_choices(self) -> list[OAuthChoice]:
        """Public channels in the workspace the app was added to."""
        choices: list[OAuthChoice] = []
        cursor = ""
        for _ in range(_MAX_CHANNEL_PAGES):
            params = {"types": "public_channel", "exclude_archived": "true", "limit": 200}
            if cursor:
                params["cursor"] = cursor
            data = self.request("GET", "/conversations.list", params=params) or {}
            if not data.get("ok"):
                raise ConnectorError(f"Slack could not list channels: {data.get('error')}")
            choices.extend(parse_channels(data))
            cursor = str((data.get("response_metadata") or {}).get("next_cursor") or "")
            if not cursor:
                break
        return sorted(choices, key=lambda c: c.label)

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
        self._post_message(target, subject=subject, message=message)
        return True

    def _post_message(self, target: str, *, subject: str, message: str) -> dict:
        """``chat.postMessage`` as agents post; returns Slack's reply (channel id, ts)."""
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
                            {"type": "mrkdwn", "text": "Posted by *Synaprank.ai*"}
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
        return payload

    # ── Live checks ────────────────────────────────────────────────────────
    def _channel_id(self) -> str:
        """The configured channel as an id; a typed ``#name`` is looked up."""
        configured = self.credentials.require("defaultChannel").strip()
        if not configured.startswith("#") and configured[:1] in ("C", "G") and configured.isalnum():
            return configured
        label = "#" + configured.lstrip("#")
        for choice in self.list_oauth_choices():
            if choice.label == label:
                return choice.id
        raise ConnectorError(
            f"Slack has no public channel called {label} that this app can see. "
            "Pick the channel again under the connection."
        )

    def verify_steps(self) -> list:
        """Read the workspace and the channel; on a test workspace, post and delete one message.

        Reads: ``auth.test`` (no scope) and ``conversations.info``
        (``channels:read``). The write step posts through the same
        ``chat.postMessage`` call agents use, then removes it with
        ``chat.delete`` — a bot token may delete its own messages
        (``chat:write``). Even a deleted message can notify channel members
        for a moment, so it runs only on a connection marked "Test account".
        https://docs.slack.dev/reference/methods/chat.delete
        """

        def workspace() -> tuple[str, dict]:
            payload = self.request("POST", "/auth.test") or {}
            if not payload.get("ok"):
                raise ConnectorError(f"Slack did not accept the sign-in: {payload.get('error')}")
            return "Signed in to the Slack workspace.", {
                "team_id": payload.get("team_id"),
                "bot_id": payload.get("bot_id"),
            }

        def channel() -> tuple[str, dict]:
            channel_id = self._channel_id()
            payload = self.request("GET", "/conversations.info", params={"channel": channel_id}) or {}
            if not payload.get("ok"):
                raise ConnectorError(f"Slack could not read the channel: {payload.get('error')}")
            info = payload.get("channel") or {}
            if info.get("is_archived"):
                raise ConnectorError("The notification channel is archived; pick another one.")
            return "Read the channel notifications go to.", {"channel_id": channel_id}

        def ping() -> tuple[str, dict]:
            vh.refuse_unless_test(self, "it posts a message to the channel, which can notify people.")
            channel_id = self._channel_id()
            text_ = "Synaprank.ai verification ping — safe to ignore"
            posted: dict = {}

            def create() -> str:
                posted.update(self._post_message(
                    channel_id, subject=text_, message=f"{vh.marker('message')}. It is deleted right away."
                ))
                return str(posted.get("ts") or "")

            def delete(ts: str) -> None:
                payload = self.request(
                    "POST", "/chat.delete", json_body={"channel": posted.get("channel") or channel_id, "ts": ts}
                ) or {}
                if not payload.get("ok"):
                    raise ConnectorError(f"Slack would not delete the message: {payload.get('error')}")

            evidence = vh.create_check_delete(what="message", create=create, check=None, delete=delete)
            return "Posted one message to the channel and deleted it straight away.", evidence

        return [
            vh.read_step("auth_test", "Sign in to the Slack workspace", workspace),
            vh.read_step("read_channel", "Read the channel notifications go to", channel),
            vh.write_step(
                "post_and_delete_message",
                "Post one message to the channel, then delete it",
                ping,
                requires_test_account=True,
            ),
        ]

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
