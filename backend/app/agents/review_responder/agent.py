"""Review Responder — replies to Google reviews the business has not answered.

Reads the business's newest Google reviews, drafts a reply to each one that
has none, checks the draft (no promised discounts or refunds, no contact
details or links, a sensible length), and proposes it. Replies are public and
written in the business's name, so they are high impact: under the Hybrid
policy every one waits for a person to approve it.
"""
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
from app.agents.base.contracts import ConnectorUse, UseMode
from app.agents.review_responder import prompts
from app.connectors.base.connector import Capability
from app.db.base import utcnow
from app.models.approval import ApprovalItem
from app.models.local import ReviewReply
from app.models.workspace import Organization
from app.services.approvals import register_applier, register_rejecter
from app.services.encryption import Ctx

#: Replies drafted per run — each is a model call. Scaled up to this bound
#: when the agent's daily limit leaves room for more.
MAX_PER_RUN = 20
#: A draft the safety check threw away is retried after this long, not on
#: every run (each retry is a paid model call that would fail the same way).
RETRY_DISCARDED_AFTER = timedelta(days=7)
#: Pending approvals expire after 30 days (approvals.expire_stale); a draft
#: older than that is no longer waiting on anyone and is written again.
DRAFT_STALE_AFTER = timedelta(days=31)


class ReviewResponderAgent(BaseAgent):
    spec = AgentSpec(
        slug="review_responder",
        name="Review Responder",
        category=AgentCategory.LOCAL,
        description="Drafts and posts replies to your Google reviews that have not been answered.",
        default_interval=timedelta(hours=6),
        default_schedule="Every 6 hours",
        required_capabilities=(Capability.READ_REVIEWS, Capability.REPLY_REVIEW),
        max_impact=Impact.HIGH,
        default_max_actions_per_day=10,
        requires_llm=True,
        connector_use=(
            ConnectorUse(
                Capability.READ_REVIEWS,
                UseMode.ONE,
                question=(
                    "Which business profile should this agent reply to reviews on?"
                ),
                also=(Capability.REPLY_REVIEW,),
                note=(
                    "Replies are posted on the same profile the reviews are read from."
                ),
            ),
        ),
    )

    def run(self, ctx: AgentContext) -> AgentResult:
        profile = ctx.with_capability(Capability.READ_REVIEWS)
        if profile is None:
            return AgentResult.skip("Waiting on Google Business Profile")

        ctx.progress("Reading your newest Google reviews")
        reviews = profile.read_reviews(limit=50)  # type: ignore[union-attr]
        if not reviews:
            return AgentResult.skip("No reviews found on your Google Business Profile")

        # A review is handled once: posted, turned down by a person, or a draft
        # still waiting for approval. A draft whose approval expired, or one the
        # safety check discarded a week ago, gets another attempt.
        now = utcnow()
        existing = {
            row.review_name: row
            for row in ctx.db.execute(
                select(ReviewReply).where(ReviewReply.tenant_id == ctx.tenant_id)
            ).scalars()
        }

        def settled(name: str) -> bool:
            row = existing.get(name)
            if row is None:
                return False
            if row.status in ("posted", "rejected"):
                return True
            age = now - (row.updated_at or row.created_at or now)
            if row.status == "discarded":
                return age < RETRY_DISCARDED_AFTER
            return age < DRAFT_STALE_AFTER

        waiting = [r for r in reviews if not r.reply and not settled(r.name)]
        if not waiting:
            return AgentResult(
                summary=f"checked {len(reviews)} reviews — every one already has a reply",
                metric_label="All reviews answered",
            )

        actions: list[AgentAction] = []
        discarded = 0
        per_run = max(1, min(MAX_PER_RUN, ctx.remaining_actions or 5))
        batch = waiting[:per_run]
        for index, review in enumerate(batch, start=1):
            ctx.progress(f"Writing replies ({index} of {len(batch)})", done=index - 1, total=len(batch))
            try:
                data = ctx.ask_json(
                    prompts.draft_reply(
                        business=ctx.org.name,
                        reviewer=review.reviewer,
                        stars=review.stars,
                        review=review.comment,
                    ),
                    system=prompts.SYSTEM,
                    schema_hint=prompts.SCHEMA_HINT,
                )
            except Exception as exc:  # noqa: BLE001 - one failure must not stop the rest
                ctx.log_warning("Reply draft failed for %s: %s", review.name, exc)
                continue
            reply = str((data or {}).get("reply") or "").strip()
            problem = prompts.check_reply(reply)
            row = existing.get(review.name)
            if row is None:
                row = ReviewReply(tenant_id=ctx.tenant_id, review_name=review.name)
                ctx.db.add(row)
            row.reviewer = review.reviewer[:255]
            row.stars = review.stars
            row.review_time = review.created_at
            row.review_encrypted = ctx.cipher.encrypt(review.comment, context=Ctx.REVIEW_TEXT)
            row.updated_at = now
            if problem:
                ctx.log_warning("Discarded reply draft (%s)", problem)
                # Recorded, so the same review is not redrafted every run.
                row.status = "discarded"
                row.reply_encrypted = ""
                discarded += 1
                ctx.db.flush()
                continue
            row.reply_encrypted = ctx.cipher.encrypt(reply, context=Ctx.REVIEW_REPLY)
            row.status = "drafted"
            ctx.db.flush()
            who = review.reviewer or "an anonymous reviewer"
            actions.append(
                AgentAction(
                    kind="reply_review",
                    title=f"Reply to {who}'s {review.stars or ''}★ review".replace(" ★", ""),
                    impact=Impact.HIGH,
                    approval_type="Review Reply",
                    target_kind="review_reply",
                    target_id=row.id,
                    payload={
                        "reply_id": row.id,
                        "reviewer": who,
                        "stars": review.stars,
                        "review": review.comment,
                        "reply": reply,
                    },
                    apply=_make_poster(row.id),
                    audit=f"replied to {who}'s {review.stars}-star Google review",
                )
            )

        summary = f"{len(waiting)} reviews without a reply; {len(actions)} replies written"
        if discarded:
            summary += (
                f", {discarded} drafts discarded by the safety check (tried again in "
                f"{RETRY_DISCARDED_AFTER.days} days)"
            )
        if len(waiting) > len(batch):
            summary += f"; {len(waiting) - len(batch)} more next run"
        return AgentResult(
            summary=summary,
            actions=actions,
            metric_label=f"{len(waiting)} reviews waiting for a reply",
            detail={"reviews_read": len(reviews), "unanswered": len(waiting)},
        )


