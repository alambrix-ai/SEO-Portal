"""Warehouse export admin API (egress-allowlisted destinations)."""
from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.api.deps import CurrentUserDep, DbSession
from app.core.exceptions import InvalidInputError, NotFoundError
from app.core.rbac import Module
from app.services import mfa as mfa_service
from app.services import warehouse as warehouse_service

router = APIRouter(prefix="/warehouse", tags=["warehouse"])


class ExportIn(BaseModel):
    provider: str = "fake"
    dataset: str = "metrics"
    destination: str = Field(min_length=3, max_length=512)
    secret: str = ""


@router.post("/exports")
def enqueue_export(body: ExportIn, current: CurrentUserDep, db: DbSession) -> dict:
    current.require_write(Module.ADMIN)
    mfa_service.require_step_up(current.claims, current.organization, current.user)
    try:
        job = warehouse_service.enqueue_export(
            db,
            current.organization,
            current.cipher,
            provider=body.provider,
            dataset=body.dataset,
            destination=body.destination,
            actor=current.user,
            secret=body.secret,
        )
        job = warehouse_service.run_export(
            db, current.organization, current.cipher, job.id
        )
    except InvalidInputError:
        raise
    except LookupError as exc:
        raise NotFoundError("Not found.") from exc
    db.commit()
    return {
        "id": job.id,
        "provider": job.provider,
        "dataset": job.dataset,
        "status": job.status,
        "row_count": job.row_count,
        "error_code": job.error_code,
    }


@router.get("/exports")
def list_exports(current: CurrentUserDep, db: DbSession) -> dict:
    current.require_view(Module.ADMIN)
    jobs = warehouse_service.list_jobs(db, tenant_id=current.tenant_id)
    return {
        "jobs": [
            {
                "id": j.id,
                "provider": j.provider,
                "dataset": j.dataset,
                "status": j.status,
                "row_count": j.row_count,
                "error_code": j.error_code,
            }
            for j in jobs
        ]
    }
