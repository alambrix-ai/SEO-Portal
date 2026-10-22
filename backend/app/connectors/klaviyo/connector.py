"""Klaviyo connector — saves emails as Klaviyo templates. It creates no campaigns.

Klaviyo's campaign API has changed shape across revisions (how the campaign
message and its content are defined, and how a template is attached to it),
so this connector deliberately does only the part whose shape is stable:
``POST /api/templates`` with a CODE template holding the email's HTML and
plain text. The person then creates the campaign in Klaviyo from that
template, choosing the audience, subject and send time there. Nothing is ever
sent from here.

Endpoints (https://developers.klaviyo.com/en/reference/api_overview):

* ``POST /api/templates`` — create the template (JSON:API body)
* ``GET  /api/accounts`` — health

Every request carries ``Authorization: Klaviyo-API-Key <private key>`` and a
``revision`` date header, which is a field so it can be moved forward without
a code change.
"""
from __future__ import annotations

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
from app.connectors.base.oauth import OAuthTokenMixin
from app.core.exceptions import ConnectorError
from app.core.logging import get_logger
from app.db.base import utcnow

log = get_logger(__name__)

#: The API revision sent when the field is blank: the newest stable one
#: (https://developers.klaviyo.com/en/docs/changelog_). Klaviyo keeps a
#: revision stable for a year and deprecated for another before retiring it
#: (https://developers.klaviyo.com/en/docs/api_versioning_and_deprecation_policy).
DEFAULT_REVISION = "2026-07-15"
#: Revisions the connect form used to fill in by itself (the field has a
#: default, and defaults are saved with the connection). A connection holding
#: exactly one of these never chose it, so it gets the current default; any
#: other revision someone typed is honoured.
PREVIOUS_DEFAULT_REVISIONS = frozenset({"2025-07-15"})
JSON_API = "application/vnd.api+json"


GUIDE = SetupGuide(
    steps=(
        'Open the API keys page in your Klaviyo account with the button below.',
        'Click “Create Private API Key”, name it Synaprank, and give it full access to Templates and read access to Accounts.',
        'Create the key, copy it and paste it here.',
    ),
    key_url='https://www.klaviyo.com/settings/account/api-keys',
    key_url_label='Open your Klaviyo API keys',
    key_field='apiKey',
    key_pattern='^pk_[A-Za-z0-9]{20,}$',
    key_hint='Klaviyo private keys start with pk_',
)


def parse_accounts(data: dict) -> list[OAuthChoice]:
    """The account a Klaviyo token belongs to (``GET /api/accounts``)."""
    out: list[OAuthChoice] = []
    for row in data.get("data") or []:
        account_id = str(row.get("id") or "")
        if not account_id:
            continue
        info = (row.get("attributes") or {}).get("contact_information") or {}
        name = str(info.get("organization_name") or "").strip()
        out.append(
            OAuthChoice(
                id=account_id,
                label=name or f"Klaviyo account {account_id}",
                detail=str(info.get("website_url") or ""),
                values={},
            )
        )
    return out


