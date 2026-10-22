"""Long-lived public links to a published post's picture (app.services.media_links)."""
from __future__ import annotations

import time
from urllib.parse import urlsplit

import pytest

from app.api.v1 import content as content_api
from app.core.config import settings
from app.core.exceptions import NotFoundError
from app.models.content import GeneratedImage
from app.services import images, media_links
from tests.conftest import requires_db


def _parts(url: str) -> tuple[str, str, int, str]:
    """(tenant, image, expires, signature) from a public image link."""
    path = urlsplit(url).path
    prefix = "/api/v1/public/images/"
    assert path.startswith(prefix)
    tenant, image, expires, sig = path[len(prefix):].split("/")
    return tenant, image, int(expires), sig


def _image(tenant: str = "tenant-a", image_id: str = "img-1") -> GeneratedImage:
    return GeneratedImage(id=image_id, tenant_id=tenant, mime="image/png", data=b"PNG")


def test_a_published_link_lasts_a_year_and_opens_only_that_picture():
    before = int(time.time())
    url = media_links.published_image_url(_image())
    tenant, image, expires, sig = _parts(url)
    assert (tenant, image) == ("tenant-a", "img-1")
    year = media_links.DEFAULT_PUBLISHED_LINK_DAYS * 24 * 3600
    assert before + year <= expires <= int(time.time()) + year
    assert media_links.verify(tenant, image, expires, sig)
    # The same route and check as the 24-hour links.
    assert images.verify(tenant, image, expires, sig)
    # Nothing else: another picture, another workspace, a later expiry.
    assert not media_links.verify(tenant, "img-2", expires, sig)
    assert not media_links.verify("tenant-b", image, expires, sig)
    assert not media_links.verify(tenant, image, expires + 1, sig)


def test_the_published_link_outlives_the_ordinary_one():
    ordinary = _parts(images.public_url("tenant-a", "img-1"))[2]
    published = _parts(media_links.published_image_url(_image()))[2]
    assert published - ordinary >= (media_links.DEFAULT_PUBLISHED_LINK_DAYS - 1) * 24 * 3600


def test_requested_lifetimes_are_bounded():
    longest = _parts(media_links.published_image_url(_image(), days=100_000))[2]
    assert longest <= int(time.time()) + media_links.MAX_PUBLISHED_LINK_DAYS * 24 * 3600
    shortest = _parts(media_links.published_image_url(_image(), days=-5))[2]
    assert shortest >= int(time.time()) + media_links.MIN_PUBLISHED_LINK_DAYS * 24 * 3600 - 5
    with pytest.raises(ValueError):
        media_links.published_image_url(_image(), days=True)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        media_links.published_image_url(GeneratedImage(tenant_id="", data=b""))


def test_tampered_expired_or_malformed_signatures_are_refused():
    tenant, image, expires, sig = _parts(media_links.published_image_url(_image()))
    flipped = ("0" if sig[0] != "0" else "1") + sig[1:]
    assert not media_links.verify(tenant, image, expires, flipped)
    assert not media_links.verify(tenant, image, expires, sig.upper())
    assert not media_links.verify(tenant, image, expires, "é" * 40)  # never raises
    assert not media_links.verify(tenant, image, expires, sig[:-1])
    assert not media_links.verify(tenant, image, expires, "")
    assert not media_links.verify(tenant, image, True, sig)  # type: ignore[arg-type]
    stale = _parts(images.public_url(tenant, image, seconds=-10))
    assert not media_links.verify(*stale)


def test_a_new_secret_key_revokes_every_link(monkeypatch):  # noqa: ANN001
    tenant, image, expires, sig = _parts(media_links.published_image_url(_image()))
    monkeypatch.setattr(settings, "secret_key", settings.secret_key + "-rotated")
    assert not media_links.verify(tenant, image, expires, sig)


def test_the_public_route_refuses_a_bad_signature_without_reading_anything():
    tenant, image, expires, sig = _parts(media_links.published_image_url(_image()))
    with pytest.raises(NotFoundError):
        content_api.public_image(tenant, "img-2", expires, sig)


