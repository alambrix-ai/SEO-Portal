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
from app.agents.base.contracts import ConnectorUse, UseMode
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
# Agent setting holding the platform audience ids each segment was created as.
REMOTE_IDS_KEY = "remote_audience_ids"


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
        connector_use=(
            ConnectorUse(
                Capability.READ_FIRST_PARTY_SIGNALS,
                UseMode.ALL,
                question="Which CRMs should this agent read customer engagement from?",
                group="CRMs",
            ),
            ConnectorUse(
                Capability.PUSH_AUDIENCE,
                UseMode.ALL,
                question="Which ad platforms should this agent create audiences on?",
                group="ad platforms that can build audiences",
            ),
        ),
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
        return merge_by_label(built)

    def _persist(
        self, ctx: AgentContext, clusters: list[vectors.Cluster]
    ) -> list[AudienceCluster]:
        # Upsert by label. Rows are kept one per label: earlier versions could
        # create two rows with the same label in one pass (k-means groups that
        # describe the same signals), and the first one found is kept.
        existing: dict[str, AudienceCluster] = {}
        duplicates: list[AudienceCluster] = []
        for row in ctx.db.execute(
            select(AudienceCluster)
            .where(AudienceCluster.tenant_id == ctx.tenant_id)
            .order_by(AudienceCluster.created_at)
        ).scalars():
            if row.label in existing:
                duplicates.append(row)
            else:
                existing[row.label] = row

        stored: list[AudienceCluster] = []
        now = utcnow()
        remote = _remote_ids(ctx)

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
                existing[cluster.label] = row
            else:
                row.size = cluster.size
                row.cohesion = cluster.cohesion
                row.top_signals = cluster.top_signals
                # When the *model* was last rebuilt. Activation does not use
                # this: the connectors can only create audiences, so an
                # activated one is never re-pushed (see _activate).
                row.refreshed_at = now
                pushed = sorted((remote.get(row.id) or {}).keys())
                if not row.is_live and pushed:
                    # A retired segment that has re-emerged. Its audiences
                    # were never removed upstream (retiring is local only),
                    # so they are still there and it is live again on them.
                    row.activated_channels = pushed
                    row.is_live = True
            stored.append(row)

        # A segment that no longer emerges from the data is retired here.
        # This is local only: none of the ad connectors can delete or pause
        # a custom audience, so the audience stays on the platform until
        # somebody removes it there. The console shows it as Retired.
        current = {c.label for c in clusters}
        for label, row in existing.items():
            if label not in current and row.is_live:
                row.is_live = False
                row.activated_channels = []
        for row in duplicates:
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
        remote = _remote_ids(ctx)
        for cluster in clusters:
            if cluster.size < vectors.MIN_CLUSTER_SIZE or cluster.cohesion < vectors.MIN_COHESION:
                continue

            # Only platforms this segment has never been pushed to. Every
            # push_audience call creates a *new* audience on the platform —
            # none of the connectors can update one — so re-pushing an
            # activated segment (as the old 12-hour refresh meant to) would
            # pile up copies upstream. Once there, it stays as first pushed.
            known = remote.get(cluster.id) or {}
            activated = cluster.activated_channels or []
            pending = [slug for slug in platforms if slug not in activated and slug not in known]
            if not pending:
                continue

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
                    writes_via=tuple(pending),
                )
            )
        return actions


def merge_by_label(clusters: list[vectors.Cluster]) -> list[vectors.Cluster]:
    """One cluster per label.

    Two k-means groups can be described by the same top signals and so get
    the same label; stored separately they became duplicate rows (and
    duplicate audiences). They are one segment: sizes add up, cohesion is the
    size-weighted mean, and the larger group's signals describe it.
    """
    merged: dict[str, vectors.Cluster] = {}
    for cluster in clusters:
        into = merged.get(cluster.label)
        if into is None:
            merged[cluster.label] = vectors.Cluster(
                label=cluster.label,
                size=cluster.size,
                cohesion=cluster.cohesion,
                top_signals=list(cluster.top_signals),
                centroid=list(cluster.centroid),
            )
            continue
        total = into.size + cluster.size
        if total:
            into.cohesion = round(
                (into.cohesion * into.size + cluster.cohesion * cluster.size) / total, 4
            )
        if cluster.size > into.size:
            into.top_signals = list(cluster.top_signals)
        into.size = total
    out = list(merged.values())
    out.sort(key=lambda c: c.size, reverse=True)
    return out


def _remote_ids(ctx: AgentContext) -> dict[str, dict[str, str]]:
    """{cluster id: {connector slug: platform audience id}}.

    Kept in the agent's own settings because AudienceCluster has no field for
    it, and activated_channels is a list of slugs the API and console read.
    """
    stored = ctx.setting(REMOTE_IDS_KEY, {}) or {}
    if not isinstance(stored, dict):
        return {}
    return {str(k): dict(v) for k, v in stored.items() if isinstance(v, dict)}


def _make_activator(cluster_id: str, platforms: list[str]):  # noqa: ANN202
    def activate(ctx: AgentContext) -> None:
        from app.core.exceptions import ConnectorError
        from app.core.user_messages import public_error_message

        cluster = ctx.db.get(AudienceCluster, cluster_id)
        if cluster is None:
            return

        remote = _remote_ids(ctx)
        known = dict(remote.get(cluster.id) or {})
        activated = list(cluster.activated_channels or [])
        failures: list[tuple[str, str]] = []
        for slug in platforms:
            if slug in known:
                # Already created there; pushing again would make a second
                # audience, not update this one.
                if slug not in activated:
                    activated.append(slug)
                continue
            connector = ctx.optional_connector(slug)
            if connector is None:
                failures.append((slug, "not connected"))
                continue
            ads: AdsConnector = connector  # type: ignore[assignment]
            try:
                remote_id = ads.push_audience(
                    label=cluster.label,
                    size=cluster.size,
                    # Signal labels only — the vectors themselves never leave.
                    signals=list(cluster.top_signals or []),
                )
            except Exception as exc:  # noqa: BLE001 - collected and raised below
                ctx.log_warning("Audience push to %s failed: %s", slug, exc)
                failures.append((slug, public_error_message(exc, fallback="")))
                continue
            known[slug] = str(remote_id or "")
            if slug not in activated:
                activated.append(slug)

        remote[cluster.id] = known
        ctx.set_setting(REMOTE_IDS_KEY, remote)
        cluster.activated_channels = activated
        cluster.is_live = bool(activated)
        ctx.db.flush()

        if failures:
            names = ", ".join(
                ctx.connector_name(slug) + (f" ({reason})" if reason else "")
                for slug, reason in failures
            )
            raise ConnectorError(
                f'The "{cluster.label}" audience could not be created on {names}.'
                + (" It is live on the other platforms." if activated else "")
            )

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
