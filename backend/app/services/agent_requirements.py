"""What each agent needs connected, and what each integration enables.

Everything here is derived from the code's own declarations — an agent's
``AgentSpec`` (required / any-of capabilities, ``requires_llm``) and each
connector's ``ConnectorSpec.capabilities`` — and from the same connector
bundle a run is built from. So the console's "ready to run" answer is the one
:meth:`BaseAgent.preflight` will reach, not a second opinion that can drift.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from app.agents.base import choice as choice_rules
from app.agents.base.contracts import BaseAgent, ConnectorUse, Impact, UseMode
from app.agents.base.policy import GUARDRAIL_NAMES, REVIEW_SETTING_NAME, Guardrail
from app.agents.base.registry import all_agents
from app.connectors.base import registry as connector_registry
from app.connectors.base.connector import Capability
from app.services import entitlements
from app.services.entitlements import UNRESTRICTED, ConnectorPolicy, PolicyLookup

#: A capability as a thing an integration lets the agents do, in plain words.
CAPABILITY_PLAIN: dict[Capability, str] = {
    Capability.LIST_PAGES: "Find the pages on your website",
    Capability.READ_PAGE: "Read your page content",
    Capability.WRITE_PAGE: "Edit pages on your website",
    Capability.INJECT_SCHEMA: "Add structured data to your pages",
    Capability.READ_AD_PERFORMANCE: "Read your ad results",
    Capability.WRITE_AD_BUDGET: "Change ad budgets",
    Capability.UPLOAD_CREATIVE: "Add new ads",
    Capability.PUSH_AUDIENCE: "Create ad audiences",
    Capability.READ_TRAFFIC_QUALITY: "Check ad clicks for fake traffic",
    Capability.BLOCK_PLACEMENT: "Block low-quality ad placements",
    Capability.READ_PAGE_SPEED: "Measure page speed",
    Capability.READ_SESSIONS: "Read website visits",
    Capability.READ_REFERRERS: "See which sites send you visitors",
    Capability.READ_SEARCH_PERFORMANCE: "Read search rankings and clicks",
    Capability.READ_BACKLINKS: "See which sites link to you and to your competitors",
    Capability.READ_CONVERSIONS: "Read conversions such as sales and leads",
    Capability.READ_FIRST_PARTY_SIGNALS: "Read customer engagement data",
    Capability.CHECK_CITATIONS: "Check whether AI assistants mention your site",
    Capability.COMPLETE: "Write text with AI",
    Capability.SEND_NOTIFICATION: "Send notifications",
    Capability.SEND_EMAIL: "Send emails",
    Capability.READ_LOCAL_RANKINGS: "Check your position in Google Maps searches",
    Capability.READ_REVIEWS: "Read your Google reviews",
    Capability.REPLY_REVIEW: "Reply to your Google reviews",
    Capability.CREATE_PAGE: "Publish new articles to your website",
    Capability.PUBLISH_SOCIAL: "Post to your social media accounts",
    Capability.CREATE_EMAIL_CAMPAIGN: "Create campaigns in your email platform",
    Capability.CREATE_SEARCH_CAMPAIGN: "Create search ad campaigns",
    Capability.GENERATE_IMAGE: "Create images with AI",
}


def plain(capability: Capability) -> str:
    return CAPABILITY_PLAIN.get(capability, capability.value.replace("_", " ").capitalize())


@dataclass(slots=True)
class NeedOption:
    slug: str
    name: str
    connected: bool
    #: The plan does not include it for this agent (app.services.entitlements).
    locked: bool = False
    locked_reason: str = ""


@dataclass(slots=True)
class NeedUse:
    """Which connector(s) an agent uses for one capability, and the control for it.

    Derived from the agent's ``ConnectorUse`` declaration and resolved with
    :mod:`app.agents.base.choice` — the same rules the run applies — so
    ``statement`` names what the run will actually use.
    """

    #: The key capability; the selection is saved under it.
    capability: str
    #: one | all | page_source
    mode: str
    #: The question the control asks.
    question: str = ""
    #: True where the control is shown; False on a need whose selection is
    #: made on another need (replying to reviews, a budget that follows the
    #: platforms chosen above).
    primary: bool = True
    #: "single" (a select), "multi" (checkboxes), or "" (nothing to choose).
    control: str = ""
    #: single: offer "Automatic" — the agent tries each connected one in turn.
    automatic_option: bool = False
    #: The saved selection.
    chosen: list[str] = field(default_factory=list)
    #: What the control shows pre-filled: what the run uses now.
    selected: list[str] = field(default_factory=list)
    #: Part of the saved selection is no longer connected.
    stale: bool = False
    #: What a run uses now (ONE: in the order it tries them).
    using: list[NeedOption] = field(default_factory=list)
    #: Connected connectors that can serve it, in catalogue order; locked
    #: ones are included (disabled, with the reason).
    options: list[NeedOption] = field(default_factory=list)
    #: The sentence for the saved configuration.
    statement: str = ""
    #: single + automatic_option, or multi ONE: what "Automatic" (nothing
    #: chosen) does. ALL / PAGE_SOURCE: what nothing restricted means.
    automatic_statement: str = ""
    #: A selection, in words: "Collects pages from {names}." (``{names}`` is
    #: replaced by the screen with the ticked names, joined with "and").
    template: str = ""
    #: A further sentence that is always true of this use.
    note: str = ""


@dataclass(slots=True)
class Need:
    key: str
    label: str
    satisfied: bool
    options: list[NeedOption]
    uses: list[NeedUse] = field(default_factory=list)
    note: str = ""


def options_for(
    capability: Capability, *, usable: frozenset[str], enabled: set[str] | None
) -> list[NeedOption]:
    out: list[NeedOption] = []
    for spec in connector_registry.all_specs():
        if capability not in spec.capabilities:
            continue
        if enabled is not None and spec.slug not in enabled:
            continue
        out.append(NeedOption(slug=spec.slug, name=spec.name, connected=spec.slug in usable))
    return out


def _name(slug: str) -> str:
    spec = connector_registry.get_spec(slug)
    return spec.name if spec is not None else slug.replace("_", " ").title()


def _in_turn(names: list[str]) -> str:
    """``A``, ``A, then B``, ``A, then B, then C`` — an order, said as one."""
    return ", then ".join(names)


def _and(names: list[str]) -> str:
    """``A``, ``A and B``, ``A, B and C``."""
    if len(names) <= 1:
        return "".join(names)
    return f"{', '.join(names[:-1])} and {names[-1]}"


def _sentence(clause: str) -> str:
    clause = clause.strip()
    if not clause:
        return ""
    clause = clause[:1].upper() + clause[1:]
    return clause if clause.endswith(".") else clause + "."


def _was_chosen(slugs: list[str]) -> str:
    names = _and([_name(s) for s in slugs])
    return f"{names} {'was' if len(slugs) == 1 else 'were'} chosen but " + (
        "is no longer connected" if len(slugs) == 1 else "are no longer connected"
    )


def describe_use(
    use: ConnectorUse,
    *,
    usable: Iterable[str],
    enabled: set[str] | None = None,
    chosen: Iterable[str] = (),
    primary: bool = True,
    policy: ConnectorPolicy = UNRESTRICTED,
    key: ConnectorUse | None = None,
    legacy: str = "",
) -> NeedUse:
    """One ``ConnectorUse`` resolved against what this workspace has connected.

    ``key`` is the declaration whose selection applies (``use`` itself unless
    it ``follows`` another); ``chosen`` is that selection as saved;
    ``legacy`` is the value of the setting it replaces, if one is saved.
    Every sentence comes from :func:`app.agents.base.choice.resolve`, the
    same resolution the run reaches.
    """
    key = key or use
    usable = frozenset(usable)
    if enabled is not None:
        usable = usable & frozenset(enabled)
    resolved = choice_rules.resolve(key, usable=usable, chosen=chosen, policy=policy)
    picked = list(resolved.chosen)
    every_capable = choice_rules.capable_slugs(key, usable)  # before the plan
    options = [
        NeedOption(
            slug=s,
            name=_name(s),
            connected=True,
            locked=not policy.permits(s),
            locked_reason="" if policy.permits(s) else policy.locked_reason,
        )
        for s in every_capable
    ]
    capable = list(resolved.capable)
    names = [_name(s) for s in resolved.order]
    template = _sentence(key.selected or "uses {names}")
    out = NeedUse(
        capability=key.capability.value,
        mode=key.mode.value,
        question=key.question,
        primary=primary and use is key,
        chosen=picked,
        stale=bool(resolved.stale),
        options=options,
        using=[NeedOption(slug=s, name=_name(s), connected=True) for s in resolved.order],
        template=template,
        note=use.note,
    )
    if out.primary and options:
        out.control = "multi" if choice_rules.is_multi(key) else "single"
    kept = [s for s in capable if s in picked]

    # ── A use that goes by another's selection: say what it means here ──
    if use is not key:
        # Only those of the key's selection that can do *this* (Meta Ads is
        # checked for fake clicks, but cannot block a placement).
        own = set(choice_rules.capable_slugs(use, usable, policy))
        here = [_name(s) for s in resolved.order if s in own]
        out.using = [o for o in out.using if o.slug in own]
        if use.selected and here and (key.mode is not UseMode.ONE or kept):
            out.statement = _sentence(use.selected.format(names=_and(here)))
        return out

    if key.mode is UseMode.PAGE_SOURCE:
        out.automatic_statement = _sentence(key.everything)
        if resolved.every:
            out.selected = capable
            out.statement = out.automatic_statement
            return out
        # A chosen website that is disconnected right now still decides which
        # collected pages are in reach, so it stays listed — and ticked.
        for slug in resolved.stale:
            if policy.permits(slug):
                out.options.append(NeedOption(slug=slug, name=_name(slug), connected=False))
        out.selected = [s for s in picked if policy.permits(s)] if picked else capable
        out.statement = (
            template.format(names=_and(names))
            if names
            else "Works on no pages: none of the websites chosen for it is available."
        )
        return out

    if key.mode is UseMode.ALL:
        group = key.group or "integrations"
        connected_names = _and([_name(s) for s in capable])
        out.automatic_statement = f"Uses all connected {group}: {connected_names}." if capable else ""
        if legacy and not picked:
            out.selected = capable
            out.statement = (
                f"Uses the list saved earlier in this agent's settings: {legacy}. Saving these "
                "settings replaces it with the selection here."
            )
            return out
        if not picked:
            out.selected = list(resolved.order)
            if len(resolved.order) < len(capable):
                out.statement = (
                    f"Uses {_and(names)} — your plan allows {policy.max_selected} at a time."
                )
            else:
                out.statement = out.automatic_statement
            return out
        out.selected = kept
        if not resolved.order:
            out.statement = f"None of the {group} chosen for this agent is connected, so it uses none."
        elif set(resolved.order) >= set(capable):
            out.statement = out.automatic_statement
        else:
            out.statement = f"Uses {_and(names)} (of {len(capable)} connected)."
        if resolved.stale:
            out.statement += f" {_sentence(_was_chosen(list(resolved.stale)))}"
        return out

    # ── ONE ──
    automatic = choice_rules.resolve(key, usable=usable, chosen=(), policy=policy)
    auto_names = [_name(s) for s in automatic.order]
    out.automatic_option = bool(key.tries_in_turn) and not key.multi and len(capable) > 1
    if not auto_names:
        out.automatic_statement = ""
    elif len(auto_names) == 1:
        out.automatic_statement = f"Uses {auto_names[0]}."
    else:
        out.automatic_statement = (
            f"Automatic tries {_in_turn(auto_names)}, and uses the first one that "
            f"{key.automatic or 'works'}."
        )

    if kept:
        out.selected = kept if key.multi else kept[:1]
        out.statement = (
            template.format(names=_and(names)) if key.multi else f"Uses {names[0]}."
        )
        if resolved.stale and key.multi:
            out.statement += f" {_sentence(_was_chosen(list(resolved.stale)))}"
        return out

    # Nothing chosen is usable: the automatic rule decided.
    if key.multi or out.automatic_option:
        out.selected = []
    else:
        out.selected = list(automatic.order[:1])
    if resolved.stale and not key.multi:
        # A single choice whose connector went away: every connector is equal,
        # so say only what is used now (the picker shows it; saving keeps it).
        out.statement = (
            f"Uses {auto_names[0]}." if len(auto_names) == 1 and not out.automatic_option
            else out.automatic_statement
        )
    elif resolved.stale:
        lost = _was_chosen(list(resolved.stale))
        if not auto_names:
            out.statement = f"{lost}, and nothing else connected can do this."
        elif len(auto_names) == 1:
            out.statement = f"{lost} — using {auto_names[0]} instead."
        else:
            out.statement = f"{lost} — trying {_in_turn(auto_names)} instead."
    else:
        out.statement = out.automatic_statement
    return out


def uses_for(
    agent: BaseAgent,
    capabilities: Iterable[Capability],
    *,
    usable: Iterable[str],
    enabled: set[str] | None = None,
    choices: Mapping[str, list[str]] | None = None,
    described: set[str] | None = None,
    policy: PolicyLookup | None = None,
    settings: Mapping | None = None,
) -> list[NeedUse]:
    """The uses covering these capabilities, each described once.

    ``described`` collects the key capabilities already given a control, so a
    selection that covers two needs is asked on the first only.
    """
    seen = described if described is not None else set()
    saved = {k: choice_rules.as_list(v) for k, v in (choices or {}).items()}
    lookup = policy or entitlements.unrestricted
    out: list[NeedUse] = []
    taken: set[str] = set()
    for capability in capabilities:
        use = agent.spec.use_for(capability)
        if use is None or id(use) in taken:
            continue
        taken.add(id(use))
        key = choice_rules.key_use(agent.spec, use)
        primary = key.capability.value not in seen
        if use is key:
            seen.add(key.capability.value)
        legacy = ""
        if key.replaces_setting:
            legacy = str((settings or {}).get(key.replaces_setting) or "").strip()
        out.append(
            describe_use(
                use,
                key=key,
                usable=usable,
                enabled=enabled,
                chosen=saved.get(key.capability.value, []),
                primary=primary,
                policy=lookup(agent.slug, key.capability),
                legacy=legacy,
            )
        )
    return out


def clean_choices(
    agent: BaseAgent,
    requested: Mapping[str, object],
    *,
    usable: Iterable[str],
    enabled: set[str] | None = None,
    saved: Mapping[str, list[str]] | None = None,
    policy: PolicyLookup | None = None,
) -> tuple[dict[str, list[str]], list[str]]:
    """Validate a submitted ``{capability: slug | [slug, …]}``. Returns ``(to store, problems)``.

    * The capability must be one the operator chooses for on this agent (a
      declaration that does not ``follow`` another).
    * A string is read as a one-item list; an empty list means automatic /
      everything, and is not stored.
    * No duplicates; at most one where the agent uses a single connector;
      no more than the plan allows.
    * Each connector must exist in this portal, be able to do the job, be
      allowed by the plan, and be connected — except that re-submitting a
      connector already saved is accepted while it is disconnected, so saving
      other settings does not silently erase the choice.
    """
    usable = frozenset(usable)
    saved = {k: choice_rules.as_list(v) for k, v in (saved or {}).items()}
    lookup = policy or entitlements.unrestricted
    problems: list[str] = []
    out: dict[str, list[str]] = {}
    for raw_key, raw_value in (requested or {}).items():
        key = str(raw_key or "").strip()
        try:
            capability = Capability(key)
        except ValueError:
            problems.append(f"{agent.spec.name} has no connection choice called {key!r}.")
            continue
        use = agent.spec.use_for(capability)
        if use is None or use.capability is not capability or not choice_rules.selectable(use):
            problems.append(
                f"{agent.spec.name} has no choice of connection to {plain(capability).lower()}."
            )
            continue
        if not isinstance(raw_value, str | list | tuple) or (
            not isinstance(raw_value, str) and not all(isinstance(v, str) for v in raw_value)
        ):
            problems.append(f"The choice for {plain(capability).lower()} must be a list of integrations.")
            continue
        items = [raw_value.strip()] if isinstance(raw_value, str) else [str(v).strip() for v in raw_value]
        items = [v for v in items if v]
        if len(set(items)) != len(items):
            twice = next(v for v in items if items.count(v) > 1)
            problems.append(f"{_name(twice)} is listed twice.")
            continue
        if not choice_rules.is_multi(use) and len(items) > 1:
            problems.append(
                f"{agent.spec.name} uses one connection to {plain(capability).lower()}: "
                "choose only one."
            )
            continue
        rules = lookup(agent.slug, capability)
        if rules.max_selected is not None and len(items) > rules.max_selected:
            problems.append(
                f"Choose at most {rules.max_selected} for {plain(capability).lower()}. "
                f"{rules.reason}".strip()
            )
            continue
        kept: list[str] = []
        for slug in items:
            spec = connector_registry.get_spec(slug)
            if spec is None or (enabled is not None and slug not in enabled):
                problems.append(f"{slug!r} is not an integration available in this workspace.")
                break
            missing = [c for c in (use.capability, *use.also) if c not in spec.capabilities]
            if missing:
                problems.append(f"{spec.name} cannot {plain(missing[0]).lower()}.")
                break
            if not rules.permits(slug):
                problems.append(f"{spec.name} cannot be used by {agent.spec.name}. {rules.locked_reason}")
                break
            if slug not in usable and slug not in saved.get(key, []):
                problems.append(
                    f"{spec.name} is not connected, so {agent.spec.name} cannot use it. "
                    "Connect it under Connectors first."
                )
                break
            kept.append(slug)
        else:
            if kept:
                out[key] = kept
    return out, problems


def _llm_options(
    *, usable: frozenset[str], enabled: set[str] | None, policy: ConnectorPolicy = UNRESTRICTED
) -> list[NeedOption]:
    from app.llm.from_connector import supports_agent_llm

    out = []
    for option in options_for(Capability.COMPLETE, usable=usable, enabled=enabled):
        if not supports_agent_llm(option.slug):
            continue
        if not policy.permits(option.slug):
            option.locked = True
            option.locked_reason = policy.locked_reason
        out.append(option)
    return out


def needs_for(
    agent: BaseAgent,
    *,
    usable: frozenset[str],
    enabled: set[str] | None = None,
    llm_choice: str = "",
    choices: Mapping[str, list[str]] | None = None,
    described: set[str] | None = None,
    policy: PolicyLookup | None = None,
    settings: Mapping | None = None,
) -> list[Need]:
    """The agent's prerequisites, each with the integrations that satisfy it.

    ``usable`` is the set of connector slugs a run would actually receive
    (connected *and* with complete credentials) — ``ConnectorBundle.connected``.
    ``choices`` is the agent's saved selections
    (:func:`app.agents.base.choice.saved_choices`); each need carries which
    connector(s) the run will use for it and the control to change that.
    ``policy`` is the plan (:func:`app.services.entitlements.lookup`).
    """
    spec = agent.spec
    needs: list[Need] = []
    described = described if described is not None else set()
    lookup = policy or entitlements.unrestricted

    def uses(*capabilities: Capability) -> list[NeedUse]:
        return uses_for(
            agent,
            capabilities,
            usable=usable,
            enabled=enabled,
            choices=choices,
            described=described,
            policy=lookup,
            settings=settings,
        )

    for capability in spec.required_capabilities:
        options = options_for(capability, usable=usable, enabled=enabled)
        needs.append(
            Need(
                key=capability.value,
                label=plain(capability),
                satisfied=any(o.connected for o in options),
                options=options,
                uses=uses(capability),
            )
        )

    # "Any AI model" declared as a capability is the same need requires_llm
    # adds below; listing it twice would show "Write text with AI" twice.
    any_of = tuple(
        c for c in spec.any_of_capabilities if not (spec.requires_llm and c is Capability.COMPLETE)
    )
    if any_of:
        seen: set[str] = set()
        options: list[NeedOption] = []
        for capability in any_of:
            for option in options_for(capability, usable=usable, enabled=enabled):
                if option.slug not in seen:
                    seen.add(option.slug)
                    options.append(option)
        needs.append(
            Need(
                key="+".join(c.value for c in any_of),
                label=" or ".join(
                    text if i == 0 else text[:1].lower() + text[1:]
                    for i, text in enumerate(plain(c) for c in any_of)
                ),
                satisfied=any(o.connected for o in options),
                options=options,
                uses=uses(*any_of),
            )
        )

    if spec.requires_llm:
        from app.llm.from_connector import effective_llm

        model_policy = lookup(agent.slug, Capability.COMPLETE)
        options = _llm_options(usable=usable, enabled=enabled, policy=model_policy)
        # Same rule as the run: the chosen model if connected (and allowed),
        # else the first connected one. Every model is equal, so a saved
        # model that is no longer connected is not mentioned — the screen
        # shows the one in use, and saving keeps it.
        using, _stale = effective_llm(
            (llm_choice or "").strip(), {o.slug for o in options if o.connected and not o.locked}
        )
        needs.append(
            Need(
                key="ai_model",
                label="Write text with AI",
                satisfied=bool(using),
                options=options,
                note=f"Uses {_name(using)}." if using else "",
            )
        )

    return needs


def _review_setting(guardrail: Guardrail) -> str:
    """The workspace setting as a person reads it, e.g. 'Review before
    publishing is set to "Always ask me"'."""
    return f'{REVIEW_SETTING_NAME} is set to "{GUARDRAIL_NAMES[guardrail.value]}"'


