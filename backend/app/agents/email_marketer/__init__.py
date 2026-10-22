"""Email Marketer agent package."""
from app.agents.base.plain import AgentGuide, Optional_, SettingField
from app.agents.email_marketer import publishing  # noqa: F401 - registers the publisher and exporters
from app.agents.email_marketer.agent import AGENT, EmailMarketerAgent
from app.connectors.base.connector import Capability
from app.services.agent_results import asset_results

GUIDE = AgentGuide(
    summary=(
        "Writes marketing emails from your brand profile: a four-email welcome series, a "
        "newsletter about your recently published articles, and win-back and promotional emails."
    ),
    effect=(
        "Never sends email to anyone. Approving an email creates a draft campaign in Mailchimp or "
        "Brevo for you to review and send there; in Klaviyo it saves the email as a template and "
        "creates no campaign. If more than one is connected, the first of Mailchimp, Brevo and "
        "Klaviyo is used. With no email platform connected, approved emails are ready to download "
        "as HTML, plain text or an .eml draft. The welcome series (for days 0, 2, 5 and 9 after "
        "sign-up) is written once, and again only when your brand profile changes; each email is "
        "a separate draft, so set up the timing in your platform's automation. A newsletter is "
        "written once per week or month about the articles published or approved in the library "
        "in the past 7 or 31 days, and is skipped when there are none. Win-back and promotional "
        "emails are written once per calendar month while switched on. Each email's button links "
        "to a page of your own website or one of your articles. An email that breaks the rules — "
        "such as an offer, price or percentage your brand profile does not contain, or a spammy "
        "subject — is rewritten once and then thrown away."
    ),
    optional=(
        Optional_(
            Capability.CREATE_EMAIL_CAMPAIGN,
            "Creates the drafts in your email platform (Mailchimp, Brevo; Klaviyo as templates). "
            "Without it, emails are for download only.",
        ),
        Optional_(
            Capability.GENERATE_IMAGE,
            "Adds an AI-made header image. Email apps load images from this platform's public "
            "server address, so images are only added when that address is a public https "
            "address; otherwise emails have no image. Image links stay valid for about 13 months.",
        ),
    ),
    uses_scope=False,
    settings=(
        SettingField(
            "programs",
            "Emails to write",
            "Any of welcome, newsletter, winback, promo, separated by commas. Leave blank for "
            "welcome, newsletter.",
            default="welcome,newsletter",
            max_length=60,
        ),
        SettingField(
            "newsletter_frequency",
            "Newsletter frequency",
            "weekly or monthly. Leave blank for weekly.",
            default="weekly",
            max_length=10,
        ),
        SettingField(
            "from_name",
            "From name (optional)",
            "The sender name on the drafts. Leave blank to use the name set on your email platform.",
            max_length=100,
        ),
    ),
)

RESULTS = lambda db, tenant_id, cipher: asset_results(  # noqa: E731
    db,
    tenant_id,
    agent_slug="email_marketer",
    title="Emails",
    description=(
        "Emails this agent wrote. Approving one creates a draft in your email platform — "
        "nothing is sent until you send it there."
    ),
    empty="No emails written yet.",
    channel_label="Platform",
)

__all__ = ["AGENT", "GUIDE", "RESULTS", "EmailMarketerAgent"]
