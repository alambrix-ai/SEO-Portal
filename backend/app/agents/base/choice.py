"""Which connectors an agent uses for each capability, and the operator's say in it.

An agent asks for a *capability* — a website it can publish to, the ad
platforms it manages — not a vendor. For every capability it looks up the
operator may choose which connected integration(s) it uses. The choice is
stored per agent in ``AgentRecord.settings[CHOICES_KEY]`` as
``{capability: [slug, …]}`` — an internal key (leading underscore), so it is
never shown as an agent setting — keyed by the *key* capability of the
agent's :class:`ConnectorUse` declaration (its own, or the one it
``follows``). A plain string saved before lists existed reads as ``[it]``.

The rules here are the only place a selection is resolved. The run context
(:class:`app.agents.base.context.AgentContext`) and the setup screen
(:mod:`app.services.agent_requirements`) both call :func:`resolve`, so the
screen's "Uses WordPress" is what the run does.

========================  ==================  =================================
Mode                      Nothing chosen      Something chosen
========================  ==================  =================================
ONE (single)              first connected,    the chosen one; if disconnected,
                          or each in turn     back to automatic
ONE + multi               as above            every chosen one still connected;
                                              none left: back to automatic
ALL                       every connected     only the chosen ones still
                                              connected — never widened
PAGE_SOURCE               every website       only pages from the chosen
                                              websites — never widened
========================  ==================  =================================

A connector the plan does not allow (:mod:`app.services.entitlements`) is
treated everywhere as if it were not connected.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from app.agents.base.contracts import AgentSpec, ConnectorUse, UseMode
from app.connectors.base.connector import Capability
from app.services.entitlements import UNRESTRICTED, ConnectorPolicy

#: Where the choices live in ``AgentRecord.settings``.
CHOICES_KEY = "_connectors"


def as_list(value: object) -> list[str]:
    """A stored or submitted value as a clean list: str → [str], duplicates dropped."""
    if isinstance(value, str):
        items: Iterable[object] = [value]
    elif isinstance(value, list | tuple):
        items = value
    else:
        return []
    out: list[str] = []
    for item in items:
        if isinstance(item, str) and item.strip() and item.strip() not in out:
            out.append(item.strip())
    return out


def saved_choices(settings: Mapping | None) -> dict[str, list[str]]:
    """``{capability value: [slug, …]}`` as stored, malformed entries dropped."""
    raw = (settings or {}).get(CHOICES_KEY)
    if not isinstance(raw, Mapping):
        return {}
    out: dict[str, list[str]] = {}
    for key, value in raw.items():
        items = as_list(value)
        if isinstance(key, str) and items:
            out[key] = items
    return out


def key_use(spec: AgentSpec | None, use: ConnectorUse) -> ConnectorUse:
    """The declaration whose selection ``use`` goes by (itself, or what it follows)."""
    if spec is not None and use.follows is not None:
        followed = spec.use_for(use.follows)
        if followed is not None and followed is not use:
            return followed
    return use


def selectable(use: ConnectorUse) -> bool:
    """Whether the operator chooses for this declaration directly."""
    return use.follows is None


def is_multi(use: ConnectorUse) -> bool:
    """Several may be selected: ALL always, PAGE_SOURCE always, ONE when declared."""
    return use.mode is not UseMode.ONE or use.multi


def selection_for(
    spec: AgentSpec | None, settings: Mapping | None, capability: Capability
) -> tuple[ConnectorUse | None, list[str]]:
    """``(the key declaration, the saved slugs)`` for a capability the agent uses."""
    if spec is None:
        return None, []
    use = spec.use_for(capability)
    if use is None:
        return None, []
    key = key_use(spec, use)
    return key, saved_choices(settings).get(key.capability.value, [])


def capable_slugs(
    use: ConnectorUse, usable: Iterable[str], policy: ConnectorPolicy = UNRESTRICTED
) -> list[str]:
    """Usable connectors the plan allows that can serve ``use``, in catalogue order."""
    from app.connectors.base import registry

    present = set(usable)
    needed = {use.capability, *use.also}
    out: list[str] = []
    for slug in registry.connector_slugs():
        if slug not in present or not policy.permits(slug):
            continue
        spec = registry.get_spec(slug)
        if spec is not None and needed <= set(spec.capabilities):
            out.append(slug)
    return out


@dataclass(frozen=True, slots=True)
class Resolution:
    """What one declaration resolves to, given what is connected and chosen."""

    #: ONE: connectors a run tries or uses, in order. ALL: every one it uses.
    #: PAGE_SOURCE: the websites whose pages it works on ("" = all, see
    #: ``every``).
    order: tuple[str, ...]
    #: The saved selection.
    chosen: tuple[str, ...]
    #: Saved slugs that are not usable now (disconnected, or not allowed).
    stale: tuple[str, ...]
    #: Usable connectors that can serve it.
    capable: tuple[str, ...]
    #: PAGE_SOURCE: no selection, so every collected page is in reach.
    every: bool = False

    @property
    def automatic(self) -> bool:
        """ONE: nothing chosen is usable, so the automatic rule decided."""
        return not (set(self.chosen) & set(self.capable))


def resolve(
    use: ConnectorUse,
    *,
    usable: Iterable[str],
    chosen: Iterable[str] = (),
    policy: ConnectorPolicy = UNRESTRICTED,
) -> Resolution:
    """The connectors a run would use for ``use``. Pure; no connections opened.

    ``use`` must be the key declaration (see :func:`key_use`).
    """
    capable = capable_slugs(use, usable, policy)
    picked = as_list(list(chosen))
    kept = [s for s in capable if s in picked]  # catalogue order
    stale = tuple(s for s in picked if s not in capable)

    if use.mode is UseMode.PAGE_SOURCE:
        # Verbatim: pages from a website that was not chosen are never
        # touched, even when every chosen one is disconnected right now.
        allowed = [s for s in picked if policy.permits(s)]
        if picked:
            return Resolution(tuple(allowed), tuple(picked), stale, tuple(capable))
        if policy.allowed is not None:
            return Resolution(tuple(capable), (), (), tuple(capable))
        return Resolution((), (), (), tuple(capable), every=True)

    if use.mode is UseMode.ALL:
        order = kept if picked else capable
        if policy.max_selected is not None:
            order = order[: policy.max_selected]
        return Resolution(tuple(order), tuple(picked), stale, tuple(capable))

    # ONE
    if kept:
        order = kept if use.multi else kept[:1]
    else:
        order = capable if use.tries_in_turn else capable[:1]
    return Resolution(tuple(order), tuple(picked), stale, tuple(capable))
