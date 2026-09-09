"""First-Party Audience Modeler — lookalikes without third-party cookies.

Interaction markers from the CRM and analytics are embedded and clustered
locally (see ``vectors.py``), and only the resulting segment — a label, a size
and the signals that define it — is pushed to an ad platform. No identifier,
no raw event, and no individual profile leaves the tenant boundary, which is
what makes this viable as cookie pools disappear rather than a re-labelling of
the same tracking.

Activating a segment on an ad platform is high impact: it is the point at
which something derived from customer behaviour reaches a third party.
"""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.base.context import AgentContext
from app.agents.base import scope as scope_rules
from app.agents.base.contracts import (
    AgentAction,
    AgentCategory,
    AgentResult,
    AgentSpec,
    BaseAgent,
    Impact,
)
from app.agents.first_party_audience_modeler import vectors
from app.connectors.base.connector import Capability
from app.connectors.base.interfaces import AdsConnector, CrmConnector
from app.core.logging import get_logger
from app.db.base import utcnow
from app.models.ads import AudienceCluster
from app.models.approval import ApprovalItem, ApprovalType
from app.models.workspace import Organization
from app.services.approvals import register_applier

log = get_logger(__name__)

SAMPLE_LIMIT = 2000
# A segment is rebuilt rather than re-pushed once it is this stale.
REFRESH_AFTER = timedelta(hours=12)


class FirstPartyAudienceModelerAgent(BaseAgent):
    spec = AgentSpec(
        slug="first_party_audience_modeler",
        name="First-Party Audience Modeler",
        category=AgentCategory.ADS,
        description="Builds lookalikes from first-party interaction vectors.",
        default_interval=timedelta(hours=3),
        default_schedule="Every 6 hours",
        required_capabilities=(Capability.READ_FIRST_PARTY_SIGNALS,),
        max_impact=Impact.HIGH,
        default_max_actions_per_day=10,
        scope_placeholder="Segments to keep, comma separated — blank to model freely",
    )

    def validate_scope(self, scope: str) -> str | None:
        """Segments to keep."""
        return scope_rules.free_text(scope, max_items=20, label="segments")

    def run(self, ctx: AgentContext) -> AgentResult:
        profiles = self._collect_signals(ctx)
        if len(profiles) < vectors.MIN_CLUSTER_SIZE:
            return AgentResult.skip(
                f"Only {len(profiles)} first-party profiles — need at least "
                f"{vectors.MIN_CLUSTER_SIZE} to model a segment"
            )

        clusters = self._build_clusters(ctx, profiles)
        stored = self._persist(ctx, clusters)
        actions = self._activate(ctx, stored)

        live = len([c for c in stored if c.is_live])
        return AgentResult(
            summary=(
                f"modelled {len(clusters)} segments from {len(profiles)} profiles, "
                f"{len(actions)} activations proposed"
            ),
            actions=actions,
            metric_label=f"{live or len(stored)} vector clusters live",
            detail={
                "profiles": len(profiles),
                "clusters": [
                    {"label": c.label, "size": c.size, "cohesion": c.cohesion}
                    for c in stored
                ],
            },
        )

    # ── Signals ────────────────────────────────────────────────────────────
    def _collect_signals(self, ctx: AgentContext) -> list[dict]:
        """Aggregated interaction markers from every connected source."""
        profiles: list[dict] = []
        for connector in ctx.all_with_capability(
            Capability.READ_FIRST_PARTY_SIGNALS
        ):
            source: CrmConnector = connector  # type: ignore[assignment]
            try:
                rows = source.read_first_party_signals(limit=SAMPLE_LIMIT)
            except Exception as exc:  # noqa: BLE001
                ctx.log_warning(
                    "Signal read failed for %s: %s", connector.slug, exc
                )
                continue
            profiles.extend(rows)
            if len(profiles) >= SAMPLE_LIMIT:
                break
        return profiles[:SAMPLE_LIMIT]

    # ── Modelling ──────────────────────────────────────────────────────────
    def _build_clusters(
        self, ctx: AgentContext, profiles: list[dict]
    ) -> list[vectors.Cluster]:
        embedded = [vectors.embed(profile.get("signals") or profile) for profile in profiles]
        k = vectors.suggest_k(len(embedded))
        groups = vectors.kmeans(embedded, k=k)

        wanted = {
            part.strip().lower() for part in (ctx.scope or "").split(",") if part.strip()
        }
        built: list[vectors.Cluster] = []

        for indices in groups:
            if not indices:
                continue
            members = [embedded[i] for i in indices]
            cohesion, top_signals = vectors.describe(members)
            label = vectors.label_for(top_signals)
            if wanted and label.lower() not in wanted:
                continue
            built.append(
                vectors.Cluster(
                    label=label,
                    size=len(indices),
                    cohesion=cohesion,
                    top_signals=top_signals,
                )
            )

        # Largest first: the biggest coherent segment is the most useful one.
        built.sort(key=lambda c: c.size, reverse=True)
        return built

    def _persist(
        self, ctx: AgentContext, clusters: list[vectors.Cluster]
    ) -> list[AudienceCluster]:
        existing = {
            row.label: row
            for row in ctx.db.execute(
                select(AudienceCluster).where(AudienceCluster.tenant_id == ctx.tenant_id)
            ).scalars()
        }
        stored: list[AudienceCluster] = []
        now = utcnow()

        for cluster in clusters:
            row = existing.get(cluster.label)
            if row is None:
                row = AudienceCluster(
                    tenant_id=ctx.tenant_id,
                    label=cluster.label,
                    size=cluster.size,
                    centroid_dims=vectors.DIMS,
                    cohesion=cluster.cohesion,
                    top_signals=cluster.top_signals,
                    activated_channels=[],
                    is_live=False,
                    refreshed_at=now,
                )
                ctx.db.add(row)
            else:
                row.size = cluster.size
                row.cohesion = cluster.cohesion
                row.top_signals = cluster.top_signals
                row.refreshed_at = now
            stored.append(row)

        # A segment that no longer emerges from the data is retired rather
        # than left live on the ad platforms with a stale definition.
        current = {c.label for c in clusters}
        for label, row in existing.items():
            if label not in current and row.is_live:
                row.is_live = False
                row.activated_channels = []

        ctx.db.flush()
        return stored

    # ── Activation ─────────────────────────────────────────────────────────
    def _activate(
        self, ctx: AgentContext, clusters: list[AudienceCluster]
    ) -> list[AgentAction]:
        platforms = [
            connector.slug
            for connector in ctx.all_with_capability(Capability.PUSH_AUDIENCE)
        ]
        if not platforms:
            return []

        actions: list[AgentAction] = []
        for cluster in clusters:
            if cluster.size < vectors.MIN_CLUSTER_SIZE or cluster.cohesion < vectors.MIN_COHESION:
                continue

            pending = [slug for slug in platforms if slug not in (cluster.activated_channels or [])]
            if not pending:
                # Already live everywhere; only re-push once it goes stale.
                if cluster.refreshed_at and utcnow() - cluster.refreshed_at < REFRESH_AFTER:
                    continue
                pending = platforms

            actions.append(
                AgentAction(
                    kind="activate_audience",
                    title=(
                        f'Activate "{cluster.label}" ({cluster.size:,} profiles) '
                        f"on {len(pending)} platform(s)"
                    ),
                    impact=Impact.HIGH,
                    approval_type=ApprovalType.AUDIENCE_ACTIVATION.value,
                    target_kind="audience_cluster",
                    target_id=cluster.id,
                    payload={
                        "cluster_id": cluster.id,
                        "label": cluster.label,
                        "size": cluster.size,
                        "cohesion": cluster.cohesion,
                        "top_signals": list(cluster.top_signals or []),
                        "platforms": pending,
                    },
                    apply=_make_activator(cluster.id, pending),
                    audit=(
                        f'activated the "{cluster.label}" segment '
                        f"({cluster.size:,} profiles) on {', '.join(pending)}"
                    ),
                )
            )
        return actions


