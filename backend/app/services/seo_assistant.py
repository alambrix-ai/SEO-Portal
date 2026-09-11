"""SEO Assistant — guides operators through the platform from a use case.

Uses the **process-wide LLM from the server ``.env``**
(``ANTHROPIC_API_KEY``, ``ASSISTANT_MODEL`` / ``LLM_MODEL``, ``LLM_MAX_TOKENS``,
``LLM_EFFORT``). It does **not** use workspace AI-model connectors — those are
only for agents the customer configures.

Ask mode advises only. Action mode returns a structured plan the console walks
through with connect and configure forms.
"""
from __future__ import annotations

import json
import re
from typing import Any, Literal

from sqlalchemy.orm import Session

from app.agents.base import all_agents
from app.connectors.base import all_specs
from app.core.config import settings
from app.core.exceptions import InvalidInputError
from app.core.logging import get_logger
from app.llm.base import LLMError
from app.llm.providers import AnthropicProvider
from app.services import connectors as connector_service
from app.services import portal_features
from app.services.assistant_errors import AssistantConfigError, AssistantUpstreamError

log = get_logger(__name__)

AssistantMode = Literal["ask", "action"]


def _catalogue(db: Session, tenant_id: Any) -> dict[str, Any]:
    """Agents and connectors the assistant may recommend, with live status."""
    enabled_agents = portal_features.enabled_agent_slugs(db)
    enabled_connectors = portal_features.enabled_connector_slugs(db)
    connected = {
        row.slug
        for row in connector_service.list_records(db, tenant_id=tenant_id)
        if row.connected
    }

    agents: list[dict[str, Any]] = []
    for agent in all_agents():
        if agent.spec.slug not in enabled_agents:
            continue
        agents.append(
            {
                "slug": agent.spec.slug,
                "name": agent.spec.name,
                "category": agent.spec.category.value,
                "description": agent.spec.description,
                "requires_llm": agent.spec.requires_llm,
                "capabilities": [c.value for c in agent.spec.required_capabilities],
                "any_of_capabilities": [c.value for c in agent.spec.any_of_capabilities],
                "scope_placeholder": agent.spec.scope_placeholder,
                "default_schedule": agent.spec.default_schedule,
            }
        )

    connectors: list[dict[str, Any]] = []
    for spec in all_specs():
        if spec.slug not in enabled_connectors:
            continue
        fields = []
        for field in spec.fields:
            fields.append(
                {
                    "key": field.key,
                    "label": field.label,
                    "placeholder": field.placeholder or "",
                    "help": field.help_text or "",
                    "type": field.kind.value,
                    "is_oauth": field.is_oauth,
                    "default": field.default or "",
                    "required": bool(field.required),
                    "secret": bool(field.is_secret),
                }
            )
        connectors.append(
            {
                "slug": spec.slug,
                "name": spec.name,
                "category": spec.category,
                "description": spec.description,
                "connected": spec.slug in connected,
                "fields": fields,
                "capabilities": [c.value for c in spec.capabilities],
            }
        )

    return {"agents": agents, "connectors": connectors}


