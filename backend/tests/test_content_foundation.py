"""Foundation for the content agents: SSRF guard, brand profile, library, images."""
from __future__ import annotations

import socket
from datetime import timedelta
from types import SimpleNamespace

import pytest

from app.core import netguard
from app.core.exceptions import InvalidInputError
from app.db.base import utcnow
from app.models.content import AssetStatus, MarketingAsset
from app.services import assets, brand_profile, images


def _resolve_to(monkeypatch, address: str) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(None, None, None, "", (address, 443))])


@pytest.mark.parametrize("address", ["127.0.0.1", "10.0.0.5", "192.168.1.10", "169.254.169.254", "::1", "0.0.0.0"])
def test_internal_addresses_are_refused(monkeypatch, address):
    _resolve_to(monkeypatch, address)
    with pytest.raises(InvalidInputError, match="not a public website"):
        netguard.check_public_url("https://looks-public.example")


def test_public_addresses_and_schemes(monkeypatch):
    _resolve_to(monkeypatch, "93.184.216.34")
    assert netguard.check_public_url("https://example.com/x") == "https://example.com/x"
    with pytest.raises(InvalidInputError):
        netguard.check_public_url("file:///etc/passwd")
    with pytest.raises(InvalidInputError):
        netguard.check_public_url("ftp://example.com")


def test_profile_is_cleaned_and_summarised_for_prompts():
    profile = brand_profile._clean(
        {
            "business_name": "  Geet   unisex salon ",
            "summary": "Haircuts and styling in Noida.",
            "keywords": ["salon near me", "Salon near me", "", "unisex salon"],
            "audiences": "Working professionals",
            "unknown": "dropped",
        }
    )
    assert profile["business_name"] == "Geet unisex salon"
    assert profile["keywords"] == ["salon near me", "unisex salon"]
    assert profile["audiences"] == ["Working professionals"]
    assert "unknown" not in profile
    org = SimpleNamespace(name="Geet", settings={"brand_profile": profile})
    text = brand_profile.brief(org)
    assert "BUSINESS: Geet unisex salon" in text and "SEARCH KEYWORDS: salon near me; unisex salon" in text
    assert brand_profile.brief(SimpleNamespace(name="x", settings={})) == ""


def test_generate_reads_the_site_and_stores_the_profile(monkeypatch):
    pages = {
        "https://geet.example/": '<html><title>Geet</title><body><h1>Geet unisex salon</h1>'
        '<p>Haircuts, colour and spa in Noida.</p><a href="/about">About</a><a href="/services">Services</a></body></html>',
        "https://geet.example/about": "<html><body><p>Family run since 2015.</p></body></html>",
        "https://geet.example/services": "<html><body><p>Haircut, hair colour, facials.</p></body></html>",
    }

    def fake_get(url, **_):  # noqa: ANN001
        import httpx

        return httpx.Response(200, text=pages[url if url in pages else url + "/"], headers={"content-type": "text/html"},
                              request=httpx.Request("GET", url))

    monkeypatch.setattr(brand_profile, "safe_get", fake_get)
    llm = SimpleNamespace(complete_json=lambda prompt, **k: ({"business_name": "Geet", "industry": "hair salon",
                                                               "keywords": ["salon near me"]}, None))
    monkeypatch.setattr(brand_profile, "workspace_llm", lambda db, org: llm)
    org = SimpleNamespace(name="Geet", settings={}, primary_domain="")
    profile = brand_profile.generate(SimpleNamespace(flush=lambda: None), org, "geet.example")
    assert profile["industry"] == "hair salon"
    assert org.settings["industry"] == "hair salon"  # agents' existing field kept in step
    assert len(profile["pages_read"]) == 3
    assert org.primary_domain == "geet.example"


class _Cipher:
    def encrypt(self, value, *, context):  # noqa: ANN001
        return value

    def decrypt(self, value, *, context, default=""):  # noqa: ANN001
        return value


def _asset(**kw) -> MarketingAsset:
    values = dict(id="a1", tenant_id="t", agent_slug="x", kind="test_kind", channel="c", title="Hello World",
                  content_encrypted='{"body": "Hi"}', status=AssetStatus.DRAFT, detail={}, image_ids=[])
    values.update(kw)
    return MarketingAsset(**values)


def test_release_publishes_schedules_or_marks_ready_to_download():
    db = SimpleNamespace(flush=lambda: None)
    org = SimpleNamespace(id="t")
    # No publisher for this kind: approval means "ready to download".
    asset = _asset()
    assets.release(db, org, asset, _Cipher())
    assert asset.status == AssetStatus.APPROVED

    published: list = []
    assets.PUBLISHERS["test_kind"] = lambda db, org, asset, content: published.append(content)
    try:
        later = _asset(scheduled_for=utcnow() + timedelta(days=1))
        assets.release(db, org, later, _Cipher())
        assert later.status == AssetStatus.SCHEDULED and published == []
        now = _asset()
        assets.release(db, org, now, _Cipher())
        assert now.status == AssetStatus.PUBLISHED and published == [{"body": "Hi"}]
    finally:
        assets.PUBLISHERS.pop("test_kind")


def test_a_failed_publish_is_recorded_on_the_asset():
    from app.core.exceptions import ConflictError

    def boom(*_):  # noqa: ANN002
        raise RuntimeError("platform said no")

    assets.PUBLISHERS["test_kind"] = boom
    try:
        asset = _asset()
        with pytest.raises(ConflictError):
            assets.publish(SimpleNamespace(flush=lambda: None), SimpleNamespace(id="t"), asset, _Cipher())
        assert asset.status == AssetStatus.FAILED and asset.error
    finally:
        assets.PUBLISHERS.pop("test_kind")


def test_generic_exports_and_csv():
    file = assets.export(_asset(), _Cipher(), "json")
    assert file.filename == "hello-world.json" and b'"body": "Hi"' in file.data
    assert "Body: Hi" in assets.export(_asset(), _Cipher(), "txt").data.decode()
    with pytest.raises(InvalidInputError):
        assets.export(_asset(), _Cipher(), "pdf")
    tsv = assets.csv_bytes(["A", "B"], [["1", None]], utf16=True)
    assert tsv.startswith(b"\xff\xfe") and "A\tB" in tsv.decode("utf-16-le")


def test_signed_image_links_expire_and_cannot_be_reused_for_another_image():
    url = images.public_url("tenant", "img1", seconds=60)
    tenant, image_id, expires, sig = url.rsplit("/", 4)[1:]
    assert images.verify(tenant, image_id, int(expires), sig)
    assert not images.verify(tenant, "img2", int(expires), sig)
    assert not images.verify("other", image_id, int(expires), sig)
    assert not images.verify(tenant, image_id, int(expires) - 3600, sig)