class KlaviyoConnector(OAuthTokenMixin, BaseConnector):
    spec = ConnectorSpec(
        slug="klaviyo",
        guide=GUIDE,
        name="Klaviyo",
        category="Email marketing",
        description=(
            "Saves each approved email as a template in Klaviyo. It does not create "
            "campaigns: you create the campaign in Klaviyo from the template and send it there."
        ),
        fields=(
            secret(
                "apiKey",
                "Private API key",
                "pk_••••••••",
                help_text=(
                    "Klaviyo → Settings → API keys → Create private API key, with full "
                    "access to Templates and read access to Accounts."
                ),
            ),
            text(
                "revision",
                "API revision (optional)",
                DEFAULT_REVISION,
                required=False,
                default=DEFAULT_REVISION,
                help_text="The Klaviyo API version date. Leave the default unless Klaviyo support asks you to change it.",
            ),
        ),
        capabilities=frozenset({Capability.CREATE_EMAIL_CAMPAIGN}),
        requirements=(
            "A Klaviyo private API key (public keys cannot create templates) with "
            "Templates full access and Accounts read access.",
            "Campaigns are created by you in Klaviyo from the saved template.",
        ),
        docs_url="https://developers.klaviyo.com/en/reference/create_template",
        base_url="https://a.klaviyo.com",
        # templates:write creates (and deletes) the template; templates:read
        # reads it back in the connection check; accounts:read is the health
        # check (and is a scope Klaviyo expects every app to request).
        oauth=OAuthBinding(
            provider="klaviyo",
            scopes=("accounts:read", "templates:read", "templates:write"),
            platform_fields=frozenset({"apiKey"}),
            token_fields=frozenset({"apiKey", "accessToken", "refreshToken"}),
            access_summary=(
                "Lets Synaprank save approved emails as templates in your Klaviyo "
                "account, read those templates back, and read the account's name. It "
                "cannot send emails or see your profiles."
            ),
            choice_noun="account",
        ),
    )

    token_url = "https://a.klaviyo.com/oauth/token"

    @property
    def _signed_in(self) -> bool:
        return from_platform_sign_in(self.credentials.values) and not self.credentials.get("apiKey")

    @property
    def revision(self) -> str:
        saved = (self.credentials.get("revision") or "").strip()
        if not saved or saved in PREVIOUS_DEFAULT_REVISIONS:
            return DEFAULT_REVISION
        return saved

    def auth_headers(self) -> dict[str, str]:
        if self._signed_in:
            authorization = f"Bearer {self.access_token}"
        else:
            authorization = f"Klaviyo-API-Key {self.credentials.require('apiKey').strip()}"
        return {
            "Authorization": authorization,
            "revision": self.revision,
            "Accept": JSON_API,
            "Content-Type": JSON_API,
        }

    # ── Sign-in ────────────────────────────────────────────────────────────
    def list_oauth_choices(self) -> list[OAuthChoice]:
        """The one account the token belongs to, so it connects without a pick."""
        return parse_accounts(self.request("GET", "/api/accounts") or {})

    # ── Templates ──────────────────────────────────────────────────────────
    def create_template(self, *, name: str, html: str, text: str) -> str:
        """Create a CODE template and return its id."""
        created = self.request(
            "POST",
            "/api/templates",
            json_body={
                "data": {
                    "type": "template",
                    "attributes": {"name": name[:255], "editor_type": "CODE", "html": html, "text": text},
                }
            },
        )
        template_id = str(((created or {}).get("data") or {}).get("id") or "")
        if not template_id:
            raise ConnectorError("Klaviyo did not return the new template. Try again.")
        return template_id

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
        """The shared email-platform method: on Klaviyo it saves a *template*.

        No campaign is created. Klaviyo templates have no subject, preview
        text or sender — those are set on the campaign the person creates in
        Klaviyo — so only the name, HTML and text are sent. Returns the
        template id and "" (no link is returned by the API).
        """
        return self.create_template(name=name, html=html, text=text), ""

    # ── Lifecycle ──────────────────────────────────────────────────────────
    def check_health(self) -> HealthReport:
        try:
            data = self.request("GET", "/api/accounts")
        except ConnectorError as exc:
            return HealthReport(ok=False, detail=self.diagnose(exc) or str(exc), checked_at=utcnow())
        accounts = (data or {}).get("data") or []
        name = ""
        if accounts:
            info = ((accounts[0].get("attributes") or {}).get("contact_information") or {})
            name = str(info.get("organization_name") or "").strip()
        return HealthReport(ok=True, detail=f"Connected to {name}" if name else "Connected", checked_at=utcnow())

    def verify_steps(self) -> list:
        """Read the account; save one template, read it back, delete it.

        The write step goes through ``create_campaign_draft`` — on Klaviyo the
        email marketer's call saves a CODE template (``POST /api/templates``) —
        then ``GET /api/templates/{id}`` and ``DELETE /api/templates/{id}``.
        A template is never sent to anyone. This connector has no audience
        setting (campaigns are built in Klaviyo), so there is none to read.
        https://developers.klaviyo.com/en/reference/delete_template
        """
        from app.connectors.base import verify_helpers as vh

        def account() -> tuple[str, dict]:
            choices = self.list_oauth_choices()
            if not choices:
                raise ConnectorError("Klaviyo answered, but returned no account for this key.")
            return "Reached the Klaviyo account.", {"account_id": choices[0].id}

        def template() -> tuple[str, dict]:
            name = vh.marker("template")

            def create() -> str:
                template_id, _ = self.create_campaign_draft(
                    name=name,
                    subject=name,
                    preview_text="",
                    html="<html><body><p>Temporary template made by a connection check. Safe to delete.</p></body></html>",
                    text="Temporary template made by a connection check. Safe to delete.",
                    from_name="",
                    reply_to="",
                )
                return template_id

            def check(template_id: str) -> None:
                try:
                    row = self.request("GET", f"/api/templates/{template_id}") or {}
                except ConnectorError as exc:
                    if getattr(exc, "http_status", None) == 403:
                        if self._signed_in:
                            # Signed in before templates:read was requested.
                            raise ConnectorError(
                                "Klaviyo saved the template but this sign-in may not read it "
                                "back. Sign in again to grant the new permission."
                            ) from exc
                        raise ConnectorError(
                            "Klaviyo saved the template but would not let this key read it back; "
                            "give the key read access to Templates."
                        ) from exc
                    raise
                attributes = (row.get("data") or {}).get("attributes") or {}
                if attributes.get("name") != name or attributes.get("editor_type") != "CODE":
                    raise ConnectorError("The template came back with a different name or editor type.")

            def delete(template_id: str) -> None:
                self.request("DELETE", f"/api/templates/{template_id}", expect_json=False)

            evidence = vh.create_check_delete(what="template", create=create, check=check, delete=delete)
            return "Saved an email template, read it back, and deleted it. Nothing was sent.", evidence

        return [
            vh.read_step("account", "Reach the Klaviyo account", account),
            vh.write_step(
                "template_roundtrip",
                "Save an email template, read it back, then delete it",
                template,
                requires_test_account=False,
            ),
        ]

    def diagnose(self, error: Exception) -> str | None:
        text_ = str(error).lower()
        if "credentials were rejected" in text_ or "401" in text_:
            return "Klaviyo rejected the key. Use a private API key (it starts with pk_), not the public site ID."
        if "access was refused" in text_ or "403" in text_:
            if self._signed_in:
                return (
                    "Klaviyo refused a permission this sign-in was not given. Sign in "
                    "again to grant the new permission."
                )
            return (
                "Klaviyo accepted the key but it lacks the access needed. Edit the key in "
                "Settings → API keys: Templates full access and Accounts read access."
            )
        if "could not be understood" in text_ or "400" in text_:
            return (
                "Klaviyo did not accept the request. If you changed the API revision, "
                f"set it back to {DEFAULT_REVISION}."
            )
        return None


CONNECTOR_CLASS = KlaviyoConnector
