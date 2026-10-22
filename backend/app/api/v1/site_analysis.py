"""Site analysis inside the portal: run, history, and one report.

* ``GET    /analyses``        history (newest first) plus what a new run would use
* ``POST   /analyses``        start one (the workspace's own AI model writes it)
* ``GET    /analyses/{id}``   progress, report, what was connected, changes since the last run
* ``DELETE /analyses/{id}``   remove one from the history
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query, Response, status
from pydantic import Field

from app.api.deps import CurrentUserDep, DbSession
from app.core.rbac import Module
from app.schemas.common import ApiModel
from app.services.demo_analysis import workspace

router = APIRouter(prefix="/analyses", tags=["site-analysis"])


class StartSiteAnalysis(ApiModel):
    website_url: str = Field(default="", max_length=500)
    business_url: str = Field(default="", max_length=1000)
    #: A connected AI model's connector slug; blank uses the first connected.
    llm_connector: str = Field(default="", max_length=64)


@router.get("")
def list_analyses(current: CurrentUserDep, db: DbSession) -> dict[str, Any]:
    current.require_view(Module.SEO)
    org = current.organization
    return {
        "analyses": workspace.history(db, org.id),
        "default_website": workspace.default_website(org),
        "models": workspace.model_options(db, org),
    }


@router.post("", status_code=status.HTTP_202_ACCEPTED)
def start_analysis(body: StartSiteAnalysis, current: CurrentUserDep, db: DbSession) -> dict[str, Any]:
    current.require_write(Module.SEO)
    row = workspace.start(
        db,
        current.organization,
        user_id=current.user.id,
        user_name=current.user.name or "",
        website=body.website_url,
        business_url=body.business_url,
        llm_connector=body.llm_connector,
    )
    return workspace.detail(db, row)


@router.get("/{analysis_id}")
def read_analysis(analysis_id: str, current: CurrentUserDep, db: DbSession) -> dict[str, Any]:
    current.require_view(Module.SEO)
    return workspace.detail(db, workspace.get(db, current.organization.id, analysis_id))


@router.get("/{analysis_id}/download")
def download_analysis(
    analysis_id: str,
    current: CurrentUserDep,
    db: DbSession,
    format: str = Query(default="pdf", max_length=8),  # noqa: A002
) -> Response:
    """The report as a PDF or HTML file, with the changes since the last run and what was connected."""
    current.require_view(Module.SEO)
    row = workspace.get(db, current.organization.id, analysis_id)
    detail = workspace.detail(db, row)
    return report_file(
        detail["report"], format,
        extra={"comparison": detail["comparison"], "context": row.context, "workspace": current.organization.name},
    )


@router.delete("/{analysis_id}")
def delete_analysis(analysis_id: str, current: CurrentUserDep, db: DbSession) -> dict[str, Any]:
    current.require_write(Module.SEO)
    row = workspace.get(db, current.organization.id, analysis_id)
    if row.status in workspace.ACTIVE:
        from app.core.exceptions import ConflictError

        raise ConflictError("This analysis is still running.")
    db.delete(row)
    db.commit()
    return {"ok": True}


def report_file(report: dict | None, fmt: str, *, extra: dict | None = None) -> Response:
    """The finished report as a download (PDF or HTML)."""
    from app.core.exceptions import ConflictError
    from app.services.demo_analysis import export

    if not report:
        raise ConflictError("This analysis has no report yet — it is still running or it failed.")
    content, media, name = export.render(report, fmt, extra=extra)
    return Response(content=content, media_type=media, headers={"Content-Disposition": f'attachment; filename="{name}"'})
