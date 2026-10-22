"""AI image generation for content assets, and signed public links to them.

Images are made with the workspace's own connected AI integration — OpenAI
(Images API) or Google Gemini (generateContent) — with the **image model the
person picked on that connection** from the vendor's own list. Nothing here
names a model: a connection without an image model makes no pictures, and
says so ("Choose an image model in your OpenAI connection to create images").
The cost lands on the customer's account like every other model call. Images
are stored in the database with the asset that uses them.

Some platforms (Instagram, Pinterest) only accept an image by *public URL*.
:func:`public_url` signs a link that is valid for a limited time and names
the workspace, so the public endpoint can serve exactly that image and nothing
else, without a login. Links are signed with a key derived for that one
purpose (HMAC of ``SECRET_KEY`` and :data:`LINK_KEY_PURPOSE`); links signed
the old way are accepted for a transition (:data:`LEGACY_LINKS_EXPIRE_BY`).

A vendor's drawing reply is read as a stream and refused once it passes
:func:`max_reply_bytes`, so an oversized reply is never held in memory whole.

Vendors answer with the picture itself (base64). Downloading a picture from a
link the vendor sends back is off unless ``IMAGE_FETCH_ALLOWED_HOSTS`` names
the host; even then only https, an exact host match, public addresses, no
redirects and a byte cap are accepted. Every result must look like a PNG, JPEG
or WebP file. Vendor error text is logged (redacted), never shown to people.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import re
import struct
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpcore
import httpx
from sqlalchemy.orm import Session

from app.core import egress
from app.core.config import settings
from app.core.exceptions import ConflictError, ConnectorError, InvalidInputError
from app.core.logging import get_logger, log_failure
from app.db.base import utcnow
from app.llm.http_provider import redact_vendor_text
from app.models.content import GeneratedImage
from app.models.workspace import Organization

log = get_logger(__name__)

#: The connector field holding the chosen image model.
IMAGE_MODEL_FIELD = "imageModel"
#: Providers tried in this order.
IMAGE_PROVIDERS = ("openai", "google_gemini")
#: How long a public image link stays valid.
PUBLIC_LINK_SECONDS = 24 * 3600

#: Picture shape → OpenAI Images size and Gemini aspect ratio.
SHAPES: dict[str, tuple[str, str]] = {
    "square": ("1024x1024", "1:1"),
    "portrait": ("1024x1536", "2:3"),
    "landscape": ("1536x1024", "3:2"),
}
SIZES = {shape: size for shape, (size, _ratio) in SHAPES.items()}

#: Seconds to wait for a vendor to draw one picture.
GENERATE_TIMEOUT_SECONDS = 180.0
#: Seconds to wait for a picture download (only when link downloads are on).
FETCH_TIMEOUT_SECONDS = 60.0
#: Longest picture link accepted from a vendor.
MAX_IMAGE_URL_CHARS = 2048
#: Room in a drawing reply for the JSON around the picture (ids, text parts).
REPLY_OVERHEAD_BYTES = 256 * 1024
#: Tests only: the raw socket layer under the egress guard for picture
#: downloads. DNS, the address policy and the pinning still run in front of it.
_fetch_inner_backend: httpcore.NetworkBackend | None = None

#: An image model id. Gemini puts it in the URL path, so separators,
#: traversal and query or fragment characters are refused, not escaped.
_MODEL_ID_SHAPE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")

_IMAGE_FAILED = "{vendor} could not create the image. Try again in a few minutes."
_DOWNLOAD_FAILED = (
    "{vendor} made the image but it could not be downloaded safely. Try again, "
    "or ask an administrator to check the image settings."
)
_NOT_AN_IMAGE = "{vendor} sent back something that is not a picture. Try again."
_TOO_BIG = "{vendor} sent back a picture that is too large to use. Try again."


class ImageModelMissing(ConflictError):
    """The image connection has no image model chosen. Content goes on without pictures."""


def image_model_of(connector: object) -> str:
    credentials = getattr(connector, "credentials", None)
    getter = getattr(credentials, "get", None)
    return str(getter(IMAGE_MODEL_FIELD) or "").strip() if callable(getter) else ""


def image_setup_problem(ctx: object) -> str:
    """Why an agent's run cannot make pictures, in plain words — or "".

    "" when no image integration is connected at all (the agents already
    treat that as "no pictures") or when the one in use has an image model.
    """
    from app.connectors.base.connector import Capability

    finder = getattr(ctx, "with_capability", None)
    connector = finder(Capability.GENERATE_IMAGE) if callable(finder) else None
    if connector is None or image_model_of(connector):
        return ""
    from app.llm.model_catalog import no_image_model_message

    return no_image_model_message(str(getattr(connector, "name", "") or "AI"))


def available_provider(db: Session, org: Organization) -> str | None:
    """The first connected integration that can make images, or None."""
    from app.services.connectors import build_connector_bundle

    bundle = build_connector_bundle(db, org)
    for slug in IMAGE_PROVIDERS:
        if slug in bundle.instances:
            return slug
    return None


def provider_for(ctx: object) -> str:
    """The image integration an agent's run uses: its choice, else the first connected.

    ``ctx`` is an :class:`~app.agents.base.context.AgentContext`; typed loosely
    so this service does not import the agent framework.
    """
    from app.connectors.base.connector import Capability

    finder = getattr(ctx, "with_capability", None)
    connector = finder(Capability.GENERATE_IMAGE) if callable(finder) else None
    return str(getattr(connector, "slug", "") or "")


@dataclass(frozen=True)
class GeneratedBytes:
    """One picture made by a vendor, not yet stored."""

    data: bytes
    mime: str
    width: int
    height: int
    provider: str
    model: str


def _checked_model(model: str, vendor: str) -> str:
    """``model`` stripped and validated, or a plain :class:`InvalidInputError`."""
    value = (model or "").strip() if isinstance(model, str) else ""
    if not value or ".." in value or not _MODEL_ID_SHAPE.fullmatch(value):
        raise InvalidInputError(
            f"That image model name cannot be used with {vendor}. Choose a "
            "different image model and try again."
        )
    return value


def generate_bytes(
    db: Session,
    org: Organization,
    prompt: str,
    *,
    shape: str = "square",
    provider: str = "",
    model_override: str | None = None,
    strict: bool = False,
) -> GeneratedBytes:
    """Make one image from ``prompt`` and return its bytes. Raises with a plain reason.

    ``provider`` is the integration to use. Outside strict mode, blank or no
    longer connected means the first connected in :data:`IMAGE_PROVIDERS`
    order that has an image model chosen (else the first connected, which then
    says to choose one), and an unknown ``shape`` is drawn square.

    ``strict`` is for callers that let a person choose (Creative Studio): the
    named ``provider`` must be connected and ``shape`` must be one of
    :data:`SHAPES`; nothing is swapped for something else. ``model_override``
    replaces the connection's saved image model for this one call; it is
    validated and used exactly. The caller still checks it against the image
    model allow-list.

    Raises :class:`ImageModelMissing` when no image model is chosen.
    """
    from app.services.connectors import build_connector_bundle

    if strict and shape not in SHAPES:
        raise InvalidInputError("Choose a square, portrait or landscape picture.")
    if strict and provider not in IMAGE_PROVIDERS:
        raise InvalidInputError("Choose OpenAI or Google Gemini to create images.")
    bundle = build_connector_bundle(db, org)
    connected = [s for s in IMAGE_PROVIDERS if s in bundle.instances]
    slug: str | None
    if strict:
        if provider not in connected:
            raise ConflictError(
                "The AI service chosen for images is not connected. Connect it under "
                "Integrations, or choose a different one."
            )
        slug = provider
    elif provider in connected:
        slug = provider
    else:
        slug = next(
            (s for s in connected if image_model_of(bundle.instances[s])),
            connected[0] if connected else None,
        )
    if slug is None:
        raise ConflictError("Connect OpenAI or Google Gemini under Integrations to create images.")
    connector = bundle.instances[slug]
    vendor = "OpenAI" if slug == "openai" else "Google Gemini"
    if model_override is not None:
        model = _checked_model(model_override, vendor)
    else:
        model = image_model_of(connector)
        if not model:
            from app.llm.model_catalog import no_image_model_message

            raise ImageModelMissing(no_image_model_message(connector.name))
        model = _checked_model(model, vendor)
    key = connector.credentials.require("apiKey")
    size, aspect_ratio = SHAPES.get(shape, SHAPES["square"])
    if slug == "openai":
        data, mime, width, height = _openai(key, model, prompt, size)
    elif aspect_ratio == SHAPES["square"][1]:
        # Square is Gemini's own default, so no imageConfig is sent for it;
        # models without aspect-ratio support still draw square pictures.
        data, mime, width, height = _gemini(key, model, prompt)
    else:
        data, mime, width, height = _gemini(key, model, prompt, aspect_ratio)
    return GeneratedBytes(
        data=data, mime=mime, width=width, height=height, provider=slug, model=model
    )


def generate(
    db: Session,
    org: Organization,
    prompt: str,
    *,
    shape: str = "square",
    provider: str = "",
    model_override: str | None = None,
    strict: bool = False,
) -> GeneratedImage:
    """Make one image from ``prompt`` and store it. Raises with a plain reason.

    ``provider`` is the integration to use — an agent passes the one chosen in
    its settings (``ctx.with_capability(Capability.GENERATE_IMAGE)``). See
    :func:`generate_bytes` for ``model_override`` and ``strict``.

    Raises :class:`ImageModelMissing` when that connection has no image model.
    """
    made = generate_bytes(
        db,
        org,
        prompt,
        shape=shape,
        provider=provider,
        model_override=model_override,
        strict=strict,
    )
    image = GeneratedImage(
        tenant_id=org.id,
        created_at=utcnow(),
        prompt=prompt[:4000],
        provider=made.provider,
        mime=made.mime,
        width=made.width,
        height=made.height,
        data=made.data,
    )
    db.add(image)
    db.flush()
    return image


def _model_refused(vendor: str, model: str, response: httpx.Response) -> ConnectorError:
    """A plain message for a refused image request, naming the setting to change.

    No vendor marks which of its models can draw, so this is where an image
    model that cannot is found out — on its first attempt. The vendor's own
    words are logged by the caller (redacted), never put in this message.
    """
    status = response.status_code
    if status in (400, 404):
        message = (
            f"{vendor} could not create an image with {model}. Choose a different "
            f"image model in your {vendor} connection."
        )
    elif status in (401, 403):
        message = (
            f"{vendor} did not allow {model} to create images for this account. Your "
            f"{vendor} account may need verifying, or choose a different image model."
        )
    elif status == 429:
        message = f"{vendor} is busy or this account has reached its limit. Try again in a few minutes."
    else:
        message = _IMAGE_FAILED.format(vendor=vendor)
    return ConnectorError(message)


def _log_refusal(vendor: str, model: str, response: httpx.Response, key: str) -> None:
    """The vendor's refusal, redacted (key, tokens, emails) and cut short — logs only."""
    log.warning(
        "%s image generation refused: status=%s model=%s detail=%s",
        vendor,
        response.status_code,
        model,
        redact_vendor_text(response.text, secrets=(key,)),
    )