def approval_behaviour(
    agent: BaseAgent, *, guardrail: Guardrail, autonomous: bool, makes_changes: bool = True
) -> str:
    """What happens to this agent's changes, given the policy in force.

    Mirrors :func:`app.agents.base.policy.decide` — the only place the
    apply-or-queue decision is made.
    """
    if not makes_changes:
        return "Nothing to approve — this agent only reports."
    if not autonomous:
        return "Every change waits for your approval before it goes live."
    if guardrail is Guardrail.HUMAN:
        return (
            "Every change waits for your approval, because "
            f"{_review_setting(Guardrail.HUMAN)}."
        )
    # Every agent proposes a single kind of change, at its declared impact, so
    # the spec's max_impact is the impact of all of its changes.
    if guardrail is Guardrail.HYBRID and agent.spec.max_impact is Impact.HIGH:
        return (
            f"Every change waits for your approval: {_review_setting(Guardrail.HYBRID)}, "
            "and this agent's changes count as big."
        )
    return (
        "Changes go live automatically, up to the daily limit. Extra changes wait "
        "for the next run."
    )


def agents_using(connector_slug: str, *, enabled_agents: set[str] | None = None) -> list[tuple[str, str]]:
    """``(slug, name)`` of every agent this integration can serve."""
    spec = connector_registry.get_spec(connector_slug)
    if spec is None:
        return []
    from app.llm.from_connector import supports_agent_llm

    is_llm = Capability.COMPLETE in spec.capabilities and supports_agent_llm(spec.slug)
    out: list[tuple[str, str]] = []
    for agent in all_agents():
        if enabled_agents is not None and agent.slug not in enabled_agents:
            continue
        from app.agents.base.plain import guide_for

        guide = guide_for(agent.slug)
        wanted = (
            set(agent.spec.required_capabilities)
            | set(agent.spec.any_of_capabilities)
            | {extra.capability for extra in (guide.optional if guide else ())}
        )
        if (wanted & spec.capabilities) or (agent.spec.requires_llm and is_llm):
            out.append((agent.slug, agent.spec.name))
    return out


def connector_abilities(connector_slug: str) -> list[str]:
    """What this integration lets agents do, in plain words, in a stable order."""
    spec = connector_registry.get_spec(connector_slug)
    if spec is None:
        return []
    return [plain(c) for c in Capability if c in spec.capabilities]
