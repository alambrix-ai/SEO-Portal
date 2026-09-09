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
Here every capable source is tried in turn.
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
    therefore always usable. ``writable`` is true for a CMS node and for
    Markdown in a repository, and false for exactly the source files above.
    """
    metadata = page.metadata or {}
    if metadata.get("source") == "rendered":
        return True
    return bool(metadata.get("writable", True))


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
) -> PageHarvest:
    """Pages from the best connected source, filtered to the agent's scope.

    ``need_usable`` is for the agents that read or rewrite a page's body. With
    it set, a source offering only source files is passed over in favour of
    one that can supply real content, and if none can that is what gets
    reported — rather than six rewrites queued against files whose write
    would be refused when somebody finally approved them.
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
    candidates = ctx.all_with_capability(capability)
    if not candidates:
        return PageHarvest(reason="Waiting on a connector that can list pages")

    best: PageHarvest | None = None
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
            return found
        # Pages, but none with a body worth reading. Keep it in case nothing
        # better turns up, and try the next source.
        best = best or found
        reasons.append(
            f"{name} lists {len(matched)} pages, but their text cannot be "
            f"read from it, so there is nothing here to analyse or rewrite."
        )

    if best is not None:
        # Everything that could serve offered only source files. Say what to
        # do about it: this is the Site Crawler's entire reason for existing.
        return PageHarvest(
            pages=[],
            source=best.source,
            listed=best.listed,
            reason=(
                f"{ctx.connector_name(best.source_slug)} lists "
                f"{len(best.pages)} pages, but their text cannot be read from "
                f"the repository on this site. Connect the Site Crawler, "
                f"which reads the published pages as a search engine sees "
                f"them."
            ),
        )

    return PageHarvest(reason=" ".join(reasons) if reasons else "No pages were found.")
