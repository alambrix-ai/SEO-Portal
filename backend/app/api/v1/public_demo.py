"""Public website analysis — the endpoint the marketing website calls.

No login. The visitor gives their website (and optionally a business listing
link such as their Google Maps profile); the analysis runs in the background
for one to two minutes and the website polls for progress and the report.

* ``POST /public/analysis``            start (one per device; header ``X-Device-Id``)
* ``GET  /public/analysis/mine``       this device's analysis, to restore after a reload
* ``GET  /public/analysis/{id}/download?format=pdf|html``  the report as an enterprise PDF
  or an interactive HTML file. Every finished report response carries
  ``downloads.pdf.url`` and ``downloads.html.url``: signed links valid for 7 days
  that work as plain ``<a href>`` links.
* ``GET  /public/analysis/{id}``       progress and, when done, the report
  (``X-Analysis-Token`` header or ``?token=``, or the device's own ``X-Device-Id``)

The website must be listed in ``DEMO_ALLOWED_ORIGINS`` for browsers to call
these from another domain.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Header, Query, Request, Response, status
from pydantic import BaseModel, Field

from app.api.deps import ClientIp, DbSession
from app.core.exceptions import NotFoundError
from app.services.demo_analysis import service

router = APIRouter(prefix="/public/analysis", tags=["public-analysis"])


class StartAnalysis(BaseModel):
    website_url: str = Field(min_length=3, max_length=500, description="The business's website, e.g. https://example.com")
    business_url: str = Field(
        default="",
        max_length=1000,
        description="Optional: the business's Google Maps / Google Business Profile, Facebook, Yelp or similar listing.",
    )


@router.post("", status_code=status.HTTP_202_ACCEPTED)
def start_analysis(
    body: StartAnalysis,
    request: Request,
    db: DbSession,
    ip: ClientIp,
    x_device_id: str = Header(default="", alias="X-Device-Id"),
) -> dict[str, Any]:
    """Start the analysis. Keep the returned ``access_token`` to read (or share) the report."""
    row, token = service.start(
        db,
        website=body.website_url,
        business_url=body.business_url,
        device_id=x_device_id,
        ip=ip,
        user_agent=request.headers.get("user-agent", ""),
    )
    return service.public_view(row, token=token)


@router.get("/mine")
def my_analysis(db: DbSession, x_device_id: str = Header(default="", alias="X-Device-Id")) -> dict[str, Any]:
    row = service.for_device(db, x_device_id) if x_device_id.strip() else None
    if row is None:
        raise NotFoundError("This device has not run an analysis yet")
    return service.public_view(service.get(db, row.id, device_id=x_device_id))


@router.get("/{analysis_id}/download")
def download_analysis(
    analysis_id: str,
    db: DbSession,
    format: str = Query(default="pdf", max_length=8),  # noqa: A002
    token: str = Query(default=""),
    expires: int = Query(default=0),
    sig: str = Query(default="", max_length=64),
    x_analysis_token: str = Header(default="", alias="X-Analysis-Token"),
    x_device_id: str = Header(default="", alias="X-Device-Id"),
) -> Response:
    """The finished report as an enterprise PDF (``format=pdf``, default) or an
    interactive single-file HTML report (``format=html``).

    Authorised by any one of: the signed ``expires``/``sig`` pair from the
    report's ``downloads`` links (plain ``<a href>`` links work), ``?token=``
    or ``X-Analysis-Token``, or the device's own ``X-Device-Id``.
    """
    from app.api.v1.site_analysis import report_file
    from app.models.demo import DemoAnalysis

    if len(analysis_id) != 36:
        raise NotFoundError("Analysis not found")
    if sig and expires:
        if not service.verify_download(analysis_id, format, expires, sig):
            raise NotFoundError("This download link has expired. Open the report again for a fresh one.")
        row = db.get(DemoAnalysis, analysis_id)
        if row is None:
            raise NotFoundError("Analysis not found")
    else:
        row = service.get(db, analysis_id, token=x_analysis_token or token, device_id=x_device_id)
    return report_file(row.report if row.status == "done" else None, format)


@router.get("/{analysis_id}")
def read_analysis(
    analysis_id: str,
    db: DbSession,
    token: str = Query(default=""),
    x_analysis_token: str = Header(default="", alias="X-Analysis-Token"),
    x_device_id: str = Header(default="", alias="X-Device-Id"),
) -> dict[str, Any]:
    if len(analysis_id) != 36:
        raise NotFoundError("Analysis not found")
    row = service.get(db, analysis_id, token=x_analysis_token or token, device_id=x_device_id)
    return service.public_view(row)
