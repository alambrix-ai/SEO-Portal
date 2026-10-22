"""Long-lived public links to the picture in a published social post.

Most platforms take a post's picture as bytes, or fetch it once while the
post is being made (Instagram), so the 24-hour link from
:func:`app.services.images.public_url` is enough. A Reddit link post is
different: the post *is* a link to the picture, and readers open it for as
long as the post is up. Pinterest is given a link only when it cannot take
the picture inline (WebP). Those posts get a link from here instead, valid
for ``PUBLISHED_LINK_DAYS`` (365 by default).

Each link:

* names exactly one picture of one workspace, and is HMAC-SHA256 signed over
  the workspace, the picture and the expiry, so it cannot be changed to reach
  anything else. The key is the one :mod:`app.services.images` derives from
  the server's secret key for public image links only
  (``images.LINK_KEY_PURPOSE``), so these signatures cannot be confused with
  any other HMAC the server makes; links signed before that are accepted
  until ``images.LEGACY_LINKS_EXPIRE_BY`` (see there for when to remove it);
* is checked in constant time (``hmac.compare_digest``) by the public route
  ``GET /api/v1/public/images/{tenant}/{image}/{expires}/{sig}``, the same
  route and signature as the 24-hour links, so no new unauthenticated
  endpoint is opened;
* stops working when the picture is deleted (the route loads it through a
  tenant-scoped query and answers 404), when it expires, or when the server's
  secret key is rotated (which revokes every link at once).

Nothing here logs the link: it is a bearer credential for that picture.
"""
from __future__ import annotations

import re

from app.core.config import settings
from app.models.content import GeneratedImage
from app.services import images

#: How long a published post's picture link stays valid when the server
#: setting ``PUBLISHED_LINK_DAYS`` (``settings.published_link_days``) is not
#: changed. Kept as the documented default; the setting is what is used.
DEFAULT_PUBLISHED_LINK_DAYS = 365
#: Bounds a caller may ask for: at least the 24-hour link's day, at most
#: five years.
MIN_PUBLISHED_LINK_DAYS = 1
MAX_PUBLISHED_LINK_DAYS = 5 * 365

#: A signature as :func:`app.services.images.public_url` writes it.
_SIGNATURE = re.compile(r"[0-9a-f]{40}")


def available() -> bool:
    """Whether a platform outside this network can open the links at all."""
    return images.is_publicly_reachable()


def published_image_url(image: GeneratedImage, *, days: int | None = None) -> str:
    """A signed public link to ``image`` that stays valid for ``days`` days
    (``settings.published_link_days`` when not given).

    ``image`` must already have been loaded through a tenant-filtered query:
    the link is signed for ``image.tenant_id`` and ``image.id`` exactly.
    """
    tenant_id = str(getattr(image, "tenant_id", "") or "")
    image_id = str(getattr(image, "id", "") or "")
    if not tenant_id or not image_id:
        raise ValueError("A published link needs a stored picture")
    if days is None:
        days = settings.published_link_days
    if isinstance(days, bool) or not isinstance(days, int):
        raise ValueError("days must be a whole number")
    bounded = min(max(days, MIN_PUBLISHED_LINK_DAYS), MAX_PUBLISHED_LINK_DAYS)
    return images.public_url(tenant_id, image_id, seconds=bounded * 24 * 3600)


def verify(tenant_id: str, image_id: str, expires: int, sig: str) -> bool:
    """True when ``sig`` is a valid, unexpired signature for that one picture.

    Delegates to :func:`app.services.images.verify` (constant-time compare),
    so the public route accepts both kinds of link with one check. Anything
    not shaped like a signature is refused first: ``compare_digest`` raises
    on non-ASCII text instead of answering False.
    """
    if not isinstance(sig, str) or not _SIGNATURE.fullmatch(sig):
        return False
    if isinstance(expires, bool) or not isinstance(expires, int):
        return False
    return images.verify(str(tenant_id), str(image_id), expires, sig)


__all__ = [
    "DEFAULT_PUBLISHED_LINK_DAYS",
    "MAX_PUBLISHED_LINK_DAYS",
    "MIN_PUBLISHED_LINK_DAYS",
    "available",
    "published_image_url",
    "verify",
]
