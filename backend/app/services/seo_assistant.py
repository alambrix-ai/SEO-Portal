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
    return f"""You are the AutoMarket AI SEO Assistant inside the customer console.

Your job: deeply analyse the operator's marketing/SEO use case, then guide them
on how to use THIS platform — which connectors to connect and which AI agents
to configure — using only the catalogue below.

{mode_rules}

Catalogue (JSON):
{json.dumps(catalogue, indent=2)}

Respond with a single JSON object (no markdown fences) shaped as:
{{
  "reply": "Clear, helpful markdown for the chat bubble. Be concrete.",
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
- If the use case is unclear, ask 1–3 focused clarifying questions in reply
  and return empty recommendations / null action_plan.
"""


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

    catalogue = _catalogue(db, tenant_id)
    provider = _provider()

    transcript: list[str] = []
    for turn in (history or [])[-8:]:
        role = (turn.get("role") or "").strip().lower()
        content = (turn.get("content") or "").strip()
        if role in {"user", "assistant"} and content:
            transcript.append(f"{role.upper()}: {content}")
    transcript.append(f"USER: {text}")
    prompt = (
        "Conversation so far:\n"
        + "\n\n".join(transcript)
        + "\n\nReply with the JSON object described in the system instructions."
    )

    try:
        result = provider.complete(prompt, system=_system_prompt(catalogue, mode))
    except AssistantConfigError:
        raise
    except LLMError as exc:
        log.exception("SEO Assistant env-LLM call failed")
        raise AssistantUpstreamError(str(exc) or (
            "The assistant could not reach the LLM configured in the server .env. "
            "Check ANTHROPIC_API_KEY and ASSISTANT_MODEL (or LLM_MODEL)."
        )) from exc
    except Exception as exc:  # noqa: BLE001 — surface as operator-facing error
        log.exception("SEO Assistant env-LLM call failed")
        raise AssistantUpstreamError(
            "The assistant could not reach the LLM configured in the server .env. "
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
