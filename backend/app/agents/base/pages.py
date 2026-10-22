"""Getting pages for an agent to work on, and saying why when there are none.

This exists because of a run that reported, truthfully but uselessly:

    On-Page SEO Sync did not run — walked 125 files in acme/website on main
    and none of them look like pages. The commonest were .jsx (62), .js (33).

Five files in that repository *were* classified as pages. They were then all
removed by the agent's scope filter, and the code fell through to the
connector's "why was the listing empty" diagnosis — which answers a question
nobody had asked, about the repository rather than about the filter. Somebody
spent an afternoon adding content folders to fix a scope typo.

So the rule here is that the reason has to name the step that actually
discarded the pages:

* an unusable scope, before anything is fetched;
* a source that genuinely returned nothing, which is the connector's own
  diagnosis to give;
* a source that returned pages the scope then excluded — with the scope
  quoted back and real URLs shown, because that is the whole fix;
* pages that exist but cannot be used for what the agent needs.

The second reason it exists is fall-forward. ``with_capability`` returns the
first connected connector in *catalogue order*, which is registration order —
an arbitrary tie-break that reads like a preference. A workspace with both a
repository and the Site Crawler connected always got the repository, because
it registers earlier, even when the repository is a React app whose pages are
compositions with no copy in them and the crawler can see the rendered words.
Here every capable source is tried in turn — unless the operator has chosen
one in the agent's settings, in which case only that one is used (see
:mod:`app.agents.base.choice`).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from app.agents.base import scope as scope_rules
from app.agents.base.context import AgentContext
from app.connectors.base.connector import Capability
from app.connectors.base.interfaces import RemotePage
from app.core.exceptions import ConnectorError
from app.core.logging import get_logger

log = get_logger(__name__)

#: How many example URLs to quote when the scope excluded everything. Enough
#: to see the shape of the site's paths, few enough to read in a toast.
_EXAMPLES = 6


def is_usable(page: RemotePage) -> bool:
    """Whether this page's body is content rather than source code.

    A repository connector lists a framework page file — ``src/app/page.jsx``
    — because knowing the URL exists is useful. Its *body* is a composition
    of components with no prose in it, so asking a model to find the topical
    gaps in it produces confident nonsense about an import block, and any
    rewrite would replace working code.

    ``rendered`` is the Site Crawler, which reads the published HTML and is
    therefore always usable *for reading*. ``writable`` is true for a CMS node
    and for Markdown in a repository, and false for exactly the source files
    above. Usable is not the same as writable: see :func:`is_writable`.
    """
    metadata = page.metadata or {}
    if metadata.get("source") == "rendered":
        return True
    return bool(metadata.get("writable", True))


def is_writable(page: RemotePage) -> bool:
    """Whether a change to this page could ever be written back.

    The Site Crawler's pages are usable — the words are real — but it reads
    the published site and its ``write_page`` refuses every call. Treating
    "usable" as "writable" queued rewrites, Q&A blocks and schema against
    those pages that could only fail on the day somebody approved them.
    """
    metadata = page.metadata or {}
    if metadata.get("source") == "rendered":
        return False
    return bool(metadata.get("writable", True))


def read_only_sources() -> list[str]:
    """Connectors that can list pages but never write one.

    Stored pages carry the slug of the system they were synced from, and
    that, not the page, is what decides whether a write can land. Read from
    the registry rather than the workspace's connections: a CMS that is
    disconnected today can be reconnected, and its pages stay proposable
    (the apply step says to reconnect it); a crawler can never write.
    """
    from app.connectors.base import registry

    return [
        spec.slug
        for spec in registry.all_specs()
        if Capability.LIST_PAGES in spec.capabilities
        and Capability.WRITE_PAGE not in spec.capabilities
    ]


def writable_rows(column):  # noqa: ANN001, ANN201 - a SQLAlchemy column
    """A condition on ``SeoPage.cms_slug`` keeping only pages a write can reach.

    For the agents that propose changes to a page. The auditor and anything
    else that only reads should not use it: a crawled page is a perfectly good
    subject for an audit.
    """
    blocked = read_only_sources()
    if not blocked:
        return None
    return column.not_in(blocked)


#: The most LLM-backed items one run takes on, however generous the daily
#: allowance. Each one is a model call, so this bounds a run's latency and
#: spend even when somebody sets the daily limit to a thousand.
MAX_ITEMS_PER_RUN = 25


def batch_size(ctx: AgentContext, *, floor: int) -> int:
    """How many pages a run should work through.

    The rule: the daily allowance still left (``remaining_actions``), but
    never fewer than ``floor`` — the fixed number each agent used before —
    and never more than :data:`MAX_ITEMS_PER_RUN`. Raising the daily limit
    therefore makes each run do more, which is what somebody raising it
    expects, while a low or used-up allowance still lets a run prepare a
    small batch: proposals queued for review do not count against the cap,
    and under autonomy the runner holds back anything over it.
    """
    try:
        remaining = int(ctx.remaining_actions)
    except (AttributeError, TypeError, ValueError):
        remaining = 0
    return max(floor, min(remaining, MAX_ITEMS_PER_RUN))


def current_body(ctx: AgentContext, connector: object, page: object) -> str:
    """The page's body as it is live now, refreshing the stored copy.

    Merging an addition into the *stored* body and writing that back replaced
    the live page with whatever the last sync saw — so an edit somebody made
    in the CMS since then was silently reverted by a Q&A block or a schema
    tag. Read first, merge into that.

    A connector that cannot read falls back to the stored copy, which is the
    best available. A body that comes back empty is refused rather than
    written over: merging into nothing would publish a page consisting only
    of the addition.
    """
    from app.services.encryption import Ctx

    body = ""
    reader = getattr(connector, "read_page", None)
    if callable(reader):
        try:
            fresh = reader(page.cms_id)  # type: ignore[attr-defined]
        except NotImplementedError:
            fresh = None
        if fresh is not None and fresh.body:
            body = fresh.body
            page.body_encrypted = ctx.cipher.encrypt(body, context=Ctx.PAGE_BODY)  # type: ignore[attr-defined]
    if not body:
        body = ctx.cipher.decrypt(page.body_encrypted, context=Ctx.PAGE_BODY)  # type: ignore[attr-defined]
    if not body:
        raise ConnectorError(
            f"The current content of {page.url} could not be read, so nothing "  # type: ignore[attr-defined]
            f"was written to it. Run the sync again and retry."
        )
    return body


@dataclass(slots=True)
class PageHarvest:
    """Pages for this run, or the reason there are none."""

    pages: list[RemotePage] = field(default_factory=list)
    source: object | None = None
    #: Empty while pages were found. Otherwise the operator's next action.
    reason: str = ""
    #: Pages the source returned before the scope filter, for reporting.
    listed: int = 0
    #: Of ``pages``, how many carry a body worth analysing.
    usable: int = 0

    @property
    def ok(self) -> bool:
        return bool(self.pages)

    @property
    def usable_pages(self) -> list[RemotePage]:
        """The pages whose body is content rather than component source."""
        return [page for page in self.pages if is_usable(page)]

    @property
    def source_slug(self) -> str:
        return getattr(self.source, "slug", "")


def _describe_source(connector: object) -> str:
    """The connector's own account of why its listing was empty."""
    describe = getattr(connector, "describe_content", None)
    if callable(describe):
        try:
            return describe()
        except Exception as exc:  # noqa: BLE001 - a diagnosis must not fail a run
            log.warning("describe_content failed on %s: %s", connector, exc)
    return ""