def test_availability_follows_the_public_address(monkeypatch):  # noqa: ANN001
    monkeypatch.setattr(images, "is_publicly_reachable", lambda: False)
    assert media_links.available() is False
    monkeypatch.setattr(images, "is_publicly_reachable", lambda: True)
    assert media_links.available() is True


@requires_db
def test_deleting_the_picture_revokes_its_published_link(clean_db, org):  # noqa: ANN001
    from app.db.base import utcnow
    from app.db.session import bind_tenant

    bind_tenant(clean_db, org.id)
    stored = GeneratedImage(tenant_id=org.id, created_at=utcnow(), mime="image/png", data=b"\x89PNG\r\n\x1a\nX")
    clean_db.add(stored)
    clean_db.commit()
    tenant, image, expires, sig = _parts(media_links.published_image_url(stored))

    served = content_api.public_image(tenant, image, expires, sig)
    assert served.status_code == 200 and served.body == b"\x89PNG\r\n\x1a\nX"

    bind_tenant(clean_db, org.id)
    clean_db.delete(clean_db.get(GeneratedImage, image))
    clean_db.commit()
    with pytest.raises(NotFoundError):
        content_api.public_image(tenant, image, expires, sig)


@requires_db
def test_a_link_signed_for_one_workspace_never_serves_another(clean_db, org, second_org):  # noqa: ANN001
    from app.db.base import utcnow
    from app.db.session import bind_tenant

    bind_tenant(clean_db, second_org.id)
    theirs = GeneratedImage(tenant_id=second_org.id, created_at=utcnow(), mime="image/png", data=b"\x89PNG\r\n\x1a\nY")
    clean_db.add(theirs)
    clean_db.commit()
    # Signed for org's workspace but naming the other workspace's picture.
    forged = images.public_url(org.id, theirs.id, seconds=3600)
    tenant, image, expires, sig = _parts(forged)
    with pytest.raises(NotFoundError):
        content_api.public_image(tenant, image, expires, sig)


# ── Signing key domain separation ──────────────────────────────────────────
def _legacy(tenant: str, image: str, expires: int) -> str:
    import hashlib
    import hmac

    message = f"{tenant}:{image}:{expires}".encode()
    return hmac.new(settings.secret_key.encode(), message, hashlib.sha256).hexdigest()[:40]


def test_links_are_signed_with_the_purpose_key_not_the_raw_secret():
    import hashlib
    import hmac

    tenant, image, expires, sig = _parts(media_links.published_image_url(_image()))
    purpose_key = hmac.new(settings.secret_key.encode(), b"public-image-link-v1", hashlib.sha256).digest()
    expected = hmac.new(purpose_key, f"{tenant}:{image}:{expires}".encode(), hashlib.sha256).hexdigest()[:40]
    assert sig == expected
    assert sig != _legacy(tenant, image, expires)


def test_old_links_still_open_during_the_transition():
    tenant, image, expires, _sig = _parts(media_links.published_image_url(_image()))
    old = _legacy(tenant, image, expires)
    assert media_links.verify(tenant, image, expires, old)
    assert images.verify(tenant, image, expires, old)
    # Still only for that exact picture, workspace and expiry.
    assert not media_links.verify(tenant, "img-2", expires, old)
    assert not media_links.verify("tenant-b", image, expires, old)


def test_old_style_signatures_are_refused_past_the_transition_cutoff():
    late = images.LEGACY_LINKS_EXPIRE_BY + 1
    assert not images.verify("tenant-a", "img-1", late, _legacy("tenant-a", "img-1", late))
    # A new-style link with that expiry is fine: the cutoff is only for old ones.
    assert images.verify("tenant-a", "img-1", late, images._signature("tenant-a", "img-1", late))


def test_an_expired_old_style_link_is_refused():
    past = int(time.time()) - 10
    assert not images.verify("tenant-a", "img-1", past, _legacy("tenant-a", "img-1", past))
