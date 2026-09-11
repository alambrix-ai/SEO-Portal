"""LlamaIndex chat engine for Willy — token-budgeted conversation memory.

Rebuilds a ``ChatMemoryBuffer`` from the client-supplied history each turn,
then runs ``SimpleChatEngine`` so follow-ups keep prior use-case context
within the token window (not a fixed last-N dump).
"""
from __future__ import annotations

from typing import Any, Sequence

from llama_index.core.base.llms.types import ChatMessage, MessageRole
from llama_index.core.chat_engine import SimpleChatEngine
from llama_index.core.llms import (
    CustomLLM,
    LLMMetadata,
    CompletionResponse,
    CompletionResponseGen,
    ChatResponse,
)
from llama_index.core.llms.callbacks import llm_chat_callback, llm_completion_callback
from llama_index.core.memory import ChatMemoryBuffer
from pydantic import Field, PrivateAttr

from app.llm.base import LLMResponse
from app.llm.providers import AnthropicProvider

# Keep enough prior turns for follow-ups without blowing Claude's context.
_DEFAULT_TOKEN_LIMIT = 24_000
_HISTORY_TURN_CAP = 40


class WillyAnthropicLLM(CustomLLM):
    """LlamaIndex LLM adapter over our Anthropic HTTP provider."""

    context_window: int = Field(default=180_000)
    num_output: int = Field(default=16_000)
    model_name: str = Field(default="claude")

    _provider: AnthropicProvider = PrivateAttr()
    _last_model: str = PrivateAttr(default="")

    def __init__(self, provider: AnthropicProvider, **kwargs: Any) -> None:
        super().__init__(model_name=provider.model, **kwargs)
        self._provider = provider
        self._last_model = provider.model

    @property
    def metadata(self) -> LLMMetadata:
        return LLMMetadata(
            context_window=self.context_window,
            num_output=self.num_output,
            model_name=self.model_name,
            is_chat_model=True,
        )

    @llm_completion_callback()
    def complete(self, prompt: str, formatted: bool = False, **kwargs: Any) -> CompletionResponse:
        _ = formatted
        result = self._provider.complete(prompt, system="")
        self._last_model = result.model or self.model_name
        return CompletionResponse(text=result.text)

    @llm_completion_callback()
    def stream_complete(
        self, prompt: str, formatted: bool = False, **kwargs: Any
    ) -> CompletionResponseGen:
        response = self.complete(prompt, formatted=formatted, **kwargs)
        yield response

    @llm_chat_callback()
    def chat(self, messages: Sequence[ChatMessage], **kwargs: Any) -> ChatResponse:
        _ = kwargs
        system_parts: list[str] = []
        api_messages: list[dict[str, str]] = []

        for msg in messages:
            content = (msg.content or "").strip()
            if not content:
                continue
            if msg.role == MessageRole.SYSTEM:
                system_parts.append(content)
                continue
            role = "assistant" if msg.role == MessageRole.ASSISTANT else "user"
            # Anthropic rejects consecutive same-role turns — merge if needed.
            if api_messages and api_messages[-1]["role"] == role:
                api_messages[-1]["content"] += "\n\n" + content
            else:
                api_messages.append({"role": role, "content": content})

        if not api_messages:
            api_messages = [{"role": "user", "content": "Continue."}]
        elif api_messages[0]["role"] != "user":
            api_messages.insert(0, {"role": "user", "content": "(continued)"})
        if api_messages[-1]["role"] != "user":
            api_messages.append({"role": "user", "content": "Continue with the JSON reply."})

        result = self._provider.complete_messages(
            api_messages,
            system="\n\n".join(system_parts).strip(),
        )
        self._last_model = result.model or self.model_name
        return ChatResponse(
            message=ChatMessage(role=MessageRole.ASSISTANT, content=result.text),
            raw={"model": self._last_model},
        )

    @property
    def last_model(self) -> str:
        return self._last_model


def _history_messages(history: list[dict[str, str]] | None) -> list[ChatMessage]:
    messages: list[ChatMessage] = []
    for turn in (history or [])[-_HISTORY_TURN_CAP:]:
        role = (turn.get("role") or "").strip().lower()
        content = (turn.get("content") or "").strip()
        if not content:
            continue
        if role == "assistant":
            messages.append(ChatMessage(role=MessageRole.ASSISTANT, content=content))
        elif role == "user":
            messages.append(ChatMessage(role=MessageRole.USER, content=content))
    return messages


def run_chat_engine(
    *,
    provider: AnthropicProvider,
    system_prompt: str,
    message: str,
    history: list[dict[str, str]] | None = None,
    token_limit: int = _DEFAULT_TOKEN_LIMIT,
) -> LLMResponse:
    """One Willy turn with LlamaIndex memory + SimpleChatEngine."""
    llm = WillyAnthropicLLM(provider=provider)
    memory = ChatMemoryBuffer.from_defaults(
        token_limit=token_limit,
        chat_history=_history_messages(history),
        llm=llm,
    )
    engine = SimpleChatEngine.from_defaults(
        llm=llm,
        memory=memory,
        system_prompt=system_prompt,
    )
    user_turn = (
        f"{message.strip()}\n\n"
        "Reply with the JSON object described in the system instructions. "
        "Use prior turns in memory for pronouns and follow-ups."
    )
    response = engine.chat(user_turn)
    text = str(getattr(response, "response", None) or response)
    return LLMResponse(text=text, model=llm.last_model or provider.model)
