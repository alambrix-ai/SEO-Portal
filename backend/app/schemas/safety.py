"""Shapes for spending-agent safety: practice mode, limits and stop switches."""
from __future__ import annotations

from pydantic import Field

from app.schemas.common import ApiModel, Toast


class PracticeRequest(ApiModel):
    on: bool


class SwitchRequest(ApiModel):
    on: bool


class PracticeSummaryOut(ApiModel):
    """"In practice mode this agent would have made N changes this week …"."""

    days: int = 7
    changes: int = 0
    automatic: int = 0
    needs_approval: int = 0
    #: Daily budget the practised budget changes would have moved, summed.
    budget_moved: float | None = None
    budget_moved_label: str = ""
    sentence: str = ""


class CapFieldOut(ApiModel):
    key: str
    label: str
    help: str = ""


class SafetyStatusOut(ApiModel):
    """The workspace's safety settings, and the platform switches in force."""

    #: Workspace-wide "Practice mode for all spending agents".
    practice_mode: bool = False
    #: "Stop all agents" is on.
    stopped: bool = False
    stopped_at: str = ""
    stopped_by: str = ""
    #: Platform-wide emergency stop (Portal Admin).
    platform_stop: bool = False
    #: Platform-wide practice mode for spending agents (Portal Admin).
    platform_practice: bool = False
    caps: dict[str, float] = Field(default_factory=dict)
    default_caps: dict[str, float] = Field(default_factory=dict)
    cap_fields: list[CapFieldOut] = Field(default_factory=list)
    spending_agents: list[str] = Field(default_factory=list)
    currency_symbol: str = ""
    #: The caller may change these (workspace admins).
    can_manage: bool = False


class CapsRequest(ApiModel):
    caps: dict[str, float]


class SafetyActionOut(ApiModel):
    status: SafetyStatusOut
    toast: Toast | None = None


class PlatformSwitchOut(ApiModel):
    on: bool = False
    by: str = ""
    at: str = ""


class PlatformSafetyOut(ApiModel):
    emergency_stop: PlatformSwitchOut = Field(default_factory=PlatformSwitchOut)
    practice_mode: PlatformSwitchOut = Field(default_factory=PlatformSwitchOut)


class PlatformSafetyActionOut(ApiModel):
    status: PlatformSafetyOut
    toast: Toast | None = None
