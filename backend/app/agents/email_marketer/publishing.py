"""Publishing and exporting email assets.

One publisher for every ``email`` asset, routed by the asset's channel — the
email platform it was written for. Publishing **creates a draft** on that
platform (in Klaviyo, a template); it never sends, schedules or test-sends
anything. A person sends the draft from the platform.

HTML and text are rendered again from the copy fields at publish and download
time, so edits made in the library are what goes out, always with the
right unsubscribe tag and always through the sanitizer.
"""
from __future__ import annotations

from email import policy
from email.message import EmailMessage

from sqlalchemy.orm import Session

from app.agents.email_marketer import render
from app.core.exceptions import ConflictError, InvalidInputError
from app.models.content import AssetKind, MarketingAsset
from app.models.workspace import Organization
from app.services import assets

PLATFORM_NAMES = {"mailchimp": "Mailchimp", "brevo": "Brevo", "klaviyo": "Klaviyo"}


def _bundle(db: Session, org: Organization):  # noqa: ANN202 - seam for tests
    from app.services.connectors import build_connector_bundle

    return build_connector_bundle(db, org)


def check_publishable(content: dict) -> None:
    """Refuse content a person has edited into something that cannot be sent."""
    subject = " ".join(str(content.get("subject") or "").split())
    if not subject:
        raise InvalidInputError("This email has no subject. Add one, then approve it again.")
    if len(subject) > 150:
        raise InvalidInputError("The subject is too long for an email platform. Shorten it, then approve again.")
    if not content.get("paragraphs"):
        raise InvalidInputError("This email has no body text. Add some, then approve it again.")
    if not render.normalise_url(str(content.get("cta_url") or "")):
        raise InvalidInputError("The button link must be a full web address starting with https://.")


@assets.register_publisher(AssetKind.EMAIL)
def publish_email(db: Session, org: Organization, asset: MarketingAsset, content: dict) -> None:
    """Create a draft campaign (Klaviyo: a template) on the asset's platform. Never sends."""
    channel = asset.channel
    if channel not in render.PLATFORMS:
        raise ConflictError("This email is for download only — use HTML, text or .eml to send it from any platform.")
    check_publishable(content)
    bundle = _bundle(db, org)
    try:
        connector = bundle.instances.get(channel)
        if connector is None:
            raise ConflictError(
                f"{PLATFORM_NAMES.get(channel, channel)} is not connected, so the draft cannot be "
                "created. Reconnect it under Integrations and approve this again, or download the email."
            )
        remote_id, url = connector.create_campaign_draft(
            name=asset.title,
            subject=" ".join(str(content.get("subject") or "").split()),
            preview_text=" ".join(str(content.get("preview_text") or "").split()),
            html=render.render_html(content, channel=channel),
            text=render.render_text(content, channel=channel),
            from_name=str(content.get("from_name") or "").strip(),
            reply_to="",
        )
    finally:
        bundle.close()
    asset.remote_id = str(remote_id)[:255]
    asset.remote_url = (url or "")[:1000]


# ── Downloads ──────────────────────────────────────────────────────────────
@assets.register_exporter(AssetKind.EMAIL, "html", "HTML email (send-ready)")
def export_html(asset: MarketingAsset, content: dict) -> assets.ExportFile:
    data = render.render_html(content, channel="export")
    return assets.ExportFile(f"{assets.slug_for(asset.title or 'email')}.html", "text/html; charset=utf-8", data.encode())


@assets.register_exporter(AssetKind.EMAIL, "txt", "Plain text")
def export_text(asset: MarketingAsset, content: dict) -> assets.ExportFile:
    body = f"Subject: {content.get('subject', '')}\nPreview: {content.get('preview_text', '')}\n\n"
    body += render.render_text(content, channel="export")
    return assets.ExportFile(f"{assets.slug_for(asset.title or 'email')}.txt", "text/plain; charset=utf-8", body.encode())


def eml_bytes(content: dict) -> bytes:
    """An unsent RFC 5322 draft: subject, plain text and HTML parts, no recipients."""
    message = EmailMessage(policy=policy.SMTP)
    message["Subject"] = " ".join(str(content.get("subject") or "").split())
    # Outlook opens a message carrying this header as a draft to edit and send.
    message["X-Unsent"] = "1"
    message.set_content(render.render_text(content, channel="export"))
    message.add_alternative(render.render_html(content, channel="export"), subtype="html")
    return message.as_bytes()


@assets.register_exporter(AssetKind.EMAIL, "eml", "Email draft (.eml)")
def export_eml(asset: MarketingAsset, content: dict) -> assets.ExportFile:
    return assets.ExportFile(f"{assets.slug_for(asset.title or 'email')}.eml", "message/rfc822", eml_bytes(content))
