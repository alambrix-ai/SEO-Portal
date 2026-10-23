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
from app.models.membership import WorkspaceMembership
from app.models.content import GeneratedImage, MarketingAsset
from app.models.creative import (
    BrandKit,
    CreativeJob,
    CreativeJobStatus,
    FileKind,
    FileSource,
    Product,
    StoredFile,
)
from app.models.demo import DemoAnalysis, SiteAnalysis
from app.models.history import AuditSnapshot, SnapshotActor, SnapshotKind, SnapshotSource
from app.models.local import LocalRankCheck, ReviewReply
from app.models.metrics import DailyMetric
from app.models.oauth import OAuthState
from app.models.offpage import BacklinkStatus, BacklinkTarget, CompetitorAlert, OutreachPitch
from app.models.portal import FeatureKind, PlatformOAuthApp, PortalFeatureFlag
from app.models.social import MetricPoint, SocialPostMetric
from app.models.verification import ConnectorVerification, PlatformSetting
from app.models.seo import (
    AeoQaPair,
    PageStatus,
    ReferralSpamEvent,
    SchemaPatch,
    SeoIssue,
    SeoPage,
)
from app.models.topic_cluster import ContentBrief, TopicCluster, TopicClusterPage
from app.models.alerts import AlertEvent, AlertRule
from app.models.product_feed import ProductFeedFinding
from app.models.pseo import PseoDataset, PseoPageJob, PseoTemplate
from app.models.repurpose import ContentRepurposeJob
from app.models.reports import ReportArtifact, ScheduledReport
from app.models.video_script import VideoScriptJob
from app.models.willy import WillyGroundingTrace
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
    "AuditSnapshot",
    "SnapshotActor",
    "SnapshotKind",
    "SnapshotSource",
    "DemoAnalysis",
    "SiteAnalysis",
    "GeneratedImage",
    "MarketingAsset",
    "BrandKit",
    "CreativeJob",
    "CreativeJobStatus",
    "FileKind",
    "FileSource",
    "Product",
    "StoredFile",
    "LocalRankCheck",
    "ReviewReply",
    "AdChannel",
    "AdCreative",
    "AlertEvent",
    "AlertRule",
    "AeoQaPair",
    "AgentRecord",
    "AgentRun",
    "AgentStatus",
    "ApprovalItem",
    "ApprovalStatus",
    "ApprovalType",
    "AudienceCluster",
    "AuthIdentity",
    "WorkspaceMembership",
    "AuditLogEntry",
    "BacklinkStatus",
    "BacklinkTarget",
    "BudgetAllocation",
    "CodePurpose",
    "CompetitorAlert",
    "ContentBrief",
    "ConnectorCategory",
    "ConnectorHealth",
    "ConnectorRecord",
    "DailyMetric",
    "FraudEvent",
    "Invitation",
    "LoginCode",
    "MetricPoint",
    "OAuthState",
    "OnboardingState",
    "Organization",
    "OutreachPitch",
    "PageStatus",
    "PlanTier",
    "PlatformOAuthApp",
    "PlatformSetting",
    "ConnectorVerification",
    "PortalFeatureFlag",
    "FeatureKind",
    "PseoDataset",
    "PseoPageJob",
    "PseoTemplate",
    "ProductFeedFinding",
    "ContentRepurposeJob",
    "VideoScriptJob",
    "RefreshToken",
    "ReferralSpamEvent",
    "ReportArtifact",
    "ScheduledReport",
    "RunStatus",
    "SchemaPatch",
    "SeoIssue",
    "SeoPage",
    "SocialPostMetric",
    "TopicCluster",
    "TopicClusterPage",
    "User",
    "WillyGroundingTrace",
]