class _ReplyTooLarge(Exception):
    """The vendor's reply passed :func:`max_reply_bytes` while it was read."""


def max_reply_bytes() -> int:
    """Most bytes read from a vendor's drawing reply: the base64 of the
    largest accepted picture plus room for the JSON around it."""
    return (settings.image_fetch_max_bytes * 4) // 3 + REPLY_OVERHEAD_BYTES


def _vendor_post(
    url: str, *, headers: dict[str, str], json: dict[str, object], timeout: float
) -> httpx.Response:
    """POST to a vendor and read the reply as a stream, stopping at the cap.

    The reply is never held whole before it is measured: a declared length
    over the cap is refused before reading, and reading stops as soon as the
    total passes it. Uncompressed replies are asked for, so the cap counts
    what is sent. Returns a fully read response (status, type and body).
    """
    cap = max_reply_bytes()
    with httpx.Client(timeout=timeout) as client, client.stream(
        "POST", url, headers={**headers, "Accept-Encoding": "identity"}, json=json
    ) as response:
        declared = response.headers.get("content-length", "")
        if declared.isdigit() and int(declared) > cap:
            raise _ReplyTooLarge()
        chunks: list[bytes] = []
        total = 0
        for chunk in response.iter_bytes():
            total += len(chunk)
            if total > cap:
                raise _ReplyTooLarge()
            chunks.append(chunk)
        content_type = response.headers.get("content-type", "")
        return httpx.Response(
            response.status_code,
            headers={"content-type": content_type} if content_type else None,
            content=b"".join(chunks),
        )


