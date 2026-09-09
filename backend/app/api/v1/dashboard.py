"""Dashboard and Reports — the KPI tiles, fleet summary and trend."""
from __future__ import annotations

from fastapi import APIRouter, Query

from app.api.deps import CurrentUserDep, DbSession
from app.core.money import compact as money_compact
from app.core.rbac import Module
from app.models.agent import AgentStatus
from app.schemas.common import Message
from app.schemas.workspace import (
    DashboardOut,
    KpiOut,
    NotificationsOut,
    ReportsOut,
)
from app.services import approvals as approval_service
from app.services import audit
from app.services import connectors as connector_service
from app.services import metrics, views

router = APIRouter(tags=["dashboard"])


@router.get("/dashboard", response_model=DashboardOut)
def dashboard(current: CurrentUserDep, db: DbSession) -> DashboardOut:
    """One call for the whole dashboard screen."""
    current.require_view(Module.DASHBOARD)

    agents = views.list_agents(
        db, tenant_id=current.tenant_id, can_write=current.can_write(Module.AGENTS)
    )
    connected, total_connectors = connector_service.counts(db, tenant_id=current.tenant_id)

    # The dashboard answers "what is happening right now", so it shows the
    # part of the fleet that is actually doing something — running, or errored
    # and therefore not. Showing the first six of the catalogue instead meant
    # a workspace with nothing started displayed six paused cards, which
    # answers a question nobody asked.
    active = [
        agent
        for agent in agents
        if agent.status in (AgentStatus.RUNNING.value, AgentStatus.ERROR.value)
    ]
    # Set up, and stopped. The panel needs these to tell "nothing configured"
    # apart from "configured and paused", which are opposite situations with
    # opposite next steps.
    ready = [
        agent
        for agent in agents
        if agent.configured and agent.status not in (
            AgentStatus.RUNNING.value,
            AgentStatus.ERROR.value,
        )
    ]
    live_connectors = (
        [
            views.connector_out(record, can_write=current.can_write(Module.CONNECTORS))
            for record in connector_service.list_records(db, tenant_id=current.tenant_id)
            if record.connected
        ]
        if current.can_view(Module.CONNECTORS)
        else []
    )
    pending = approval_service.pending_count(db, tenant_id=current.tenant_id)
    snapshot = metrics.snapshot(db, tenant_id=current.tenant_id, range_key="30d")

    # A Client Viewer can see the dashboard but not the approvals module, so
    # the queue panel is empty for them rather than forbidden.
    approval_items = (
        [
            views.approval_out(item)
            for item in approval_service.list_pending(
                db, tenant_id=current.tenant_id, limit=4
            )
        ]
        if current.can_view(Module.APPROVALS)
        else []
    )

    return DashboardOut(
        kpis=KpiOut(
            organic_sessions=metrics.compact(snapshot.organic_sessions),
            organic_sessions_raw=snapshot.organic_sessions,
            agents_running=sum(1 for a in agents if a.status == AgentStatus.RUNNING.value),
            agents_total=len(agents),
            connectors_connected=connected,
            connectors_total=total_connectors,
            pending_approvals=pending,
        ),
        # A slice of what is live; the hub shows the whole catalogue.
        agents=active[:6],
        active_agents=len(active),
        ready=ready[:6],
        ready_agents=len(ready),
        connectors=live_connectors[:6],
        approvals=approval_items,
        global_autonomy=current.organization.global_autonomy,
    )


@router.get("/notifications", response_model=NotificationsOut)
def notifications(
    current: CurrentUserDep, db: DbSession, limit: int = Query(default=20, ge=1, le=50)
) -> NotificationsOut:
    """Recent activity in this workspace — what the header's bell opens.

    Backed by the audit log rather than by transient client-side toasts. A
    toast the user missed while on another screen is gone forever, and a bell
    that counts only what happened in this tab since it loaded is a decoration.
    This is the same record the Admin screen shows, filtered to the modules the
    caller is allowed to see — a Client viewer must not learn about ad-budget
    moves through the notification panel that RBAC keeps off their Ads screen.
    """
    from app.core.rbac import Module, access_map

    allowed = {
        module for module, level in access_map(current.user.role).items() if level != "none"
    }
    entries = [
        views.audit_out(entry)
        # Over-fetched, because the filter below is applied in Python: module
        # is a display string on the row, not an enum the query can trust.
        for entry in audit.list_entries(db, tenant_id=current.tenant_id, limit=limit * 4)
        if not entry.module or entry.module in allowed
    ][:limit]

    return NotificationsOut(
        entries=entries,
        unseen=audit.unseen_count(
            db,
            tenant_id=current.tenant_id,
            since=current.user.notifications_seen_at,
            viewer_id=current.user.id,
            modules=allowed,
        ),
        pending_approvals=(
            approval_service.pending_count(db, tenant_id=current.tenant_id)
            if Module.APPROVALS.value in allowed
            else 0
        ),
    )


@router.post("/notifications/seen", response_model=Message)
def mark_notifications_seen(current: CurrentUserDep, db: DbSession) -> Message:
    """Clear this person's unseen count.

    A separate write rather than a side effect of reading the panel: a GET
    that mutates is the kind of thing a prefetch, a retry or a browser's
    speculative load quietly triggers, and the badge would clear without
    anyone having looked.
    """
    audit.mark_seen(db, user=current.user, tenant_id=current.tenant_id)
    db.commit()
    return Message(detail="Notifications marked as seen")


@router.get("/reports", response_model=ReportsOut)
def reports(
    current: CurrentUserDep,
    db: DbSession,
    range: str = Query(default="30d", pattern="^(7d|30d|90d)$"),
) -> ReportsOut:
    """KPI tiles and the organic-session trend for one range."""
    current.require_view(Module.REPORTS)
    snapshot = metrics.snapshot(db, tenant_id=current.tenant_id, range_key=range)

    return ReportsOut(
        range=snapshot.range_key,
        organic_sessions=metrics.compact(snapshot.organic_sessions),
        aeo_citations=snapshot.aeo_citations,
        backlinks_won=snapshot.backlinks_won,
        blended_cac=snapshot.blended_cac,
        # Money, so the money formatter — not the count formatter.
        ad_spend=money_compact(snapshot.ad_spend),
        fraud_blocked=snapshot.fraud_blocked,
        referral_spam_blocked=snapshot.referral_spam_blocked,
        pages_optimised=snapshot.pages_optimised,
        trend=snapshot.trend,
        # Pre-rendered for the SVG polyline the Reports chart draws.
    )
