"""The website's public analysis endpoint offers both downloads (PDF and interactive HTML)."""
from __future__ import annotations

import time
from urllib.parse import parse_qs, urlsplit

import pytest

from app.api.v1.public_demo import download_analysis
from app.core.exceptions import NotFoundError
from app.models.demo import DemoAnalysis, DemoStatus
from app.services.demo_analysis import service
from tests.test_analysis_export import REPORT

ID = "11111111-2222-3333-4444-555555555555"


class FakeDb:
    def __init__(self, row: DemoAnalysis) -> None:
        self.row = row

    def get(self, model, key):  # noqa: ANN001, ANN201
        return self.row if key == self.row.id else None


def _row(status: str = DemoStatus.DONE) -> DemoAnalysis:
    return DemoAnalysis(
        id=ID, website_url="https://glow.example", business_url="", status=status,
        report=REPORT if status == DemoStatus.DONE else None, progress=[], device_hash="d", fingerprint_hash="f",
        ip_hash="i", token_hash="t", error="", model="Claude",
    )


def _link(view: dict, fmt: str) -> dict[str, str]:
    query = parse_qs(urlsplit(view["downloads"][fmt]["url"]).query)
    return {k: v[0] for k, v in query.items()}


def test_finished_report_lists_both_downloads() -> None:
    view = service.public_view(_row())
    assert set(view["downloads"]) == {"pdf", "html"}
    assert view["downloads"]["pdf"]["filename"].endswith(".pdf")
    assert view["downloads"]["html"]["label"].startswith("Interactive")
    assert f"/api/v1/public/analysis/{ID}/download?format=html" in view["downloads"]["html"]["url"]


def test_unfinished_report_has_no_downloads() -> None:
    assert service.public_view(_row(DemoStatus.RUNNING))["downloads"] is None


@pytest.mark.parametrize(("fmt", "start", "media"), [("pdf", b"%PDF", "application/pdf"), ("html", b"<!doctype html>", "text/html")])
def test_signed_link_downloads_each_format(fmt: str, start: bytes, media: str) -> None:
    row = _row()
    q = _link(service.public_view(row), fmt)
    response = download_analysis(ID, FakeDb(row), format=fmt, token="", expires=int(q["expires"]), sig=q["sig"],
                                 x_analysis_token="", x_device_id="")
    assert response.body.startswith(start)
    assert response.media_type.startswith(media)
    assert "attachment;" in response.headers["content-disposition"]


def test_a_pdf_link_cannot_fetch_the_html() -> None:
    row = _row()
    q = _link(service.public_view(row), "pdf")
    with pytest.raises(NotFoundError):
        download_analysis(ID, FakeDb(row), format="html", token="", expires=int(q["expires"]), sig=q["sig"],
                          x_analysis_token="", x_device_id="")


def test_expired_or_tampered_links_are_refused() -> None:
    row = _row()
    past = int(time.time()) - 10
    with pytest.raises(NotFoundError):
        download_analysis(ID, FakeDb(row), format="pdf", token="", expires=past,
                          sig=service.download_signature(ID, "pdf", past), x_analysis_token="", x_device_id="")
    with pytest.raises(NotFoundError):
        download_analysis(ID, FakeDb(row), format="pdf", token="", expires=int(time.time()) + 60, sig="0" * 40,
                          x_analysis_token="", x_device_id="")
