"""Creative Studio: writes and designs one job's creatives, for review.

An on-demand agent. It never runs on a schedule and is not installed in a
workspace: a Creative Studio job starts it through
``runner.run_on_demand`` with the job's id on ``ctx.job`` and the model the
person chose as a strict ``LLMOverride`` (checked against the allow-list
before the run, see ``services.creative.model_choice``).

The work itself lives in :mod:`app.services.creative.pipeline`: copy written
with untrusted input fenced and the reply validated, a background, rendering,
QA, and one marketing asset plus one pending approval per format. Nothing it
makes is published or applied by the run; every creative waits for a person.
"""
from __future__ import annotations

from datetime import timedelta

from app.agents.base.context import AgentContext
from app.agents.base.contracts import AgentCategory, AgentResult, AgentSpec, BaseAgent, Impact

SLUG = "creative_studio"
NAME = "Creative Studio"


class CreativeStudioAgent(BaseAgent):
    spec = AgentSpec(
        slug=SLUG,
        name=NAME,
        category=AgentCategory.CONTENT,
        description=(
            "Writes and designs on-brand banners, social images, email headers, "
            "flyers and brochures from your brand kit and products, for you to review."
        ),
        default_interval=timedelta(days=1),
        requires_llm=True,
        max_impact=Impact.HIGH,
        on_demand=True,
    )

    def run(self, ctx: AgentContext) -> AgentResult:
        from app.services.creative import pipeline

        return pipeline.generate(ctx)


AGENT = CreativeStudioAgent()