def _post(
    vendor: str, model: str, url: str, *, headers: dict[str, str], body: dict[str, object]
) -> httpx.Response:
    """One vendor call, its reply capped at :func:`max_reply_bytes`. Not
    retried: each attempt is billed to the customer."""
    try:
        return _vendor_post(url, headers=headers, json=body, timeout=GENERATE_TIMEOUT_SECONDS)
    except _ReplyTooLarge:
        log.warning("%s image reply rejected: over %d bytes", vendor, max_reply_bytes())
        raise ConnectorError(_TOO_BIG.format(vendor=vendor)) from None
    except httpx.HTTPError as exc:
        log_failure(log, "image generation request failed", exc, vendor=vendor, model=model)
        raise ConnectorError(_IMAGE_FAILED.format(vendor=vendor)) from None


def _json_body(vendor: str, response: httpx.Response) -> dict[str, object]:
    try:
        body = response.json()
    except ValueError as exc:
        log_failure(log, "image generation reply was not JSON", exc, vendor=vendor)
        raise ConnectorError(_IMAGE_FAILED.format(vendor=vendor)) from None
    return body if isinstance(body, dict) else {}


def _sniff_mime(data: bytes) -> str | None:
    """The picture type from its first bytes, or None when it is not PNG, JPEG or WebP."""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _checked_image(vendor: str, data: bytes) -> tuple[bytes, str, int, int]:
    """``data`` if it is a picture within the byte cap, with its type and size."""
    if len(data) > settings.image_fetch_max_bytes:
        log.warning("%s image rejected: %d bytes over the cap", vendor, len(data))
        raise ConnectorError(_TOO_BIG.format(vendor=vendor))
    mime = _sniff_mime(data)
    if mime is None:
        log.warning("%s image rejected: not PNG, JPEG or WebP (%d bytes)", vendor, len(data))
        raise ConnectorError(_NOT_AN_IMAGE.format(vendor=vendor))
    width, height = _png_size(data)
    return data, mime, width, height