def _system_prompt(catalogue: dict[str, Any], mode: AssistantMode) -> str:
    mode_rules = (
        "MODE: ask — Advise only. Explain which agents and connectors fit, "
        "why, and how the operator should connect them in the console. Do not "
        "claim you will connect anything yourself. action_plan must be null."
        if mode == "ask"
        else "MODE: action — After analysing the use case, return an action_plan "
        "the console will execute step by step. Only include connectors/agents "
        "from the catalogue. Prefer connectors that are already connected when "
        "they cover the need. Order steps: connectors first, then agent configs, "
        "then optional resume."
    )
    return f"""You are Willy, the AutoMarket AI assistant inside THIS customer console.

Scope (hard guardrail):
- You ONLY help with this portal: SEO, AEO, technical SEO, off-page/PR, ads,
  connectors, AI agents, onboarding, approvals, and workspace setup.
- Refuse anything outside that scope: maths, general trivia, coding homework,
  news, personal advice, other products, or role-play unrelated to AutoMarket.
- When refusing, say briefly that you only help with this portal, give 1–2
  example questions that ARE in scope, and return empty recommendations and
  null action_plan. Do not answer the off-topic question itself.

Your job for in-scope requests: analyse the operator's marketing/SEO use case,
then guide them on THIS platform — which connectors to connect and which AI
agents to configure — using only the catalogue below.

Memory (LlamaIndex chat engine):
- Prior turns are in conversation memory. Treat follow-ups as the same thread
  ("that agent", "yes", "connect it", pronouns, shortened asks).
- Do not re-ask for facts the operator already gave unless they conflict.
- If this turn is a clarification of the previous use case, refine
  recommendations / action_plan accordingly instead of starting over.

{mode_rules}

Catalogue (JSON):
{json.dumps(catalogue, indent=2)}

Respond with a single JSON object (no markdown fences around the whole object)
shaped as:
{{
  "reply": "Helpful markdown for the chat bubble (short paragraphs, **bold**, lists).",
  "recommendations": {{
    "connectors": [{{"slug": "...", "name": "...", "reason": "..."}}],
    "agents": [{{"slug": "...", "name": "...", "reason": "...", "depends_on": ["connector_slug"]}}]
  }},
  "action_plan": null or {{
    "summary": "One line",
    "steps": [
      {{
        "id": "connect-wordpress",
        "type": "connect_connector",
        "slug": "wordpress",
        "title": "Connect WordPress",
        "reason": "...",
        "fields": [{{"key": "siteUrl", "label": "Site URL", "secret": false, "required": true}}]
      }},
      {{
        "id": "configure-on-page",
        "type": "configure_agent",
        "slug": "on_page_seo_sync",
        "title": "Configure On-Page SEO Sync",
        "reason": "...",
        "config": {{
          "schedule": "Daily",
          "scope": "/",
          "notify_channel": "None",
          "llm_connector": "anthropic_claude",
          "max_actions_per_day": 20
        }}
      }},
      {{
        "id": "resume-on-page",
        "type": "resume_agent",
        "slug": "on_page_seo_sync",
        "title": "Start On-Page SEO Sync",
        "reason": "..."
      }}
    ]
  }}
}}

Rules:
- Only recommend slugs that exist in the catalogue.
- If a needed connector is already connected, say so and skip a connect step.
- For LLM-writing agents, llm_connector must be a connected AI model connector
  slug from the catalogue (openai, anthropic_claude, google_gemini, perplexity).
- Keep reply under ~350 words unless the operator asks for more depth.
- If the use case is unclear but still about this portal, ask 1–3 focused
  clarifying questions and return empty recommendations / null action_plan.
"""


_OFF_TOPIC_REPLY = (
    "I only help with **this AutoMarket portal** — SEO, AEO, connectors, and "
    "agents.\n\n"
    "Try something like:\n"
    "- *How do I improve on-page SEO for our product pages?*\n"
    "- *Which connectors do I need for technical SEO audits?*\n"
    "- *Help me set up agents for backlinks and AEO.*"
)


def _looks_off_topic(message: str) -> bool:
    """Cheap pre-filter so obvious off-topic prompts never hit the LLM."""
    text = (message or "").strip()
    if not text:
        return False
    lowered = text.lower()

    # Pure arithmetic / equations (e.g. "2+2", "what is 5*7").
    compact = re.sub(r"\s+", "", lowered)
    if re.fullmatch(r"(whatis|whats|calculate|solve)?[\d\+\-\*/×÷\(\)\.=x]+", compact):
        return True
    if re.fullmatch(r"[\d\s\+\-\*/×÷\(\)\.=x\?]+", text) and any(ch.isdigit() for ch in text):
        return True

    portal_tokens = (
        "seo",
        "aeo",
        "agent",
        "connector",
        "wordpress",
        "shopify",
        "backlink",
        "audit",
        "ranking",
        "keyword",
        "content",
        "ads",
        "campaign",
        "onboarding",
        "workspace",
        "portal",
        "automarket",
        "willy",
        "site",
        "page",
        "crawl",
        "sitemap",
        "gsc",
        "analytics",
        "pr ",
        "outreach",
        "technical",
        "organic",
        "traffic",
        "conversion",
        "schema",
        "citation",
    )
    if any(token in lowered for token in portal_tokens):
        return False

    off_starters = (
        "tell me a joke",
        "who is",
        "what is the capital",
        "write a poem",
        "write code",
        "translate ",
        "weather",
        "stock price",
    )
    if any(lowered.startswith(s) or s in lowered for s in off_starters):
        return True

    # Short messages with digits and operators, no portal vocabulary.
    if len(text) <= 32 and re.search(r"\d", text) and re.search(r"[\+\-\*/×÷=]", text):
        return True

    return False


def _refusal_payload(mode: AssistantMode) -> dict[str, Any]:
    return {
        "reply": _OFF_TOPIC_REPLY,
        "mode": mode,
        "recommendations": {"connectors": [], "agents": []},
        "action_plan": None,
        "model": _env_model() or "guardrail",
    }


def _extract_json(text: str) -> dict[str, Any]:
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{[\s\S]*\}", raw)
    if match:
        data = json.loads(match.group(0))
        if isinstance(data, dict):
            return data
    raise AssistantUpstreamError(
        "The assistant returned an unreadable reply. Please try again.",
    )


def _env_model() -> str:
    return (settings.assistant_model or settings.llm_model or "").strip()


