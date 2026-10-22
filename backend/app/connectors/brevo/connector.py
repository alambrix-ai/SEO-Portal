"""Brevo connector — API v3, draft email campaigns only.

``POST /v3/emailCampaigns`` creates a campaign that stays a draft unless a
``scheduledAt`` is given — this connector never sends one, and never calls the
send-now or test-send endpoints. Nothing reaches a contact until somebody
sends the draft from Brevo.

Endpoints (https://developers.brevo.com/reference):

* ``POST /v3/emailCampaigns`` — create the draft
* ``GET  /v3/account`` and ``GET /v3/contacts/lists/{id}`` — health

Auth is the ``api-key`` header.
"""
from __future__ import annotations

from app.connectors.base.connector import SetupGuide
from app.connectors.base.connector import BaseConnector, Capability, ConnectorSpec, HealthReport
from app.connectors.base.credentials import secret, text
from app.core.exceptions import ConnectorConfigError, ConnectorError
from app.core.logging import get_logger
from app.db.base import utcnow

log = get_logger(__name__)


GUIDE = SetupGuide(
    steps=(
        'Open the API keys page in your Brevo account with the button below.',
        'Click “Generate a new API key”, name it Synaprank, and click “Generate”. Brevo may send you a verification code first.',
        'Copy the key and paste it here, then fill in the sender name and email your campaigns come from.',
    ),
    key_url='https://app.brevo.com/settings/keys/api',
    key_url_label='Open your Brevo API keys',
    key_field='apiKey',
    key_pattern='^xkeysib-[A-Za-z0-9\\-]{20,}$',
    key_hint='Brevo API keys start with xkeysib-',
)


