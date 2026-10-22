"""Mailchimp connector — Marketing API v3, draft campaigns only.

Creates a *regular* campaign addressed to one audience and uploads its HTML.
The campaign stays a draft: this connector never calls the send, schedule or
test-send endpoints, so nothing reaches a subscriber until somebody sends it
from Mailchimp.

Endpoints (https://mailchimp.com/developer/marketing/api/):

* ``POST /3.0/campaigns`` — create the draft (type, recipients, settings)
* ``PUT  /3.0/campaigns/{id}/content`` — set its HTML and plain text
* ``DELETE /3.0/campaigns/{id}`` — remove a half-made draft if the content upload fails
* ``GET  /3.0/ping`` and ``GET /3.0/lists/{id}`` — health

Auth is HTTP Basic with any user name and the API key as the password. The
key ends in ``-<data centre>`` (for example ``-us21``), which is the API host.
"""
from __future__ import annotations

import base64
import re

from app.connectors.base.connector import (
    BaseConnector,
    Capability,
    ConnectorSpec,
    HealthReport,
    OAuthBinding,
    OAuthChoice,
    SetupGuide,
)
from app.connectors.base.credentials import from_platform_sign_in, secret, text
from app.core.exceptions import ConnectorConfigError, ConnectorError
from app.core.logging import get_logger
from app.db.base import utcnow

log = get_logger(__name__)

#: A data centre code as Mailchimp issues them: letters then digits (us21).
#: Checked strictly because it becomes the API host name.
_DATA_CENTRE = re.compile(r"^[a-z]{2,5}[0-9]{1,3}$")


GUIDE = SetupGuide(
    steps=(
        'Open the API keys page in your Mailchimp account with the button below (Profile → Extras → API keys).',
        'Click “Create A Key”, name it Synaprank, and click “Generate Key”.',
        'Click “Copy Key to Clipboard” and paste it here. Mailchimp shows it only once.',
    ),
    key_url='https://admin.mailchimp.com/account/api/',
    key_url_label='Open your Mailchimp API keys',
    key_field='apiKey',
    key_pattern='^[0-9a-f]{32}-us[0-9]{1,2}$',
    key_hint='Mailchimp keys end with -us and a number, like -us21',
)


def parse_audiences(data: dict) -> list[OAuthChoice]:
    """Audiences out of ``GET /lists``, with their campaign defaults.

    Mailchimp requires a from name and from address on every audience, so
    the draft's sender is taken from there rather than asked for.
    """
    out: list[OAuthChoice] = []
    for row in data.get("lists") or []:
        list_id = str(row.get("id") or "")
        if not list_id:
            continue
        defaults = row.get("campaign_defaults") or {}
        members = (row.get("stats") or {}).get("member_count")
        values = {"audienceId": list_id}
        if defaults.get("from_name"):
            values["fromName"] = str(defaults["from_name"])
        if defaults.get("from_email"):
            values["replyTo"] = str(defaults["from_email"])
        out.append(
            OAuthChoice(
                id=list_id,
                label=str(row.get("name") or list_id),
                detail=f"{members} contacts" if isinstance(members, int) else "",
                values=values,
            )
        )
    return out


