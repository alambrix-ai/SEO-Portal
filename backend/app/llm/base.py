"""The LLM contract every agent talks to.

Agents never import a vendor SDK. They ask this interface for structured
output and get a validated dict back, which keeps the whole fleet swappable
between the deterministic mock (tests, demos, offline) and a real provider.
"""
from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class LLMUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cost: float = 0.0

    def __add__(self, other: LLMUsage) -> LLMUsage:
        return LLMUsage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cost=round(self.cost + other.cost, 6),
        )


@dataclass(slots=True)
class LLMResponse:
    text: str
    usage: LLMUsage = field(default_factory=LLMUsage)
    model: str = ""
    stop_reason: str = ""

    def json(self) -> Any:
        """Parse the response as JSON, tolerating a fenced code block."""
        return extract_json(self.text)


class LLMError(Exception):
    """Raised when a provider fails in a way the caller should handle."""


def extract_json(text: str) -> Any:
    """Pull the first JSON value out of a model response.

    Models wrap JSON in prose or fences often enough that the parsing belongs
    here rather than in eleven agents.
    """
    if not text:
        raise LLMError("Empty response")

    fenced = re.search(r"```(?:json)?\s*(.+?)\s*```", text, re.DOTALL)
    candidate = fenced.group(1) if fenced else text

    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        pass

    # Fall back to the outermost brace/bracket pair.
    for opener, closer in (("{", "}"), ("[", "]")):
        start, end = candidate.find(opener), candidate.rfind(closer)
        if start != -1 and end > start:
            try:
                return json.loads(candidate[start : end + 1])
            except json.JSONDecodeError:
                continue
    raise LLMError(f"Response was not JSON: {text[:200]}")


class LLMProvider(ABC):
    """Minimal surface: a completion, and a structured completion."""

    name: str = "provider"

    @abstractmethod
    def complete(
        self,
        prompt: str,
        *,
        system: str = "",
        max_tokens: int | None = None,
        temperature: float = 0.2,
    ) -> LLMResponse:
        """Return a single completion for one prompt."""

    def complete_json(
        self,
        prompt: str,
        *,
        system: str = "",
        schema_hint: str = "",
        max_tokens: int | None = None,
        temperature: float = 0.2,
    ) -> tuple[Any, LLMUsage]:
        """Return ``(parsed_json, usage)``.

        ``schema_hint`` is appended to the prompt so the model is told the exact
        shape expected; the parse is still defensive.
        """
        instruction = prompt
        if schema_hint:
            instruction = (
                f"{prompt}\n\nRespond with JSON only, matching this shape:\n{schema_hint}"
            )
        response = self.complete(
            instruction, system=system, max_tokens=max_tokens, temperature=temperature
        )
        return response.json(), response.usage

    def health(self) -> bool:
        """Cheap reachability probe. Overridden where a provider can be down."""
        return True