class BrevoConnector(BaseConnector):
    spec = ConnectorSpec(
        slug="brevo",
        guide=GUIDE,
        name="Brevo",
        category="Email marketing",
        description=(
            "Creates draft email campaigns in Brevo (formerly Sendinblue). Nothing "
            "is sent: you review and send each draft in Brevo."
        ),
        fields=(
            secret(
                "apiKey",
                "API key",
                "xkeysib-••••••••",
                help_text="Brevo → your name → SMTP & API → API keys → Generate a new API key.",
            ),
            text(
                "senderName",
                "Sender name",
                "Your Brand",
                required=False,
                help_text="Leave blank to send with the sender email alone; Brevo does not require a name.",
            ),
            text(
                "senderEmail",
                "Sender email",
                "hello@yourdomain.com",
                help_text="Must be a sender you have added and verified in Brevo (Senders, domains & dedicated IPs).",
            ),
            text(
                "listIds",
                "Contact list IDs (optional)",
                "2, 7",
                required=False,
                help_text=(
                    "Numbers from Contacts → Lists, separated by commas. Drafts are "
                    "addressed to these lists; leave blank to choose recipients in Brevo."
                ),
            ),
        ),
        capabilities=frozenset({Capability.CREATE_EMAIL_CAMPAIGN}),
        requirements=(
            "A Brevo API key (v3) from the account that owns the contact lists.",
            "The sender email must be a verified sender in that Brevo account.",
        ),
        docs_url="https://developers.brevo.com/reference/createemailcampaign",
        base_url="https://api.brevo.com/v3",
    )

    def auth_headers(self) -> dict[str, str]:
        return {
            "api-key": self.credentials.require("apiKey").strip(),
            "Content-Type": "application/json",
        }

    @property
    def list_ids(self) -> list[int]:
        raw = self.credentials.get("listIds")
        out: list[int] = []
        for part in raw.replace(";", ",").split(","):
            part = part.strip()
            if not part:
                continue
            if not part.isdigit():
                raise ConnectorConfigError(
                    f"Brevo list IDs are numbers, separated by commas — {part!r} is not one."
                )
            out.append(int(part))
        return out

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
        """Create a draft email campaign with this content. Never sends it.

        Brevo builds the plain-text part itself, so ``text`` is not uploaded.
        Returns the campaign id; Brevo returns no link, so the URL is "".
        """
        sender_email = self.credentials.require("senderEmail").strip()
        body: dict = {
            "name": name[:255],
            "subject": subject,
            # sender.name is optional in Brevo's API; only the address is needed.
            # https://developers.brevo.com/reference/create-email-campaign
            "sender": {
                **({"name": n} if (n := (from_name or self.credentials.get("senderName") or "").strip()) else {}),
                "email": sender_email,
            },
            "htmlContent": html,
            "previewText": preview_text,
            "replyTo": reply_to or sender_email,
        }
        lists = self.list_ids
        if lists:
            body["recipients"] = {"listIds": lists}
        created = self.request("POST", "/emailCampaigns", json_body=body)
        campaign_id = (created or {}).get("id")
        if not campaign_id:
            raise ConnectorError("Brevo did not return the new campaign. Try again.")
        return str(campaign_id), ""

    # ── Lifecycle ──────────────────────────────────────────────────────────
    def check_health(self) -> HealthReport:
        try:
            account = self.request("GET", "/account")
            for list_id in self.list_ids:
                self.request("GET", f"/contacts/lists/{list_id}")
        except ConnectorError as exc:
            return HealthReport(ok=False, detail=self.diagnose(exc) or str(exc), checked_at=utcnow())
        who = str((account or {}).get("companyName") or (account or {}).get("email") or "").strip()
        return HealthReport(ok=True, detail=f"Connected as {who}" if who else "Connected", checked_at=utcnow())

    def verify_steps(self) -> list:
        """Read the account, the sender and the lists; round-trip one draft campaign.

        The write step goes through ``create_campaign_draft`` (the email
        marketer's call, with no ``scheduledAt``, so it stays a draft), reads
        it back with ``GET /emailCampaigns/{id}`` (status must be ``draft``) and
        removes it with ``DELETE /emailCampaigns/{id}``. Nothing is sent.
        https://developers.brevo.com/reference/create-email-campaign
        """
        from app.connectors.base import verify_helpers as vh

        def account() -> tuple[str, dict]:
            data = self.request("GET", "/account") or {}
            plans = [str(p.get("type") or "") for p in data.get("plan") or [] if isinstance(p, dict)]
            return "Reached the Brevo account.", {"plan_types": [p for p in plans if p]}

        def sender() -> tuple[str, dict]:
            wanted = self.credentials.require("senderEmail").strip().lower()
            data = self.request("GET", "/senders") or {}
            rows = data.get("senders") or []
            match = next((s for s in rows if str(s.get("email") or "").strip().lower() == wanted), None)
            if match is None:
                raise ConnectorError(
                    "The sender email is not one of this Brevo account's senders. Add and "
                    "verify it under Senders, domains & dedicated IPs."
                )
            if not match.get("active"):
                raise ConnectorError("The sender email is in Brevo but not yet verified (inactive).")
            return "The sender email is a verified, active sender in Brevo.", {"sender_id": match.get("id")}

        def lists() -> tuple[str, dict]:
            ids = self.list_ids
            total = 0
            for list_id in ids:
                row = self.request("GET", f"/contacts/lists/{list_id}") or {}
                count = row.get("totalSubscribers")
                total += count if isinstance(count, int) else 0
            if not ids:
                return "No contact lists are set; drafts are saved without recipients.", {"list_ids": []}
            return (
                f"Read {vh.plural(len(ids), 'contact list')} drafts are addressed to.",
                {"list_ids": ids, "subscribers": total},
            )

        def draft() -> tuple[str, dict]:
            def create() -> str:
                campaign_id, _ = self.create_campaign_draft(
                    name=vh.marker("draft"),
                    subject=vh.marker("draft"),
                    preview_text="Temporary draft made by a connection check. Safe to delete.",
                    html="<html><body><p>Temporary draft made by a connection check. Safe to delete.</p></body></html>",
                    text="",
                    from_name="",
                    reply_to="",
                )
                return campaign_id

            def check(campaign_id: str) -> None:
                row = self.request(
                    "GET", f"/emailCampaigns/{campaign_id}", params={"excludeHtmlContent": "true"}
                ) or {}
                if row.get("status") != "draft":
                    raise ConnectorError(f"The campaign came back as {row.get('status')!r}, not a draft.")

            def delete(campaign_id: str) -> None:
                self.request("DELETE", f"/emailCampaigns/{campaign_id}", expect_json=False)

            evidence = vh.create_check_delete(
                what="draft campaign", create=create, check=check, delete=delete
            )
            return "Saved a draft campaign, read it back, and deleted it. Nothing was sent.", evidence

        return [
            vh.read_step("account", "Reach the Brevo account", account),
            vh.read_step("sender", "Check the sender email is verified in Brevo", sender),
            vh.read_step("contact_lists", "Read the contact lists drafts are addressed to", lists),
            vh.write_step(
                "draft_campaign_roundtrip",
                "Save a draft campaign, read it back, then delete it",
                draft,
                requires_test_account=False,
            ),
        ]

    def diagnose(self, error: Exception) -> str | None:
        text_ = str(error).lower()
        if "list ids are numbers" in text_:
            return str(error)
        if "credentials were rejected" in text_ or "401" in text_:
            return (
                "Brevo rejected the API key. Generate a new v3 key under SMTP & API → "
                "API keys (an SMTP key will not work)."
            )
        if "could not be found" in text_ or "404" in text_:
            return "One of the contact list IDs does not exist in this Brevo account. Check them under Contacts → Lists."
        if "access was refused" in text_ or "403" in text_:
            return (
                "Brevo refused the request. If your account limits API access to known "
                "IP addresses, add this server's address under Security → Authorised IPs."
            )
        if "could not be understood" in text_ or "400" in text_:
            return (
                "Brevo did not accept the campaign. Check that the sender email is a "
                "verified sender in Brevo and that the list IDs are correct."
            )
        return None


CONNECTOR_CLASS = BrevoConnector
