"""Shared response shapes.

Pydantic models rather than raw dicts so the OpenAPI schema the frontend is
typed against stays accurate.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field

T = TypeVar("T")


class ApiModel(BaseModel):
    """Base for every response model."""

    model_config = ConfigDict(from_attributes=True, populate_by_name=True)


class Message(ApiModel):
    detail: str


class ErrorResponse(ApiModel):
    code: str
    detail: str
    fields: dict[str, Any] = Field(default_factory=dict)


class Page(ApiModel, Generic[T]):
    items: list[T]
    total: int
    limit: int = 50
    offset: int = 0


class Toast(ApiModel):
    """A message the console shows after an action.

    Returned by mutating endpoints so the wording lives with the rule that
    produced it rather than being duplicated in the frontend.
    """

    message: str
    kind: str = "info"  # info | success | warning | error


class ActionResult(ApiModel):
    ok: bool = True
    toast: Toast | None = None


class HealthStatus(ApiModel):
    status: str
    app: str
    environment: str
    version: str
    database: str
    agents_registered: int
    connectors_registered: int
    model: str
    scheduler: str
    checked_at: datetime