def _decode_base64(vendor: str, encoded: object) -> bytes:
    """Strict base64 decode, refusing anything over the byte cap before decoding."""
    if not isinstance(encoded, str):
        raise ConnectorError(_NOT_AN_IMAGE.format(vendor=vendor))
    if len(encoded) > (settings.image_fetch_max_bytes * 4) // 3 + 4:
        log.warning("%s image rejected: %d base64 characters over the cap", vendor, len(encoded))
        raise ConnectorError(_TOO_BIG.format(vendor=vendor))
    try:
        return base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        log_failure(log, "image base64 did not decode", exc, vendor=vendor)
        raise ConnectorError(_NOT_AN_IMAGE.format(vendor=vendor)) from None


def _openai(key: str, model: str, prompt: str, size: str) -> tuple[bytes, str, int, int]:
    response = _post(
        "OpenAI",
        model,
        f"{settings.openai_base_url.rstrip('/')}/v1/images/generations",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        body={"model": model, "prompt": prompt, "size": size, "n": 1},
    )
    if response.status_code >= 400:
        _log_refusal("OpenAI", model, response, key)
        raise _model_refused("OpenAI", model, response)
    items = _json_body("OpenAI", response).get("data")
    item = items[0] if isinstance(items, list) and items and isinstance(items[0], dict) else {}
    encoded = item.get("b64_json")
    if encoded:
        return _checked_image("OpenAI", _decode_base64("OpenAI", encoded))
    if item.get("url"):
        # Some image models answer with a short-lived link instead of the bytes.
        return _checked_image("OpenAI", _fetch_image("OpenAI", item["url"]))
    raise ConnectorError("OpenAI returned no image data. Try again.")


def _gemini(key: str, model: str, prompt: str, aspect_ratio: str = "") -> tuple[bytes, str, int, int]:
    body: dict[str, object] = {"contents": [{"parts": [{"text": prompt}]}]}
    if aspect_ratio:
        body["generationConfig"] = {"imageConfig": {"aspectRatio": aspect_ratio}}
    # The key goes in Google's own header, never in the URL, so it cannot end
    # up in proxy, access or exception logs.
    response = _post(
        "Google Gemini",
        model,
        f"{settings.gemini_base_url.rstrip('/')}/v1beta/models/{model}:generateContent",
        headers={"x-goog-api-key": key, "Content-Type": "application/json"},
        body=body,
    )
    if response.status_code >= 400:
        _log_refusal("Google Gemini", model, response, key)
        raise _model_refused("Google Gemini", model, response)
    candidates = _json_body("Google Gemini", response).get("candidates")
    for candidate in candidates if isinstance(candidates, list) else []:
        content = candidate.get("content") if isinstance(candidate, dict) else None
        parts = content.get("parts") if isinstance(content, dict) else None
        for part in parts if isinstance(parts, list) else []:
            inline = (part.get("inlineData") or part.get("inline_data")) if isinstance(part, dict) else None
            if isinstance(inline, dict) and inline.get("data"):
                # The type comes from the bytes, not from the vendor's label.
                return _checked_image("Google Gemini", _decode_base64("Google Gemini", inline["data"]))
    raise ConnectorError(
        f"Google Gemini answered without a picture, so {model} may not make images. "
        "Choose a different image model in your Google Gemini connection."
    )