def _excluded_message(scope: str, urls: list[str], source_name: str) -> str:
    """The message that used to be a lie about the repository."""
    shown = ", ".join(urls[:_EXAMPLES])
    more = f" and {len(urls) - _EXAMPLES} more" if len(urls) > _EXAMPLES else ""
    return (
        f"{source_name} returned {len(urls)} pages but none of them are in "
        f"{scope_rules.describe(scope)}. The pages it can see are: {shown}"
        f"{more}. Change the scope to one of those paths, or clear it to "
        f"cover the whole site."
    )


def harvest(
    ctx: AgentContext,
    *,
    limit: int = 200,
    capability: Capability = Capability.LIST_PAGES,
    need_usable: bool = False,
    prefer_writable: bool = False,
    sources: list | None = None,
) -> PageHarvest:
    """Pages from the best connected source, filtered to the agent's scope.

    ``sources`` restricts the sources tried (see :func:`harvest_each`); by
    default they are the agent's candidates for ``capability``.

    ``need_usable`` is for the agents that read or rewrite a page's body. With
    it set, a source offering only source files is passed over in favour of
    one that can supply real content, and if none can that is what gets
    reported — rather than six rewrites queued against files whose write
    would be refused when somebody finally approved them.

    ``prefer_writable`` is for the agents that change pages. A source whose
    usable pages can none of them be written — the Site Crawler — is kept
    only as a fallback while the remaining sources are tried, so where a CMS
    and the crawler both list a page, the copy that can be written back wins.
    The crawler's pages are still returned when nothing better exists: they
    are worth syncing for the audit even though no rewrite can land on them.
    """
    scope = (ctx.scope or "").strip()

    # Before any network call: a scope that cannot match anything is a
    # configuration error, and silently filtering every page on the strength
    # of it is how this went unnoticed. Validated at run time as well as on
    # save because a value stored before the validator existed is still in
    # the database, and a stored typo is exactly the case that hurts.
    problem = scope_rules.paths(scope)
    if problem:
        return PageHarvest(reason=f"The scope saved for this agent is unusable. {problem}")

    matcher = scope_rules.path_matcher(scope)
    # Only the website chosen in the agent's settings, when one is chosen
    # and still connected; otherwise every capable source, in turn.
    candidates = list(sources) if sources is not None else ctx.candidates(capability)
    if not candidates:
        return PageHarvest(reason="Waiting on a connector that can list pages")

    best: PageHarvest | None = None
    read_only: PageHarvest | None = None
    reasons: list[str] = []

    for connector in candidates:
        name = ctx.connector_name(getattr(connector, "slug", ""))
        try:
            listed = connector.list_pages(limit=limit)  # type: ignore[attr-defined]
        except ConnectorError as exc:
            # One broken source must not hide a working one.
            reasons.append(f"{name}: {exc}")
            continue

        if not listed:
            reasons.append(_describe_source(connector) or f"{name} returned no pages.")
            continue

        matched = [page for page in listed if matcher(page.url)]
        if not matched:
            reasons.append(
                _excluded_message(scope, [page.url for page in listed], name)
            )
            continue

        usable = [page for page in matched if is_usable(page)]
        found = PageHarvest(
            pages=matched,
            source=connector,
            listed=len(listed),
            usable=len(usable),
        )
        if usable or not need_usable:
            if prefer_writable and usable and not any(is_writable(p) for p in usable):
                read_only = read_only or found
                continue
            return found
        # Pages, but none with a body worth reading. Keep it in case nothing
        # better turns up, and try the next source.
        best = best or found
        reasons.append(
            f"{name} lists {len(matched)} pages, but their text cannot be "
            f"read from it, so there is nothing here to analyse or rewrite."
        )

    if read_only is not None:
        return read_only

    if best is not None:
        # Everything that could serve offered only source files. Say what to
        # do about it: this is the Site Crawler's entire reason for existing.
        # When a choice narrowed the sources to one, a connected crawler was
        # never tried, and choosing it is the fix.
        chosen = ctx.chosen_connector(capability)
        crawler_skipped = (
            chosen is not None
            and getattr(chosen, "slug", "") != "site_crawler"
            and ctx.has_connector("site_crawler")
        )
        next_step = (
            "Choose the Site Crawler as this agent's website in its settings — it "
            "reads the published pages as a search engine sees them."
            if crawler_skipped
            else "Connect the Site Crawler, which reads the published pages as a "
            "search engine sees them."
        )
        return PageHarvest(
            pages=[],
            source=best.source,
            listed=best.listed,
            reason=(
                f"{ctx.connector_name(best.source_slug)} lists "
                f"{len(best.pages)} pages, but their text cannot be read from "
                f"the repository on this site. {next_step}"
            ),
        )

    return PageHarvest(reason=" ".join(reasons) if reasons else "No pages were found.")


