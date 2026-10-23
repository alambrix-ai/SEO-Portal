"""Anomaly alert rule CRUD (RBAC: SEO write)."""
from __future__ import annotations

from fastapi import APIRouter
from pydantic import Field
from sqlalchemy import select

from app.api.deps import ClientIp, CurrentUserDep, DbSession
from app.core.exceptions import InvalidInputError, NotFoundError
from app.core.rbac import Module
from app.models.alerts import AlertEvent, AlertRule
from app.schemas.common import ActionResult, ApiModel, Toast
from app.services import anomaly_alerts as alerts_svc
from app.services import audit as audit_service

router = APIRouter(prefix="/alerts", tags=["alerts"])


class AlertRuleIn(ApiModel):
    name: str = Field(min_length=2, max_length=120)
    metric: str = Field(max_length=64)
    threshold: float = 0.3
    channel: str = "email"
    webhook_url: str = ""


class AlertRuleOut(ApiModel):
    id: str
    name: str
    metric: str
    threshold: float
    channel: str
    webhook_url: str = ""
    enabled: str


class AlertEventOut(ApiModel):
    id: str
    rule_id: str
    metric: str
    summary: str
    status: str
    dedupe_key: str


class AlertsWorkspaceOut(ApiModel):
    rules: list[AlertRuleOut]
    events: list[AlertEventOut]
    read_only: bool = False


@router.get("", response_model=AlertsWorkspaceOut)
def workspace(current: CurrentUserDep, db: DbSession) -> AlertsWorkspaceOut:
    current.require_view(Module.SEO)
    rules = list(
        db.execute(
            select(AlertRule)
            .where(AlertRule.tenant_id == current.tenant_id)
            .order_by(AlertRule.created_at.desc())
        ).scalars()
    )
    events = list(
        db.execute(
            select(AlertEvent)
            .where(AlertEvent.tenant_id == current.tenant_id)
            .order_by(AlertEvent.fired_at.desc())
            .limit(50)
        ).scalars()
    )
    return AlertsWorkspaceOut(
        rules=[
            AlertRuleOut(
                id=r.id,
                name=r.name,
                metric=r.metric,
                threshold=r.threshold,
                channel=r.channel,
                webhook_url=r.webhook_url if current.can_write(Module.SEO) else "",
                enabled=r.enabled,
            )
            for r in rules
        ],
        events=[
            AlertEventOut(
                id=e.id,
                rule_id=e.rule_id,
                metric=e.metric,
                summary=e.summary,
                status=e.status,
                dedupe_key=e.dedupe_key,
            )
            for e in events
        ],
        read_only=not current.can_write(Module.SEO),
    )


@router.post("/rules", response_model=ActionResult)
def create_rule(
    body: AlertRuleIn, current: CurrentUserDep, db: DbSession, ip: ClientIp
) -> ActionResult:
    current.require_write(Module.SEO)
    # validate_rule_input / netguard raise InvalidInputError with operator-facing copy.
    data = alerts_svc.validate_rule_input(
        name=body.name,
        metric=body.metric,
        threshold=body.threshold,
        channel=body.channel,
        webhook_url=body.webhook_url,
    )

    rule = AlertRule(tenant_id=current.tenant_id, enabled="on", **data)
    db.add(rule)
    audit_service.record_user_action(
        db,
        user=current.user,
        action=f"created alert rule {data['name']}",
        module="seo",
        ip_address=ip,
    )
    db.commit()
    return ActionResult(toast=Toast(message="Alert rule saved"))


@router.post("/rules/{rule_id}/disable", response_model=ActionResult)
def disable_rule(
    rule_id: str, current: CurrentUserDep, db: DbSession, ip: ClientIp
) -> ActionResult:
    current.require_write(Module.SEO)
    rule = db.execute(
        select(AlertRule).where(
            AlertRule.tenant_id == current.tenant_id, AlertRule.id == rule_id
        )
    ).scalar_one_or_none()
    if rule is None:
        raise NotFoundError("Alert rule not found")
    rule.enabled = "off"
    audit_service.record_user_action(
        db,
        user=current.user,
        action=f"disabled alert rule {rule.name}",
        module="seo",
        ip_address=ip,
    )
    db.commit()
    return ActionResult(toast=Toast(message="Alert rule disabled", kind="info"))