def _allowed_image_url(vendor: str, url: object) -> str:
    """``url`` when picture downloads are on and it passes every check; else a plain error.

    https only, no credentials, the default port, a host named exactly in
    ``IMAGE_FETCH_ALLOWED_HOSTS``, and every address it resolves to public
    (``egress.validate_url`` unwraps IPv4-mapped IPv6 addresses). The download
    itself re-checks the address when it connects, so a second DNS answer
    cannot point it somewhere else.
    """
    allowed = set(settings.image_fetch_allowed_hosts)
    if not allowed:
        log.warning("%s sent an image link; link downloads are off (IMAGE_FETCH_ALLOWED_HOSTS)", vendor)
        raise ConnectorError(_DOWNLOAD_FAILED.format(vendor=vendor))
    if not isinstance(url, str) or len(url) > MAX_IMAGE_URL_CHARS:
        log.warning("%s image link rejected: missing or too long", vendor)
        raise ConnectorError(_DOWNLOAD_FAILED.format(vendor=vendor))
    target = url.strip()
    try:
        parts = urlsplit(target)
        port = parts.port
    except ValueError:
        log.warning("%s image link rejected: unparseable", vendor)
        raise ConnectorError(_DOWNLOAD_FAILED.format(vendor=vendor)) from None
    host = (parts.hostname or "").lower().rstrip(".")
    if parts.scheme.lower() != "https" or parts.username or parts.password or port not in (None, 443):
        log.warning("%s image link rejected: not plain https", vendor)
        raise ConnectorError(_DOWNLOAD_FAILED.format(vendor=vendor))
    if host not in allowed:
        log.warning("%s image link rejected: host is not in IMAGE_FETCH_ALLOWED_HOSTS", vendor)
        raise ConnectorError(_DOWNLOAD_FAILED.format(vendor=vendor))
    try:
        # The host list decides WHICH hosts; the address must still be public,
        # so the egress allow-list stays empty (an entry there would open
        # private addresses for that host).
        egress.validate_url(target, allow=egress.NO_ALLOWLIST, require_https=True, allowed_ports={443})
    except egress.EgressBlockedError as exc:
        log_failure(log, "image link host is not public", exc, vendor=vendor, host=host)
        raise ConnectorError(_DOWNLOAD_FAILED.format(vendor=vendor)) from None
    return target


def _fetch_image(vendor: str, url: object) -> bytes:
    """Download a vendor's picture link: allowed host, no redirects, capped bytes.

    Off unless ``IMAGE_FETCH_ALLOWED_HOSTS`` is set. The connection goes
    through the egress guard (pinned to a checked public address; proxy
    settings from the environment ignored), a redirect is refused, and the
    download stops as soon as it passes ``IMAGE_FETCH_MAX_BYTES``.
    """
    target = _allowed_image_url(vendor, url)
    cap = settings.image_fetch_max_bytes
    try:
        with egress.pinned_client(
            timeout=httpx.Timeout(FETCH_TIMEOUT_SECONDS, connect=10.0),
            allow=egress.NO_ALLOWLIST,
            headers={"Accept": "image/png, image/jpeg, image/webp", "Accept-Encoding": "identity"},
            inner_backend=_fetch_inner_backend,
        ) as client:
            response = egress.fetch(
                client,
                "GET",
                target,
                allow=egress.NO_ALLOWLIST,
                require_https=True,
                allowed_ports={443},
                max_bytes=cap,
                max_redirects=0,
            )
    except egress.EgressBlockedError as exc:
        log.warning("%s image download refused: reason=%s", vendor, exc.reason)
        if exc.reason == "too_large":
            raise ConnectorError(_TOO_BIG.format(vendor=vendor)) from None
        raise ConnectorError(_DOWNLOAD_FAILED.format(vendor=vendor)) from None
    except httpx.HTTPError as exc:
        log_failure(log, "image download failed", exc, vendor=vendor)
        raise ConnectorError(_DOWNLOAD_FAILED.format(vendor=vendor)) from None
    if response.status_code != 200:
        log.warning("%s image download refused: status=%s", vendor, response.status_code)
        raise ConnectorError(_DOWNLOAD_FAILED.format(vendor=vendor))
    return response.content


