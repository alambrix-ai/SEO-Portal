"""A repository as a content system.

Shared by the GitHub and Bitbucket connectors. The two APIs differ in shape
but not in substance, so the workflow lives here and each vendor supplies six
primitives.

**The central decision: a write opens a pull request, it does not push to the
default branch.**

That is not caution for its own sake. A site whose content is in the code has
a deploy pipeline attached to its default branch — a commit there builds and
ships. An agent committing straight to it would put generated prose on a live
site with no review, no build check and no way to undo it except another
commit, and it would do so on the schedule the agent runs on. Meanwhile the
team already has the exact review mechanism this needs, and uses it for every
other change to the same files.

So the agent's output arrives as a branch and a pull request, described in the
PR body: which agent, what it changed, and why. The customer's own CI runs
against it. Their reviewers see a normal diff. Nothing reaches the site until
somebody merges — which means for these connectors "approved in the platform"
and "merged in the repository" are two separate gates, and that is the correct
number for a change to somebody's production website.

It also makes ``write_page`` honest about what it returns: True means "a pull
request is open", not "the page is live". The agent records a proposal; the
merge is a human act the platform never performs.
"""
from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass
from datetime import datetime

from app.connectors.base import content_files as cf
from app.connectors.base.connector import HealthReport
from app.core.config import settings
from app.connectors.base.interfaces import CmsConnector, RemotePage
from app.core.exceptions import ConnectorConfigError, ConnectorError
from app.core.logging import get_logger
from app.db.base import utcnow

log = get_logger(__name__)




@dataclass(slots=True)
class RepoFile:
    """One file in the repository, as the host describes it."""

    path: str
    #: Blob revision, where the host gives one. Needed by GitHub to update a
    #: file without clobbering a concurrent change.
    revision: str = ""
    size: int = 0


