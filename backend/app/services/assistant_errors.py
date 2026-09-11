"""SEO Assistant domain errors."""
from __future__ import annotations

from app.core.exceptions import AppError


class AssistantConfigError(AppError):
    status_code = 503
    code = "assistant_not_configured"


class AssistantUpstreamError(AppError):
    status_code = 502
    code = "assistant_upstream"