def _provider() -> AnthropicProvider:
    """Build Claude from server ``.env`` only — never from a workspace connector."""
    key = (settings.anthropic_api_key or "").strip()
    model = _env_model()
    if not key:
        raise AssistantConfigError(
            "SEO Assistant needs ANTHROPIC_API_KEY in the server .env "
            "(not a workspace Anthropic connector).",
        )
    if not model:
        raise AssistantConfigError(
            "SEO Assistant needs ASSISTANT_MODEL or LLM_MODEL in the server .env "
            "(for example claude-sonnet-4-5).",
        )
    return AnthropicProvider(
        api_key=key,
        model=model,
        base_url=settings.anthropic_base_url,
        default_max_tokens=int(settings.llm_max_tokens or 16000),
        effort=(settings.llm_effort or "medium"),
    )


def chat(
    db: Session,
    *,
    tenant_id: Any,
    mode: AssistantMode,
    message: str,
    history: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    """Run one assistant turn and return structured guidance."""
    text = (message or "").strip()
    if not text:
        raise InvalidInputError("Describe your use case so the assistant can help.")

    if _looks_off_topic(text):
        log.info("Willy guardrail refused off-topic prompt")
        return _refusal_payload(mode)

    catalogue = _catalogue(db, tenant_id)
    provider = _provider()

    try:
        from app.llm.willy_chat_engine import run_chat_engine

        result = run_chat_engine(
            provider=provider,
            system_prompt=_system_prompt(catalogue, mode),
            message=text,
            history=history,
        )
    except AssistantConfigError:
        raise
    except LLMError as exc:
        log.exception("SEO Assistant env-LLM call failed")
        raise AssistantUpstreamError(str(exc) or (
            "The assistant could not reach the LLM configured in the server .env. "
            "Check ANTHROPIC_API_KEY and ASSISTANT_MODEL (or LLM_MODEL)."
        )) from exc
    except Exception as exc:  # noqa: BLE001 — surface as operator-facing error
        log.exception("SEO Assistant LlamaIndex chat engine failed")
        raise AssistantUpstreamError(
            "The assistant could not complete this turn. "
            "Check ANTHROPIC_API_KEY and ASSISTANT_MODEL (or LLM_MODEL).",
        ) from exc

    parsed = _extract_json(result.text)
    reply = str(parsed.get("reply") or "").strip() or (
        "I looked at your use case — open the recommendations below."
    )
    recommendations = parsed.get("recommendations") or {"connectors": [], "agents": []}
    action_plan = parsed.get("action_plan") if mode == "action" else None
    if mode == "ask":
        action_plan = None

    # Drop unknown slugs so the UI never offers a dead link.
    known_c = {c["slug"] for c in catalogue["connectors"]}
    known_a = {a["slug"] for a in catalogue["agents"]}
    rec_connectors = [
        row
        for row in (recommendations.get("connectors") or [])
        if isinstance(row, dict) and row.get("slug") in known_c
    ]
    rec_agents = [
        row
        for row in (recommendations.get("agents") or [])
        if isinstance(row, dict) and row.get("slug") in known_a
    ]

    if isinstance(action_plan, dict):
        steps = []
        for step in action_plan.get("steps") or []:
            if not isinstance(step, dict):
                continue
            step_type = step.get("type")
            slug = step.get("slug")
            if step_type == "connect_connector" and slug in known_c:
                catalog_fields = next(
                    (c["fields"] for c in catalogue["connectors"] if c["slug"] == slug),
                    [],
                )
                already = next(
                    (c["connected"] for c in catalogue["connectors"] if c["slug"] == slug),
                    False,
                )
                if already:
                    continue
                steps.append(
                    {
                        "id": step.get("id") or f"connect-{slug}",
                        "type": "connect_connector",
                        "slug": slug,
                        "title": step.get("title") or f"Connect {slug}",
                        "reason": step.get("reason") or "",
                        "fields": catalog_fields or step.get("fields") or [],
                    }
                )
            elif step_type in {"configure_agent", "resume_agent"} and slug in known_a:
                steps.append(
                    {
                        "id": step.get("id") or f"{step_type}-{slug}",
                        "type": step_type,
                        "slug": slug,
                        "title": step.get("title") or step_type.replace("_", " ").title(),
                        "reason": step.get("reason") or "",
                        "config": step.get("config") if step_type == "configure_agent" else None,
                    }
                )
        action_plan = {
            "summary": action_plan.get("summary") or "",
            "steps": steps,
        }
        if not steps:
            action_plan = None

    return {
        "reply": reply,
        "mode": mode,
        "recommendations": {"connectors": rec_connectors, "agents": rec_agents},
        "action_plan": action_plan,
        "model": result.model or _env_model(),
    }
