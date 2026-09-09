"""Model package.

Importing this module registers every mapper, which both Alembic's autogenerate
and ``Base.metadata`` depend on.
"""
from app.models.ads import (
    AdChannel,
    AdCreative,
    AudienceCluster,
    BudgetAllocation,
    FraudEvent,
)
from app.models.agent import AgentRecord, AgentRun, AgentStatus, RunStatus
from app.models.approval import ApprovalItem, ApprovalStatus, ApprovalType
from app.models.connector import ConnectorCategory, ConnectorHealth, ConnectorRecord
from app.models.identity import AuthIdentity, CodePurpose, LoginCode
from app.models.metrics import DailyMetric
from app.models.offpage import BacklinkStatus, BacklinkTarget, CompetitorAlert, OutreachPitch
from app.models.seo import (
    AeoQaPair,
    PageStatus,
    ReferralSpamEvent,
    SchemaPatch,
    SeoIssue,
    SeoPage,
)
from app.models.workspace import (
    AuditLogEntry,
    Invitation,
    OnboardingState,
    Organization,
    PlanTier,
    RefreshToken,
    User,
)

__all__ = [
    "AdChannel",
    "AdCreative",
    "AeoQaPair",
    "AgentRecord",
    "AgentRun",
    "AgentStatus",
    "ApprovalItem",
    "ApprovalStatus",
    "ApprovalType",
    "AudienceCluster",
    "AuthIdentity",
    "AuditLogEntry",
    "BacklinkStatus",
    "BacklinkTarget",
    "BudgetAllocation",
    "CodePurpose",
    "CompetitorAlert",
    "ConnectorCategory",
    "ConnectorHealth",
    "ConnectorRecord",
    "DailyMetric",
    "FraudEvent",
    "Invitation",
    "LoginCode",
    "OnboardingState",
    "Organization",
    "OutreachPitch",
    "PageStatus",
    "PlanTier",
    "RefreshToken",
    "ReferralSpamEvent",
    "RunStatus",
    "SchemaPatch",
    "SeoIssue",
    "SeoPage",
    "User",
]
