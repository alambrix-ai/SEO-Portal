"""v1 router aggregation."""
from __future__ import annotations

from fastapi import APIRouter

from app.api.v1 import (
    admin,
    ads,
    agency,
    agents,
    alerts,
    billing,
    approvals,
    assistant,
    auth,
    brand_kit,
    connectors,
    content,
    creative,
    csp,
    dashboard,
    enterprise,
    files,
    history,
    insights,
    oauth,
    offpage,
    onboarding,
    portal,
    public_demo,
    safety,
    seo,
    site_analysis,
    social,
    topics,
    verification,
    visibility,
    workflow,
    workspaces,
)

api_router = APIRouter()

# Unauthenticated (or self-service) first, then the console's screens in the
# order they appear in the navigation.
api_router.include_router(auth.router)
api_router.include_router(public_demo.router)
# Public: the redirect URI sign-in providers call back.
api_router.include_router(oauth.router)
# Public: browsers post CSP violation reports here without credentials.
api_router.include_router(csp.router)
api_router.include_router(workspaces.router)
api_router.include_router(dashboard.router)
api_router.include_router(onboarding.router)
api_router.include_router(assistant.router)
api_router.include_router(agents.router)
api_router.include_router(seo.router)
api_router.include_router(visibility.router)
api_router.include_router(topics.router)
api_router.include_router(alerts.router)
api_router.include_router(site_analysis.router)
api_router.include_router(history.router)
api_router.include_router(offpage.router)
api_router.include_router(ads.router)
api_router.include_router(connectors.router)
api_router.include_router(content.router)
api_router.include_router(creative.router)
api_router.include_router(social.router)
api_router.include_router(brand_kit.router)
api_router.include_router(files.router)
api_router.include_router(approvals.router)
api_router.include_router(insights.router)
api_router.include_router(safety.router)
api_router.include_router(admin.router)
    api_router.include_router(enterprise.router)
    api_router.include_router(agency.router)
    api_router.include_router(workflow.router)
    api_router.include_router(billing.router)
    api_router.include_router(portal.router)
api_router.include_router(verification.router)

__all__ = ["api_router"]