class MailchimpConnector(BaseConnector):
    spec = ConnectorSpec(
        slug="mailchimp",
        guide=GUIDE,
        name="Mailchimp",
        category="Email marketing",
        description=(
            "Creates draft email campaigns for one Mailchimp audience. Nothing is "
            "sent: you review and send each draft in Mailchimp."
        ),
        fields=(
            secret(
                "apiKey",
                "API key",
                "0123456789abcdef-us21",
                help_text=(
                    "Mailchimp → Profile → Extras → API keys → Create a key. The part "
                    "after the dash (for example us21) tells us which Mailchimp server "
                    "your account is on."
                ),
            ),
            text(
                "audienceId",
                "Audience ID",
                "a1b2c3d4e5",
                help_text=(
                    "Audience → Settings → Audience name and defaults → Audience ID. "
                    "Drafts are addressed to this audience."
                ),
            ),
            text(
                "fromName",
                "From name",
                "Your Brand",
                required=False,
                help_text="Leave blank to use the audience's default from name.",
            ),
            text(
                "replyTo",
                "Reply-to email",
                "hello@yourdomain.com",
                required=False,
                help_text=(
                    "Where replies go. Use an address on a domain you have verified in "
                    "Mailchimp. Leave blank to use the audience's default from email."
                ),
            ),
        ),
        capabilities=frozenset({Capability.CREATE_EMAIL_CAMPAIGN}),
        requirements=(
            "A Mailchimp account with at least one audience, and an API key "
            "created in that same account.",
            "Mailchimp only sends from a verified domain: verify the domain of "
            "the reply-to address in Mailchimp before you send a draft.",
        ),
        docs_url="https://mailchimp.com/developer/marketing/api/campaigns/",
        # Mailchimp OAuth has no scopes: the token acts as the account. The
        # connector itself only creates drafts and never sends.
        oauth=OAuthBinding(
            provider="mailchimp",
            platform_fields=frozenset({"apiKey"}),
            token_fields=frozenset({"apiKey", "accessToken"}),
            access_summary=(
                "Lets Synaprank save email drafts to the audience you pick. Mailchimp "
                "grants full account access to connected apps; nothing is ever sent "
                "from here — you review and send each draft yourself."
            ),
            choice_noun="audience",
            choice_fields=("audienceId",),
        ),
    )

    # ── Account ────────────────────────────────────────────────────────────
    @property
    def _signed_in(self) -> bool:
        """Connected with Mailchimp's sign-in rather than a pasted key."""
        return (
            from_platform_sign_in(self.credentials.values)
            and not self.credentials.get("apiKey")
            and bool(self.credentials.get("accessToken"))
        )

    @property
    def data_centre(self) -> str:
        if self._signed_in:
            centre = (self.credentials.get("dataCentre") or "").strip().lower()
            if not _DATA_CENTRE.match(centre):
                raise ConnectorConfigError(
                    "Mailchimp did not say which server this account is on. Connect it again."
                )
            return centre
        key = self.credentials.require("apiKey").strip()
        centre = key.rsplit("-", 1)[-1].lower() if "-" in key else ""
        if not _DATA_CENTRE.match(centre):
            raise ConnectorConfigError(
                "That Mailchimp API key is incomplete — it should end with a dash and "
                "your server code, for example -us21. Copy the whole key again."
            )
        return centre

    @property
    def base_url(self) -> str:
        return f"https://{self.data_centre}.api.mailchimp.com/3.0"

    def auth_headers(self) -> dict[str, str]:
        if self._signed_in:
            return {
                "Authorization": f"Bearer {self.credentials.require('accessToken')}",
                "Content-Type": "application/json",
            }
        token = base64.b64encode(f"anystring:{self.credentials.require('apiKey').strip()}".encode()).decode()
        return {"Authorization": f"Basic {token}", "Content-Type": "application/json"}

    # ── Sign-in ────────────────────────────────────────────────────────────
    def list_oauth_choices(self) -> list[OAuthChoice]:
        """The account's audiences (``GET /lists``), with sender defaults."""
        choices: list[OAuthChoice] = []
        fields = (
            "lists.id,lists.name,lists.stats.member_count,"
            "lists.campaign_defaults.from_name,lists.campaign_defaults.from_email,total_items"
        )
        for page in range(10):
            data = self.request(
                "GET", "/lists", params={"count": 100, "offset": page * 100, "fields": fields}
            ) or {}
            batch = parse_audiences(data)
            choices.extend(batch)
            if len(batch) < 100:
                break
        return choices

    # ── Campaigns ──────────────────────────────────────────────────────────
    def create_campaign_draft(
        self,
        *,
        name: str,
        subject: str,
        preview_text: str,
        html: str,
        text: str,
        from_name: str,
        reply_to: str,
    ) -> tuple[str, str]:
        """Create a draft regular campaign with this content. Never sends it.

        Returns the campaign id and the link that opens it in Mailchimp's editor.
        """
        created = self.request(
            "POST",
            "/campaigns",
            json_body={
                "type": "regular",
                "recipients": {"list_id": self.credentials.require("audienceId").strip()},
                "settings": {
                    "subject_line": subject,
                    "preview_text": preview_text,
                    "title": name[:100],
                    **self._sender(from_name=from_name, reply_to=reply_to),
                },
            },
        )
        campaign_id = str((created or {}).get("id") or "")
        if not campaign_id:
            raise ConnectorError("Mailchimp did not return the new campaign. Try again.")
        try:
            self.request(
                "PUT",
                f"/campaigns/{campaign_id}/content",
                json_body={"html": html, "plain_text": text},
            )
        except ConnectorError:
            # An empty draft left behind would be sent blank by somebody who
            # trusted it; remove it and report the failure.
            try:
                self.request("DELETE", f"/campaigns/{campaign_id}", expect_json=False)
            except ConnectorError:
                log.warning("Could not remove the incomplete Mailchimp draft %s", campaign_id)
            raise
        web_id = (created or {}).get("web_id")
        url = f"https://{self.data_centre}.admin.mailchimp.com/campaigns/edit?id={web_id}" if web_id else ""
        return campaign_id, url

    def _sender(self, *, from_name: str, reply_to: str) -> dict[str, str]:
        """``from_name`` and ``reply_to`` for a draft, blanks filled from the audience.

        Both are optional when a campaign is created, and ``reply_to`` is
        required to send. Every audience carries ``campaign_defaults``
        (``from_name``, ``from_email``), which is what Mailchimp itself offers
        for a new campaign, so a blank setting takes the audience's default
        rather than failing.
        https://mailchimp.com/developer/marketing/api/campaigns/add-campaign/
        https://mailchimp.com/developer/marketing/api/lists/get-list-info/
        """
        name = (from_name or self.credentials.get("fromName") or "").strip()
        reply = (reply_to or self.credentials.get("replyTo") or "").strip()
        if not name or not reply:
            audience = self.credentials.require("audienceId").strip()
            data = self.request(
                "GET",
                f"/lists/{audience}",
                params={"fields": "campaign_defaults.from_name,campaign_defaults.from_email"},
            ) or {}
            defaults = data.get("campaign_defaults") or {}
            name = name or str(defaults.get("from_name") or "").strip()
            reply = reply or str(defaults.get("from_email") or "").strip()
        out: dict[str, str] = {}
        if name:
            out["from_name"] = name
        if reply:
            out["reply_to"] = reply
        return out

    # ── Lifecycle ──────────────────────────────────────────────────────────
    def check_health(self) -> HealthReport:
        try:
            self.request("GET", "/ping")
            audience = self.request(
                "GET",
                f"/lists/{self.credentials.require('audienceId').strip()}",
                params={"fields": "id,name"},
            )
        except ConnectorError as exc:
            return HealthReport(ok=False, detail=self.diagnose(exc) or str(exc), checked_at=utcnow())
        name = str((audience or {}).get("name") or "").strip()
        return HealthReport(
            ok=True,
            detail=f"Connected — drafts go to the audience “{name}”" if name else "Connected",
            checked_at=utcnow(),
        )

    def verify_steps(self) -> list:
        """Ping the account, read the audience, and round-trip one draft campaign.

        The write step goes through ``create_campaign_draft`` — the call the
        email marketer makes — then reads the campaign back
        (``GET /campaigns/{id}``, status must still be ``save``, i.e. a draft)
        and removes it with ``DELETE /campaigns/{id}``. Nothing is sent,
        scheduled or test-sent; a draft is visible only inside the account.
        https://mailchimp.com/developer/marketing/api/campaigns/
        """
        from app.connectors.base import verify_helpers as vh

        def ping() -> tuple[str, dict]:
            data = self.request("GET", "/ping") or {}
            if not data.get("health_status"):
                raise ConnectorError("Mailchimp answered, but not with its health status.")
            return f"Reached Mailchimp on server {self.data_centre}.", {"data_centre": self.data_centre}

        def audience() -> tuple[str, dict]:
            audience_id = self.credentials.require("audienceId").strip()
            data = self.request(
                "GET", f"/lists/{audience_id}", params={"fields": "id,name,stats.member_count"}
            ) or {}
            members = (data.get("stats") or {}).get("member_count")
            count = members if isinstance(members, int) else 0
            return (
                f"Read the audience drafts are addressed to ({vh.plural(count, 'contact')}).",
                {"audience_id": str(data.get("id") or audience_id), "member_count": count},
            )

        def draft() -> tuple[str, dict]:
            def create() -> str:
                campaign_id, _ = self.create_campaign_draft(
                    name=vh.marker("draft"),
                    subject=vh.marker("draft"),
                    preview_text="Temporary draft made by a connection check. Safe to delete.",
                    html="<p>Temporary draft made by a connection check. Safe to delete.</p>",
                    text="Temporary draft made by a connection check. Safe to delete.",
                    from_name="",
                    reply_to="",
                )
                return campaign_id

            def check(campaign_id: str) -> None:
                row = self.request(
                    "GET", f"/campaigns/{campaign_id}", params={"fields": "id,status,recipients.list_id"}
                ) or {}
                if row.get("status") != "save":
                    raise ConnectorError(f"The campaign came back as {row.get('status')!r}, not a draft.")

            def delete(campaign_id: str) -> None:
                self.request("DELETE", f"/campaigns/{campaign_id}", expect_json=False)

            evidence = vh.create_check_delete(
                what="draft campaign", create=create, check=check, delete=delete
            )
            return "Saved a draft campaign with content, read it back, and deleted it. Nothing was sent.", evidence

        return [
            vh.read_step("ping", "Reach the Mailchimp account", ping),
            vh.read_step("read_audience", "Read the audience drafts are addressed to", audience),
            vh.write_step(
                "draft_campaign_roundtrip",
                "Save a draft campaign, read it back, then delete it",
                draft,
                requires_test_account=False,
            ),
        ]

    def diagnose(self, error: Exception) -> str | None:
        text_ = str(error).lower()
        if "server code" in text_:
            return str(error)
        if "credentials were rejected" in text_ or "401" in text_:
            return (
                "Mailchimp rejected the API key. Create a new key under Profile → Extras → "
                "API keys and paste all of it, including the part after the dash."
            )
        if "could not be found" in text_ or "404" in text_:
            return (
                "Mailchimp has no audience with that Audience ID in this account. Copy it "
                "from Audience → Settings → Audience name and defaults."
            )
        if "access was refused" in text_ or "403" in text_:
            return (
                "Mailchimp refused the request. The account may be suspended or the key "
                "may belong to a different account than the audience."
            )
        return None


CONNECTOR_CLASS = MailchimpConnector
