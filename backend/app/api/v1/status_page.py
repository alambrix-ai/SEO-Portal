"""Public status page — platform health only, no tenant data."""
from __future__ import annotations

from fastapi import APIRouter
from sqlalchemy import text

from app.db.base import utcnow
from app.db.session import engine

router = APIRouter(prefix="/status", tags=["status"])


@router.get("")
def public_status() -> dict:
    """Unauthenticated platform status.

    Intentionally omits organisation names, tenant counts, user emails, and
    any other tenant-scoped signal.
    """
    db_ok = False
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        db_ok = True
    except Exception:  # noqa: BLE001 - status stays up even if DB is down
        db_ok = False

    components = [
        {"name": "api", "status": "operational"},
        {"name": "database", "status": "operational" if db_ok else "degraded"},
        {"name": "agent_runner", "status": "operational"},
    ]
    overall = "operational" if db_ok else "degraded"
    return {
        "status": overall,
        "checked_at": utcnow().isoformat(),
        "components": components,
    }