def _make_poster(reply_id: str):  # noqa: ANN202
    def post(ctx: AgentContext) -> None:
        row = ctx.db.get(ReviewReply, reply_id)
        if row is None:
            raise LookupError("That reply draft no longer exists")
        if row.status == "posted":
            return
        profile = ctx.with_capability(Capability.REPLY_REVIEW)
        if profile is None:
            raise LookupError(
                "Google Business Profile is not connected, so this reply cannot be "
                "posted. Reconnect it and approve this again."
            )
        text = ctx.cipher.decrypt(row.reply_encrypted, context=Ctx.REVIEW_REPLY)
        profile.reply_to_review(row.review_name, text)  # type: ignore[union-attr]
        row.status = "posted"
        row.posted_at = utcnow()
        ctx.db.flush()

    return post


def _record(db: Session, org: Organization):  # noqa: ANN202
    from app.models.agent import AgentRecord

    record = db.execute(
        select(AgentRecord).where(AgentRecord.tenant_id == org.id, AgentRecord.slug == "review_responder")
    ).scalar_one_or_none()
    if record is None:
        raise LookupError("Review Responder is not installed in this workspace")
    return record


@register_applier("review_reply")
def apply_reply(db: Session, org: Organization, item: ApprovalItem, payload: dict) -> None:
    from app.orchestration.runner import build_context

    ctx = build_context(db, _record(db, org), org, trigger="approval")
    _make_poster(payload.get("reply_id") or item.target_id or "")(ctx)


@register_rejecter("review_reply")
def discard_reply(db: Session, org: Organization, item: ApprovalItem, payload: dict) -> None:
    """A rejected reply is kept as rejected, so the same review is not redrafted."""
    row = db.get(ReviewReply, payload.get("reply_id") or item.target_id or "")
    if row is not None and row.status != "posted":
        row.status = "rejected"
        db.flush()


AGENT = ReviewResponderAgent()