class GitHostConnector(CmsConnector):
    """A git host presented as a CMS.

    Subclasses implement the vendor calls; everything a caller sees is here.
    """

    #: Branch that gets deployed. Pull requests target it and are never
    #: committed to directly.
    default_branch_field = "branch"

    # ── Configuration ──────────────────────────────────────────────────────
    @property
    def repository(self) -> str:
        """``owner/repo``, however the operator wrote it.

        People paste what they have, which is the address bar: a full URL,
        sometimes with ``.git`` on the end. Only stripping slashes turned
        ``https://github.com/you/site`` into a request for
        ``/repos/https://github.com/you/site``, which the vendor answers with
        a 404 that blames the repository rather than the input.

        The shape is checked here too. Two non-empty segments is the whole
        rule, and failing on it locally is worth more than a 404 from a
        vendor that will not say what it disliked.
        """
        raw = self.credentials.require("repository").strip()
        for prefix in ("https://", "http://", "git@", "ssh://"):
            raw = raw.removeprefix(prefix)
        # An SSH remote is host:owner/repo — the colon comes before the first
        # slash, so it has to be handled before the host strip below.
        head = raw.split("/", 1)[0]
        if ":" in head:
            raw = raw.split(":", 1)[1]
        # Drop a host if one survived the prefix strip.
        elif "/" in raw and "." in head:
            raw = raw.split("/", 1)[1]
        raw = raw.removesuffix(".git").strip("/")

        parts = [p for p in raw.split("/") if p]
        if len(parts) != 2:
            raise ConnectorConfigError(
                f"Repository should be owner/repo — got {raw!r}. Paste the two "
                "names, not the full URL: for https://github.com/acme/site "
                "that is acme/site."
            )
        return "/".join(parts)

    @property
    def branch(self) -> str:
        """The deploy branch, as configured. No default.

        Guessing ``main`` was wrong for two reasons: a repository on ``master``
        or ``trunk`` would have every read fail with a confusing 404, and a
        repository that *has* a ``main`` alongside the branch that actually
        deploys would have pull requests opened against the wrong one. The
        field is required, so this cannot be empty in practice — and if it is,
        saying so beats picking one.
        """
        return self.credentials.require(self.default_branch_field)

    @property
    def content_root(self) -> str:
        """Where the content lives. Empty means the whole repository.

        Asked for rather than guessed: ``content/`` is right for Hugo,
        ``src/pages/`` for Astro, ``_posts/`` for Jekyll and the repository
        root for a hand-written site, and treating a framework repository as
        all-content turns every component into a page to be rewritten.
        """
        return (self.credentials.get("contentPath") or "").strip("/")

    @property
    def url_prefix(self) -> str:
        return (self.credentials.get("urlPrefix") or "").strip("/")

    def url_for(self, path: str) -> str:
        return cf.path_to_url(path, content_root=self.content_root, url_prefix=self.url_prefix)

    # ── Vendor primitives ──────────────────────────────────────────────────
    @abstractmethod
    def _list_files(self) -> list[RepoFile]:
        """Every file under the content root, one call if the host allows it."""

    @abstractmethod
    def _read_file(self, path: str) -> tuple[str, str]:
        """Return ``(text, revision)`` for a file on the deploy branch."""

    @abstractmethod
    def _branch_head(self) -> str:
        """The commit the deploy branch currently points at."""

    @abstractmethod
    def _create_branch(self, name: str, *, from_commit: str) -> None:
        ...

    @abstractmethod
    def _commit_file(
        self, *, branch: str, path: str, text: str, message: str, revision: str
    ) -> None:
        ...

    @abstractmethod
    def _open_pull_request(self, *, branch: str, title: str, body: str) -> str:
        """Open a PR from ``branch`` onto the deploy branch; return its URL."""

    @abstractmethod
    def _probe(self) -> str:
        """Cheapest call that proves the credentials work. Returns a detail line."""

    # ── Reads ──────────────────────────────────────────────────────────────
    def list_pages(self, *, limit: int = 100, path_prefix: str = "") -> list[RemotePage]:
        """Content files in the repository, as pages.

        The bodies are not fetched here. A listing of two hundred pages would
        otherwise be two hundred file reads, and the SEO pipeline reads the
        ones it wants to work on individually.
        """
        pages: list[RemotePage] = []
        configured = bool(self.content_root)
        for entry in self._list_files():
            if not cf.is_content_path(entry.path, content_root_configured=configured):
                continue
            url = self.url_for(entry.path)
            if path_prefix and not url.startswith(path_prefix):
                continue
            pages.append(
                RemotePage(
                    remote_id=entry.path,
                    url=url,
                    # Derived from the filename until the body is read; the
                    # real title comes from the frontmatter in read_page.
                    title=cf.title_for(cf.parse(entry.path, "")),
                    word_count=0,
                    metadata={
                        "path": entry.path,
                        "branch": self.branch,
                        "size": entry.size,
                        # False for source files: listed, because the URL
                        # inventory is useful, but never rewritten.
                        "writable": cf.is_rewritable(entry.path),
                    },
                )
            )
            if len(pages) >= limit:
                break
        return pages

    def describe_content(self) -> str:
        """Why the listing was empty, in terms the operator can act on.

        Called when ``list_pages`` finds nothing. "0 pages synced" reported as
        a success is the exact silence this platform keeps eliminating: the
        repository is connected, the token works, and the agent has quietly
        concluded the site has no content. Usually it means the pages are
        ``.tsx`` files somewhere this has not been told to look.

        The extension histogram is the useful part — it is the difference
        between "your repository is empty" and "your pages are .tsx and they
        live in src/pages".
        """
        files = self._list_files()
        if not files:
            root = self.content_root or "the repository root"
            return (
                f"{self.repository} has no files under {root} on branch "
                f"{self.branch}. Check the branch and the content folder."
            )

        from collections import Counter

        extensions = Counter(
            f".{entry.path.rsplit('.', 1)[-1].lower()}" if "." in entry.path else "(no extension)"
            for entry in files
        )
        commonest = ", ".join(f"{ext} ({count})" for ext, count in extensions.most_common(4))
        hint = (
            "Set the content folder to where your pages live — src/pages or "
            "app for Next, src/routes for SvelteKit, src/pages for Astro."
            if any(ext in cf.FRAMEWORK_EXTENSIONS for ext in extensions)
            else "This connector reads Markdown, MDX, HTML, Astro and "
            "framework page files. If the site's content is generated from "
            "somewhere else, that source is what needs connecting."
        )
        return (
            f"Walked {len(files)} files in {self.repository} on {self.branch} "
            f"and none of them look like pages. The commonest were "
            f"{commonest}. {hint}"
        )

    def read_page(self, remote_id: str) -> RemotePage:
        text, revision = self._read_file(remote_id)
        parsed = cf.parse(remote_id, text)
        return RemotePage(
            remote_id=remote_id,
            url=self.url_for(remote_id),
            title=cf.title_for(parsed),
            body=parsed.body,
            word_count=cf.word_count(parsed.body),
            schema_types=cf.schema_types(parsed.body),
            metadata={
                "path": remote_id,
                "branch": self.branch,
                "revision": revision,
                "has_frontmatter": parsed.has_frontmatter,
                "frontmatter": parsed.frontmatter,
                "writable": cf.is_rewritable(remote_id),
            },
        )

    # ── Writes ─────────────────────────────────────────────────────────────
    def write_page(self, remote_id: str, *, body: str, title: str | None = None) -> bool:
        """Open a pull request that rewrites one content file.

        The frontmatter is read, kept, and only the title is edited within it —
        see :mod:`app.connectors.base.content_files` for why replacing the
        whole file would break the customer's build.

        Refused outright for source files. A ``.jsx`` or ``.vue`` page is a
        component tree, and on most modern sites the page file is a
        composition with no copy in it at all — the words live in a dozen
        components. Replacing its body with prose would replace working code,
        so the write stops here rather than in a pull request that breaks the
        build.
        """
        if not cf.is_rewritable(remote_id):
            raise ConnectorError(
                f"{remote_id} is source, not prose — its body is code, and "
                "replacing it would break the build. Connect the Site Crawler "
                "to analyse what this page actually publishes; the copy itself "
                "lives in the components it renders."
            )
        current, revision = self._read_file(remote_id)
        parsed = cf.parse(remote_id, current)
        if parsed.body.strip() == body.strip() and title in (None, cf.title_for(parsed)):
            log.info("%s: %s already matches; no pull request opened", self.slug, remote_id)
            return False

        parsed.body = body if body.startswith("\n") else f"\n{body}"
        if title:
            cf.set_frontmatter_value(parsed, "title", title)

        return self._propose(
            path=remote_id,
            text=parsed.render(),
            revision=revision,
            summary=f"Rewrite {self.url_for(remote_id)}",
            detail=(
                "The body copy of this page was rewritten to close a measured "
                "semantic gap. Frontmatter is unchanged apart from the title."
            ),
        )

    def inject_schema(self, remote_id: str, *, json_ld: dict) -> bool:
        """Open a pull request that adds or replaces the page's JSON-LD.

        Prose files only, for the same reason as ``write_page``: a
        ``<script>`` block appended to a ``.jsx`` file is a syntax error, not
        structured data.
        """
        if not cf.is_rewritable(remote_id):
            raise ConnectorError(
                f"{remote_id} is source, not prose — a script block appended "
                "to it would not compile. Add the JSON-LD in the component "
                "that renders this route."
            )
        current, revision = self._read_file(remote_id)
        parsed = cf.parse(remote_id, current)
        before = parsed.body
        cf.inject_json_ld(parsed, json_ld)
        if parsed.body == before:
            return False

        graph = json_ld.get("@type") or "structured data"
        return self._propose(
            path=remote_id,
            text=parsed.render(),
            revision=revision,
            summary=f"Add {graph} markup to {self.url_for(remote_id)}",
            detail=(
                "A JSON-LD block was added or replaced so this page is eligible "
                "for the corresponding rich result."
            ),
        )

    def _propose(
        self, *, path: str, text: str, revision: str, summary: str, detail: str
    ) -> bool:
        """Branch, commit, open a pull request. The whole write path."""
        head = self._branch_head()
        branch = self._branch_name(path)

        try:
            self._create_branch(branch, from_commit=head)
        except ConnectorConfigError:
            # Retrying with a different branch name cannot fix a missing
            # credential; it just fails twice as slowly.
            raise
        except ConnectorError as exc:
            # A branch left behind by an earlier run that nobody merged. Reusing
            # it would stack unrelated edits into one pull request, so this run
            # gets its own — and the stale one stays for a human to close.
            log.info("%s: branch %s exists (%s); using a fresh one", self.slug, branch, exc)
            branch = self._branch_name(path, unique=True)
            self._create_branch(branch, from_commit=head)

        self._commit_file(
            branch=branch,
            path=path,
            text=text,
            message=f"{summary}\n\nOpened by AutoMarket AI.",
            revision=revision,
        )
        url = self._open_pull_request(
            branch=branch,
            title=summary,
            body=(
                f"{detail}\n\n"
                f"- Page: `{self.url_for(path)}`\n"
                f"- File: `{path}`\n"
                f"- Branch: `{branch}` onto `{self.branch}`\n\n"
                "Opened automatically by AutoMarket AI. Nothing is live until "
                "this is merged — review it as you would any other change to "
                "this file.\n"
            ),
        )
        log.info("%s: opened %s", self.slug, url or branch)
        return True

    def _branch_name(self, path: str, *, unique: bool = False) -> str:
        slug = path.rsplit("/", 1)[-1].rsplit(".", 1)[0]
        safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in slug).strip("-").lower()
        name = f"automarket/{safe or 'content'}"
        if unique:
            name = f"{name}-{utcnow().strftime('%Y%m%d%H%M%S')}"
        return name

    # ── Health ─────────────────────────────────────────────────────────────
    def check_health(self) -> HealthReport:
        try:
            detail = self._probe()
        except ConnectorError as exc:
            return HealthReport(ok=False, detail=str(exc), checked_at=utcnow())
        return HealthReport(ok=True, detail=detail, checked_at=utcnow())

    @staticmethod
    def _stamp(value: str | datetime | None) -> str:
        if isinstance(value, datetime):
            return value.isoformat()
        return str(value or "")