@dataclass(slots=True)
class MultiHarvest:
    """Pages from every website the operator chose, each source on its own."""

    #: One harvest per source that returned pages, read-only sources first
    #: (so where two list the same URL, the copy that can be written back is
    #: stored last and wins).
    found: list[PageHarvest] = field(default_factory=list)
    #: Why each other source returned nothing, naming it.
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.found)

    @property
    def reason(self) -> str:
        return " ".join(self.problems) or "No pages were found."


def harvest_each(
    ctx: AgentContext,
    *,
    limit: int = 200,
    capability: Capability = Capability.LIST_PAGES,
    need_usable: bool = False,
    prefer_writable: bool = False,
) -> MultiHarvest:
    """Pages from *every* website chosen in the agent's settings.

    With websites chosen, each one is harvested on its own: one that fails
    or returns nothing is reported by name and does not stop the others.
    With nothing chosen this is :func:`harvest` — the first source that has
    pages the agent can work on — exactly as before.
    """
    finder = getattr(ctx, "chosen_connectors", None)
    chosen = finder(capability) if callable(finder) else []
    if not chosen:
        one = harvest(
            ctx,
            limit=limit,
            capability=capability,
            need_usable=need_usable,
            prefer_writable=prefer_writable,
        )
        return MultiHarvest(found=[one] if one.ok else [], problems=[] if one.ok else [one.reason])

    out = MultiHarvest()
    for connector in chosen:
        one = harvest(
            ctx,
            limit=limit,
            capability=capability,
            need_usable=need_usable,
            prefer_writable=prefer_writable,
            sources=[connector],
        )
        if one.ok:
            out.found.append(one)
            continue
        name = ctx.connector_name(getattr(connector, "slug", ""))
        reason = one.reason or "No pages were found."
        out.problems.append(reason if reason.startswith(name) else f"{name}: {reason}")
    read_only = set(read_only_sources())
    out.found.sort(key=lambda h: 0 if h.source_slug in read_only else 1)
    return out


def source_filter(ctx: AgentContext, column, capability: Capability):  # noqa: ANN001, ANN201
    """A condition keeping only pages from the websites this agent works on, or None.

    ``column`` is ``SeoPage.cms_slug``. ``None`` when every website is in
    reach (nothing chosen); an empty selection never matches anything.
    """
    finder = getattr(ctx, "page_sources", None)
    sources = finder(capability) if callable(finder) else None
    if sources is None:
        return None
    return column.in_(list(sources))
