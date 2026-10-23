"""Multi-step approvals, scoped API tokens, signed webhooks, ROI snapshots."""
from __future__ import annotations

from datetime import timedelta
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session

from app.db.base import Base, new_id, utcnow
from app.models.approval import ApprovalItem, ApprovalStatus, ApprovalType
from app.models.workflow import (
    ApiToken,
    ApprovalStep,
    OutboundWebhook,
    RoiAttributionSnapshot,
    WebhookDeliveryLog,
)
from app.models.workspace import AuditLogEntry, Organization
from app.services import workflow as workflow_svc
from tests.test_seo_pipeline_fixes import TENANT, PlainCipher


@compiles(JSONB, "sqlite")
def _jsonb_on_sqlite(type_, compiler, **kw):  # noqa: ANN001, ANN003, ANN202
    return "JSON"


@compiles(PG_UUID, "sqlite")
def _uuid_on_sqlite(type_, compiler, **kw):  # noqa: ANN001, ANN003, ANN202
    return "CHAR(36)"


@pytest.fixture
def wf_db():  # noqa: ANN201
    engine = create_engine("sqlite://")
    tables = [
        Organization.__table__,
        AuditLogEntry.__table__,
        ApprovalItem.__table__,
        ApprovalStep.__table__,
        ApiToken.__table__,
        OutboundWebhook.__table__,
        WebhookDeliveryLog.__table__,
        RoiAttributionSnapshot.__table__,
    ]
    Base.metadata.create_all(engine, tables=tables)
    with Session(engine) as session:
        org = Organization(
            id=TENANT,
            name="Acme",
            slug="acme-wf",
            wrapped_dek="test",
            dek_version=1,
            plan_name="Enterprise",
            plan_tier="enterprise",
            seats_total=10,
            is_active=True,
            data_region="eu-central",
            settings={},
            ip_allowlist_enabled=False,
            mfa_required=False,
        )
        session.add(org)
        session.flush()
        yield session, org
    engine.dispose()


def test_rls_registered() -> None:
    from app.db.rls import TENANT_SCOPED_TABLES

    for name in (
        "approval_steps",
        "api_tokens",
        "outbound_webhooks",
        "webhook_delivery_logs",
        "roi_attribution_snapshots",
    ):
        assert name in TENANT_SCOPED_TABLES


def test_approval_chain_order(wf_db):  # noqa: ANN001
    db, org = wf_db
    item = ApprovalItem(
        tenant_id=org.id,
        type=ApprovalType.TECHNICAL_FIX.value,
        title="Fix",
        status=ApprovalStatus.PENDING.value,
        submitted_at=utcnow(),
    )
    db.add(item)
    db.flush()
    steps = workflow_svc.define_approval_chain(
        db,
        org,
        item,
        steps=[
            {"required_role": "approver", "sla_hours": 12},
            {"required_role": "admin", "sla_hours": 24},
        ],
        actor_email="a@example.com",
    )
    assert len(steps) == 2
    uid1, uid2 = new_id(), new_id()
    with pytest.raises(ValueError, match="prior"):
        workflow_svc.decide_step(
            db,
            org,
            steps[1],
            decision="approved",
            actor_user_id=uid2,
            actor_email="boss@example.com",
            actor_role="admin",
        )
    workflow_svc.decide_step(
        db,
        org,
        steps[0],
        decision="approved",
        actor_user_id=uid1,
        actor_email="a@example.com",
        actor_role="approver",
    )
    workflow_svc.decide_step(
        db,
        org,
        steps[1],
        decision="approved",
        actor_user_id=uid2,
        actor_email="boss@example.com",
        actor_role="admin",
    )
    assert steps[1].status == "approved"


def test_api_token_scoped_auth(wf_db):  # noqa: ANN001
    db, org = wf_db
    cipher = PlainCipher()
    minted = workflow_svc.mint_api_token(
        db,
        org,
        cipher,
        label="metrics",
        scopes=["metrics:read"],
        created_by_id=None,
        actor_email="a@example.com",
    )
    assert minted.raw_token.startswith("srk_")
    ok = workflow_svc.authenticate_api_token(
        db, minted.raw_token, required_scope="metrics:read"
    )
    assert ok is not None and ok[0].id == org.id
    assert (
        workflow_svc.authenticate_api_token(
            db, minted.raw_token, required_scope="approvals:write"
        )
        is None
    )
    workflow_svc.revoke_api_token(db, org, minted.row.id, actor_email="a@example.com")
    assert (
        workflow_svc.authenticate_api_token(
            db, minted.raw_token, required_scope="metrics:read"
        )
        is None
    )


def test_webhook_rejects_private_url(wf_db):  # noqa: ANN001
    db, org = wf_db
    cipher = PlainCipher()
    with pytest.raises(Exception):
        workflow_svc.register_webhook(
            db,
            org,
            cipher,
            name="bad",
            url="http://127.0.0.1/hooks",
            events=["approval.decided"],
            secret="x" * 20,
            created_by_id=None,
            actor_email="a@example.com",
        )


def test_webhook_signature_and_delivery(wf_db):  # noqa: ANN001
    db, org = wf_db
    cipher = PlainCipher()
    url = "https://hooks.example.com/synaprank"
    with patch("app.services.workflow.netguard.check_public_url", return_value=url):
        hook = workflow_svc.register_webhook(
            db,
            org,
            cipher,
            name="ok",
            url=url,
            events=["approval.decided"],
            secret="super-secret-value",
            created_by_id=None,
            actor_email="a@example.com",
        )
    body = b'{"ok":true}'
    sig = workflow_svc.sign_webhook_payload(
        "super-secret-value", body=body, timestamp=1_700_000_000, nonce="abc"
    )
    assert len(sig) == 64
    fake_resp = MagicMock(status_code=204)
    with patch("app.services.workflow.egress.pinned_client") as pinned:
        pinned.return_value.__enter__.return_value = MagicMock()
        pinned.return_value.__exit__.return_value = None
        with patch("app.services.workflow.egress.fetch", return_value=fake_resp):
            with patch("app.services.workflow.netguard.check_public_url", return_value=url):
                log = workflow_svc.deliver_webhook(
                    db,
                    org,
                    cipher,
                    hook,
                    event="approval.decided",
                    payload={"ok": True},
                )
    assert log.success is True
    assert log.nonce


def test_roi_blended_roas(wf_db):  # noqa: ANN001
    db, org = wf_db
    start = utcnow() - timedelta(days=7)
    end = utcnow()
    row = workflow_svc.record_roi_snapshot(
        db,
        org,
        period_start=start,
        period_end=end,
        channels={
            "google_ads": {"spend": "100", "revenue": "250", "conversions": "10"},
            "meta_ads": {"spend": "50", "revenue": "50", "conversions": "2"},
        },
        created_by_id=None,
        actor_email="a@example.com",
    )
    assert row.blended_roas == "2.0000"


def test_api_token_rejects_unknown_scope(wf_db):  # noqa: ANN001
    db, org = wf_db
    with pytest.raises(ValueError, match="scopes"):
        workflow_svc.mint_api_token(
            db,
            org,
            PlainCipher(),
            label="x",
            scopes=["admin:all"],
            created_by_id=None,
            actor_email="a@example.com",
        )