def _png_size(data: bytes) -> tuple[int, int]:
    if data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) >= 24:
        width, height = struct.unpack(">II", data[16:24])
        return int(width), int(height)
    return 0, 0


# ── Signed public links ────────────────────────────────────────────────────
#: Names what the derived signing key is for, so a signature made here can
#: never stand in for an HMAC the server makes with ``SECRET_KEY`` elsewhere.
LINK_KEY_PURPOSE = b"public-image-link-v1"
#: Links signed the old way (straight with ``SECRET_KEY``) are still accepted,
#: but only for expiries up to here: this release's date (2026-10-22) plus the
#: longest link it could have issued (``media_links.MAX_PUBLISHED_LINK_DAYS``,
#: five years). Remove ``_legacy_signature`` and its use in :func:`verify` once
#: ``PUBLISHED_LINK_DAYS`` (365 by default) have passed since this release was
#: deployed: by then every link signed the old way has expired.
LEGACY_LINKS_EXPIRE_BY = 1_950_307_200  # 2031-10-21 00:00 UTC


def _link_key() -> bytes:
    """HMAC(SECRET_KEY, purpose): the key used only for public image links."""
    return hmac.new(settings.secret_key.encode(), LINK_KEY_PURPOSE, hashlib.sha256).digest()


def _message(tenant_id: str, image_id: str, expires: int) -> bytes:
    return f"{tenant_id}:{image_id}:{expires}".encode()


def _signature(tenant_id: str, image_id: str, expires: int) -> str:
    return hmac.new(_link_key(), _message(tenant_id, image_id, expires), hashlib.sha256).hexdigest()[:40]


def _legacy_signature(tenant_id: str, image_id: str, expires: int) -> str:
    """The signature links had before the purpose key; verified, never issued."""
    message = _message(tenant_id, image_id, expires)
    return hmac.new(settings.secret_key.encode(), message, hashlib.sha256).hexdigest()[:40]


def public_url(tenant_id: str, image_id: str, *, seconds: int = PUBLIC_LINK_SECONDS) -> str:
    """An unauthenticated, expiring link to one image (for platforms that fetch by URL)."""
    expires = int(time.time()) + seconds
    sig = _signature(tenant_id, image_id, expires)
    base = (settings.public_api_base_url or settings.public_base_url).rstrip("/")
    return f"{base}/api/v1/public/images/{tenant_id}/{image_id}/{expires}/{sig}"


#: A signature exactly as :func:`_signature` writes it: 40 lower-case hex digits.
SIGNATURE_SHAPE = re.compile(r"^[0-9a-f]{40}$")


def verify(tenant_id: str, image_id: str, expires: int, sig: str) -> bool:
    """True only for an unexpired signature for this exact image.

    Anything not shaped like a signature is refused before the comparison:
    ``hmac.compare_digest`` raises ``TypeError`` on non-ASCII text, which
    would otherwise surface as a server error instead of "not found".
    """
    if not isinstance(sig, str) or not SIGNATURE_SHAPE.fullmatch(sig):
        return False
    if isinstance(expires, bool) or not isinstance(expires, int) or expires < int(time.time()):
        return False
    if hmac.compare_digest(_signature(tenant_id, image_id, expires), sig):
        return True
    # Transition only (see LEGACY_LINKS_EXPIRE_BY): links issued before the
    # purpose key, already in published posts.
    return expires <= LEGACY_LINKS_EXPIRE_BY and hmac.compare_digest(
        _legacy_signature(tenant_id, image_id, expires), sig
    )


def is_publicly_reachable() -> bool:
    """Whether a platform outside this network can fetch the public links."""
    base = (settings.public_api_base_url or settings.public_base_url).lower()
    return base.startswith("https://") and "localhost" not in base and "127.0.0.1" not in base