def _make_activator(cluster_id: str, platforms: list[str]):  # noqa: ANN202
    def activate(ctx: AgentContext) -> None:
        cluster = ctx.db.get(AudienceCluster, cluster_id)
        if cluster is None:
            return

        activated = list(cluster.activated_channels or [])
        for slug in platforms:
            connector = ctx.optional_connector(slug)
            if connector is None:
                continue
            ads: AdsConnector = connector  # type: ignore[assignment]
            try:
                ads.push_audience(
                    label=cluster.label,
                    size=cluster.size,
                    # Signal labels only — the vectors themselves never leave.
                    signals=list(cluster.top_signals or []),
                )
            except Exception as exc:  # noqa: BLE001
                ctx.log_warning("Audience push to %s failed: %s", slug, exc)
                continue
            if slug not in activated:
                activated.append(slug)

        cluster.activated_channels = activated
        cluster.is_live = bool(activated)
        cluster.refreshed_at = utcnow()
        ctx.db.flush()

    return activate


@register_applier("audience_cluster")
def apply_activation(
    db: Session, org: Organization, item: ApprovalItem, payload: dict
) -> None:
    from app.models.agent import AgentRecord
    from app.orchestration.runner import build_context

    record = db.execute(
        select(AgentRecord).where(
            AgentRecord.tenant_id == org.id,
            AgentRecord.slug == "first_party_audience_modeler",
        )
    ).scalar_one_or_none()
    if record is None:
        raise LookupError("First-Party Audience Modeler is not installed in this workspace")

    ctx = build_context(db, record, org, trigger="approval")
    _make_activator(
        payload.get("cluster_id") or item.target_id or "",
        [str(p) for p in (payload.get("platforms") or [])],
    )(ctx)


AGENT = FirstPartyAudienceModelerAgent()
