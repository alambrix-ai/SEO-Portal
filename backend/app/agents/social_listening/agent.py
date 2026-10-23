"""Ingest social mentions, score sentiment, propose reply drafts for negatives."""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.base.context import AgentContext
from app.agents.base.contracts import (
    AgentAction,
    AgentCategory,
    AgentResult,
    AgentSpec,
    BaseAgent,
    Impact,
)
from app.db.base import utcnow
from app.models.approval import ApprovalItem, ApprovalType
from app.models.social_mentions import SocialMention
from app.models.workspace import Organization
from app.services import social_listening as listening
from app.services.approvals import register_applier, register_rejecter
from app.services.encryption import Ctx, OrgCipher

TARGET_KIND = "social_mention"
SLUG = "social_listening"
MAX_ACTIONS = 5


class SocialListeningAgent(BaseAgent):
    spec = AgentSpec(
        slug=SLUG,
        name="Social Listening",
        category=AgentCategory.LOCAL,
        description=(
            "Ingests social mentions, scores sentiment, and routes negatives to "
            "approval-gated reply drafts (same human gate as review replies)."
        ),
        default_interval=timedelta(hours=6),
        default_schedule="Every 6 hours",
        required_capabilities=(),
        max_impact=Impact.HIGH,
        default_max_actions_per_day=15,
        scope_placeholder="One mention per line: network|id|author|text",
        requires_llm=False,
    )

    def run(self, ctx: AgentContext) -> AgentResult:
        incoming = listening.parse_scope_mentions(ctx.scope or "")
        if not incoming:
            return AgentResult.skip(
                "No mentions in scope — paste network|id|author|text lines, "
                "or connect a listening source"
            )

        actions: list[AgentAction] = []
        ingested = 0
        for item in incoming:
            # Fence third-party text before any future model use; never log it.
            _ = listening.as_untrusted(item.text)
            sentiment = listening.score_sentiment(item.text)
            existing = ctx.db.execute(
                select(SocialMention).where(
                    SocialMention.tenant_id == ctx.tenant_id,
                    SocialMention.network == item.network,
                    SocialMention.external_id == item.external_id,
                )
            ).scalar_one_or_none()
            if existing is not None and existing.status in {"replied", "ignored", "routed"}:
                continue
            if existing is None:
                existing = SocialMention(
                    tenant_id=ctx.tenant_id,
                    network=item.network,
                    external_id=item.external_id,
                    author=item.author,
                    text_encrypted=ctx.cipher.encrypt(
                        item.text, context=Ctx.SOCIAL_MENTION
                    ),
                    observed_at=utcnow(),
                    detail={},
                )
                ctx.db.add(existing)
                ingested += 1
            existing.sentiment = sentiment.label
            existing.sentiment_score = sentiment.score
            existing.author = item.author[:200]

            if sentiment.label != "negative":
                existing.status = "ignored" if sentiment.label == "positive" else "new"
                continue
            if len(actions) >= min(MAX_ACTIONS, max(ctx.remaining_actions, 1)):
                continue

            reply = listening.draft_reply(
                business=getattr(ctx.org, "name", "") or "our team",
                author=item.author,
                text=item.text,
                sentiment=sentiment.label,
            )
            existing.reply_encrypted = ctx.cipher.encrypt(reply, context=Ctx.SOCIAL_MENTION)
            existing.status = "routed"
            ctx.db.flush()
            actions.append(
                AgentAction(
                    kind="reply_social_mention",
                    title=f'Reply to negative {item.network} mention from {item.author or "user"}',
                    impact=Impact.HIGH,
                    approval_type=ApprovalType.SOCIAL_REPLY.value,
                    target_kind=TARGET_KIND,
                    target_id=existing.id,
                    payload={
                        "mention_id": existing.id,
                        "network": item.network,
                        "author": item.author[:80],
                        "reply": reply,
                        "sentiment": sentiment.label,
                    },
                    apply=_make_applier(existing.id),
                    audit=f"approved social reply on {item.network}",
                    metrics={"social_replies": 1},
                )
            )

        ctx.db.flush()
        if not actions and ingested == 0:
            return AgentResult.skip("No new actionable mentions")
        if not actions:
            return AgentResult(
                summary=f"Ingested {ingested} mention{'s' if ingested != 1 else ''}; none needed a reply",
                metric_label=f"{ingested} mention ingested",
                metrics={"social_mentions_ingested": ingested},
            )
        return AgentResult(
            summary=(
                f"Routed {len(actions)} negative mention{'s' if len(actions) != 1 else ''} "
                f"for reply approval"
                + (f"; ingested {ingested}" if ingested else "")
            ),
            actions=actions,
            metric_label=f"{len(actions)} reply proposed",
            metrics={
                "social_mentions_ingested": ingested,
                "social_replies_proposed": len(actions),
            },
        )


def _make_applier(mention_id: str):  # noqa: ANN202
    def apply(ctx: AgentContext) -> None:
        _mark_replied(ctx.db, ctx.org, mention_id)

    return apply


def _mark_replied(db: Session, org: Organization, mention_id: str) -> None:
    mention = db.get(SocialMention, mention_id)
    if mention is None or mention.tenant_id != org.id:
        raise LookupError("Mention no longer exists")
    # Publishing to the network requires a channel-specific connector; approval
    # marks the draft ready / recorded so operators can post or export it.
    mention.status = "replied"
    db.flush()


@register_applier(TARGET_KIND)
def apply_social_mention(
    db: Session, org: Organization, item: ApprovalItem, payload: dict
) -> None:
    mention_id = str((payload or {}).get("mention_id") or item.target_id or "")
    if not mention_id:
        raise ValueError("Missing mention id")
    reply = str((payload or {}).get("reply") or "")
    mention = db.get(SocialMention, mention_id)
    if mention is None or mention.tenant_id != org.id:
        raise LookupError("Mention no longer exists")
    if reply:
        cipher = OrgCipher.for_org(org)
        mention.reply_encrypted = cipher.encrypt(reply, context=Ctx.SOCIAL_MENTION)
    _mark_replied(db, org, mention_id)
    mention.approval_item_id = item.id


@register_rejecter(TARGET_KIND)
def reject_social_mention(
    db: Session, org: Organization, item: ApprovalItem, payload: dict
) -> None:
    mention_id = str((payload or {}).get("mention_id") or item.target_id or "")
    mention = db.get(SocialMention, mention_id) if mention_id else None
    if mention is None or mention.tenant_id != org.id:
        return
    mention.status = "ignored"
    mention.approval_item_id = item.id
    db.flush()


AGENT = SocialListeningAgent()
