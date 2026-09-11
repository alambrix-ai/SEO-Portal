"""SEO Assistant HTTP API."""
from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any, Literal

from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from pydantic import Field

from app.api.deps import CurrentUserDep, DbSession
from app.core.exceptions import ForbiddenError
from app.core.rbac import Module
from app.schemas.common import ApiModel
from app.services import seo_assistant

router = APIRouter(prefix="/assistant", tags=["assistant"])


class ChatTurn(ApiModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=8000)


class AssistantChatRequest(ApiModel):
    message: str = Field(min_length=1, max_length=8000)
    mode: Literal["ask", "action"] = "ask"
    history: list[ChatTurn] = Field(default_factory=list, max_length=40)


class RecommendedItem(ApiModel):
    slug: str
    name: str = ""
    reason: str = ""
    depends_on: list[str] = Field(default_factory=list)


class RecommendationsOut(ApiModel):
    connectors: list[RecommendedItem] = Field(default_factory=list)
    agents: list[RecommendedItem] = Field(default_factory=list)


class ActionFieldOut(ApiModel):
    key: str
    label: str = ""
    secret: bool = False
    required: bool = True
    placeholder: str = ""
    help: str = ""
    type: str = "text"
    is_oauth: bool = False


class ActionStepOut(ApiModel):
    id: str
    type: Literal["connect_connector", "configure_agent", "resume_agent"]
    slug: str
    title: str = ""
    reason: str = ""
    fields: list[ActionFieldOut] = Field(default_factory=list)
    config: dict | None = None


class ActionPlanOut(ApiModel):
    summary: str = ""
    steps: list[ActionStepOut] = Field(default_factory=list)


class AssistantChatResponse(ApiModel):
    reply: str
    mode: Literal["ask", "action"]
    recommendations: RecommendationsOut
    action_plan: ActionPlanOut | None = None
    model: str = ""


def _require_assistant_access(current: CurrentUserDep) -> None:
    if not current.module_enabled(Module.ONBOARDING):
        raise ForbiddenError(
            "The SEO Assistant is not enabled for this workspace. "
            "Ask a platform administrator to enable Onboarding, or join a "
            "workspace where it is available."
        )
    if not current.can_view(Module.ONBOARDING):
        raise ForbiddenError(
            "You do not have access to the SEO Assistant. "
            "Ask your workspace admin to grant Onboarding access for your role."
        )


@router.post("/chat", response_model=AssistantChatResponse)
def chat(body: AssistantChatRequest, current: CurrentUserDep, db: DbSession) -> AssistantChatResponse:
    """Analyse a use case and recommend (or plan) connectors and agents."""
    _require_assistant_access(current)
    result = seo_assistant.chat(
        db,
        tenant_id=current.tenant_id,
        mode=body.mode,
        message=body.message,
        history=[turn.model_dump() for turn in body.history],
    )
    return AssistantChatResponse(**result)


@router.post("/chat/stream")
def chat_stream(
    body: AssistantChatRequest, current: CurrentUserDep, db: DbSession
) -> StreamingResponse:
    """Stream Willy's markdown reply as SSE, then a final structured payload."""
    _require_assistant_access(current)

    def generate() -> Iterator[bytes]:
        for event in seo_assistant.chat_stream(
            db,
            tenant_id=current.tenant_id,
            mode=body.mode,
            message=body.message,
            history=[turn.model_dump() for turn in body.history],
        ):
            name = str(event.get("event") or "message")
            payload: dict[str, Any]
            if name == "final":
                payload = event.get("data") or {}
                data = json.dumps(payload, default=str)
            elif name in {"delta", "status"}:
                data = json.dumps({"text": event.get("text") or ""})
            else:
                data = json.dumps({"detail": event.get("detail") or "error"})
            yield f"event: {name}\ndata: {data}\n\n".encode("utf-8")

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
