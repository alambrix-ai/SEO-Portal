"""Role-based access control.

The matrix below is the single source of truth for what each role may see and
do. Both the API guards and the ``/auth/me`` payload the frontend renders its
navigation from read this one table, so UI and enforcement cannot drift.
"""
from __future__ import annotations

from enum import StrEnum


class Role(StrEnum):
    ADMIN = "admin"
    MANAGER = "manager"
    SEO = "seo"
    ADS = "ads"
    APPROVER = "approver"
    CLIENT = "client"


class Access(StrEnum):
    FULL = "full"
    VIEW = "view"
    NONE = "none"


class Module(StrEnum):
    DASHBOARD = "dashboard"
    ONBOARDING = "onboarding"
    AGENTS = "agents"
    SEO = "seo"
    OFFPAGE = "offpage"
    ADS = "ads"
    CONNECTORS = "connectors"
    APPROVALS = "approvals"
    REPORTS = "reports"
    ADMIN = "admin"
    #: Articles, campaigns, social posts and emails the content agents write.
    CONTENT = "content"
    #: Brand kits (logos, colours, fonts) and products the Creative Studio uses.
    BRAND_KIT = "brand_kit"


ROLE_LABELS: dict[Role, str] = {
    Role.ADMIN: "Super Admin",
    Role.MANAGER: "Marketing Manager",
    Role.SEO: "SEO/AEO Specialist",
    Role.ADS: "Ads Specialist",
    Role.APPROVER: "Content Approver",
    Role.CLIENT: "Client Viewer",
}

# Short labels for the login screen's "Demo as" role chips.
ROLE_SHORT_LABELS: dict[Role, str] = {
    Role.ADMIN: "Super Admin",
    Role.MANAGER: "Manager",
    Role.SEO: "SEO Specialist",
    Role.ADS: "Ads Specialist",
    Role.APPROVER: "Approver",
    Role.CLIENT: "Client Viewer",
}

F, V, N = Access.FULL, Access.VIEW, Access.NONE

ROLE_ACCESS: dict[Role, dict[Module, Access]] = {
    Role.ADMIN: {
        Module.DASHBOARD: F, Module.ONBOARDING: F, Module.AGENTS: F, Module.SEO: F,
        Module.OFFPAGE: F, Module.ADS: F, Module.CONNECTORS: F, Module.APPROVALS: F,
        Module.REPORTS: F, Module.ADMIN: F, Module.CONTENT: F, Module.BRAND_KIT: F,
    },
    Role.MANAGER: {
        Module.DASHBOARD: F, Module.ONBOARDING: F, Module.AGENTS: F, Module.SEO: F,
        Module.OFFPAGE: F, Module.ADS: F, Module.CONNECTORS: V, Module.APPROVALS: F,
        Module.REPORTS: F, Module.ADMIN: V, Module.CONTENT: F, Module.BRAND_KIT: F,
    },
    Role.SEO: {
        Module.DASHBOARD: F, Module.ONBOARDING: N, Module.AGENTS: V, Module.SEO: F,
        Module.OFFPAGE: F, Module.ADS: N, Module.CONNECTORS: V, Module.APPROVALS: V,
        Module.REPORTS: V, Module.ADMIN: N, Module.CONTENT: F, Module.BRAND_KIT: V,
    },
    Role.ADS: {
        Module.DASHBOARD: F, Module.ONBOARDING: N, Module.AGENTS: V, Module.SEO: N,
        Module.OFFPAGE: N, Module.ADS: F, Module.CONNECTORS: V, Module.APPROVALS: V,
        Module.REPORTS: V, Module.ADMIN: N, Module.CONTENT: F, Module.BRAND_KIT: V,
    },
    Role.APPROVER: {
        Module.DASHBOARD: F, Module.ONBOARDING: N, Module.AGENTS: V, Module.SEO: V,
        Module.OFFPAGE: V, Module.ADS: V, Module.CONNECTORS: V, Module.APPROVALS: F,
        Module.REPORTS: V, Module.ADMIN: N, Module.CONTENT: V, Module.BRAND_KIT: V,
    },
    Role.CLIENT: {
        Module.DASHBOARD: V, Module.ONBOARDING: N, Module.AGENTS: N, Module.SEO: N,
        Module.OFFPAGE: N, Module.ADS: N, Module.CONNECTORS: N, Module.APPROVALS: N,
        Module.REPORTS: V, Module.ADMIN: N, Module.CONTENT: N, Module.BRAND_KIT: N,
    },
}

# Modules an agent category writes into — used to decide whether a role may
# start/stop or reconfigure a given agent.
AGENT_CATEGORY_MODULE: dict[str, Module] = {
    "SEO & AEO": Module.SEO,
    "Off-Page": Module.OFFPAGE,
    "Ads": Module.ADS,
    "Local business": Module.SEO,
    "Content & Social": Module.CONTENT,
}


_ACCESS_RANK = {Access.NONE: 0, Access.VIEW: 1, Access.FULL: 2}


def access_for(role: Role | str, module: Module | str) -> Access:
    return ROLE_ACCESS[Role(role)][Module(module)]


def can_view(role: Role | str, module: Module | str) -> bool:
    return access_for(role, module) in (Access.FULL, Access.VIEW)


def can_write(role: Role | str, module: Module | str) -> bool:
    return access_for(role, module) is Access.FULL


def access_map(role: Role | str) -> dict[str, str]:
    """Serialisable access map for the ``/auth/me`` payload."""
    return {m.value: a.value for m, a in ROLE_ACCESS[Role(role)].items()}


def validate_permission_map(permissions: dict) -> dict[str, str]:
    """Normalise a custom-role permission map; reject unknown modules/levels."""
    if not isinstance(permissions, dict):
        raise ValueError("permissions must be an object")
    out: dict[str, str] = {}
    for key, value in permissions.items():
        module = Module(str(key))
        access = Access(str(value).lower())
        out[module.value] = access.value
    return out


def merge_access(
    base_role: Role | str,
    overlay: dict[str, str] | None,
) -> dict[Module, Access]:
    """Built-in role access, widened (never narrowed below NONE) by overlay.

    Custom roles may grant additional view/full on modules the base role
    already allows at least NONE for; they cannot invent modules outside
    :class:`Module`. Overlay values that are lower than the base are ignored
    so a custom role cannot strip Admin rights by accident.
    """
    merged = dict(ROLE_ACCESS[Role(base_role)])
    if not overlay:
        return merged
    for key, value in overlay.items():
        try:
            module = Module(str(key))
            access = Access(str(value).lower())
        except ValueError:
            continue
        if _ACCESS_RANK[access] > _ACCESS_RANK[merged[module]]:
            merged[module] = access
    return merged


def access_for_merged(
    base_role: Role | str,
    module: Module | str,
    overlay: dict[str, str] | None = None,
) -> Access:
    module_key = Module(module) if not isinstance(module, Module) else module
    return merge_access(base_role, overlay)[module_key]
