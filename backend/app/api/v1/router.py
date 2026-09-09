"""v1 router aggregation."""
from __future__ import annotations

from fastapi import APIRouter

from app.api.v1 import (
    admin,
    ads,
    agents,
    approvals,
    auth,
    connectors,
    dashboard,
    offpage,
    onboarding,
    seo,
)

api_router = APIRouter()

# Unauthenticated (or self-service) first, then the console's screens in the
# order they appear in the navigation.
api_router.include_router(auth.router)
api_router.include_router(dashboard.router)
api_router.include_router(onboarding.router)
api_router.include_router(agents.router)
api_router.include_router(seo.router)
api_router.include_router(offpage.router)
api_router.include_router(ads.router)
api_router.include_router(connectors.router)
api_router.include_router(approvals.router)
api_router.include_router(admin.router)

__all__ = ["api_router"]
