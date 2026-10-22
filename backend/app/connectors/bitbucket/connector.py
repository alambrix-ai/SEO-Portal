"""Bitbucket Cloud connector — a repository as the content system.

The GitHub connector's sibling, and the same design: writes arrive as pull
requests rather than commits to the deploy branch. See
:mod:`app.connectors.base.git_base` for why.

Two places where Bitbucket's API differs from GitHub's in ways that matter,
rather than in ways that are merely cosmetic:

* **There is no recursive tree endpoint.** ``/src`` lists one directory at a
  time, so the walk is breadth-first and bounded. That is the reason a content
  folder matters more here than on GitHub: pointed at the root of a large
  repository this makes many more calls.

* **Committing is a form post, not JSON.** The file contents are sent as a
  form field named after the path. That is what the ``form`` parameter on
  ``request`` exists for.

Auth is a repository or workspace access token (sent as a bearer token), or
an Atlassian account email and API token (sent as Basic). App passwords used
to be the second option; Atlassian finished deprecating them on 28 July 2026,
so the fields name API tokens now. A token scoped to the one repository is
the smaller grant and the one to prefer.
"""
from __future__ import annotations

import base64
import re
from collections import deque

from app.connectors.base.connector import Capability, ConnectorSpec, OAuthBinding, OAuthChoice
from app.connectors.base.credentials import from_platform_sign_in, secret, text
from app.connectors.base.git_base import GitHostConnector, RepoFile
from app.connectors.base.oauth import OAuthTokenMixin
from app.core.config import settings
from app.core.exceptions import ConnectorError
from app.core.logging import get_logger

log = get_logger(__name__)

API = "https://api.bitbucket.org"


def parse_repositories(data: dict) -> list[OAuthChoice]:
    """Repositories out of ``GET /2.0/repositories?role=member``."""
    out: list[OAuthChoice] = []
    for row in data.get("values") or []:
        full = str(row.get("full_name") or "")
        if not full:
            continue
        branch = str((row.get("mainbranch") or {}).get("name") or "")
        workspace = str((row.get("workspace") or {}).get("name") or full.split("/", 1)[0])
        out.append(
            OAuthChoice(
                id=full,
                label=full,
                detail=" · ".join(
                    part for part in (f"Workspace {workspace}",
                                      f"deploys from {branch}" if branch else "") if part
                ),
                values={"repository": full, **({"branch": branch} if branch else {})},
            )
        )
    return out


class BitbucketConnector(OAuthTokenMixin, GitHostConnector):
    spec = ConnectorSpec(
        slug="bitbucket",
        name="Bitbucket",
        category="CMS",
        description=(
            "Treat a Bitbucket repository as the CMS: read Markdown, MDX and "
            "HTML pages, and open a pull request for every rewrite."
        ),
        fields=(
            text("repository", "Repository", "your-workspace/your-website"),
            secret(
                "token",
                "Access token or API token",
                "",
                help_text=(
                    "Either a repository access token (Repository settings → "
                    "Access tokens — the smallest grant, and the one to "
                    "prefer) or an Atlassian API token. App passwords were "
                    "deprecated on 28 July 2026 and no longer authenticate."
                ),
            ),
            text(
                "username",
                "Atlassian account email",
                "you@yourcompany.com",
                required=False,
                help_text=(
                    "Only with an API token, which authenticates as Basic "
                    "with your email as the username. Leave it empty for a "
                    "repository or workspace access token — those are sent "
                    "as a bearer token and no username applies."
                ),
            ),
            text(
                "branch",
                "Deploy branch",
                "main",
                required=False,
                help_text=(
                    "The branch your site deploys from. Pull requests target it. "
                    "Leave blank to use your repository's main branch."
                ),
            ),
            text(
                "contentPath",
                "Content folder",
                "content",
                required=False,
                help_text=(
                    "Where the pages live. Worth setting here: Bitbucket has no "
                    "recursive listing, so a narrow folder is markedly faster."
                ),
            ),
            text("urlPrefix", "URL prefix", "", required=False),
            text(
                "articlesPath",
                "Articles folder",
                "content/blog",
                required=False,
                help_text=(
                    "Where new articles are added as Markdown files, each in its own pull "
                    "request, for example content/blog or _posts. Leave empty to use the "
                    "content folder."
                ),
            ),
        ),
        capabilities=frozenset(
            {
                Capability.LIST_PAGES,
                Capability.READ_PAGE,
                Capability.WRITE_PAGE,
                Capability.INJECT_SCHEMA,
                Capability.CREATE_PAGE,
            }
        ),
        requirements=(
            "A repository or workspace access token, or an Atlassian "
            "account email with an API token. App passwords stopped working "
            "on 28 July 2026.",
            "Scopes: repository:write and pullrequest:write.",
        ),
        docs_url="https://developer.atlassian.com/cloud/bitbucket/rest/",
        base_url=API,
        # Bitbucket takes the scopes from the OAuth consumer's own settings
        # (Repositories: Write, Pull requests: Write); the authorize request
        # carries none. These are listed for Portal Admin and the docs.
        oauth=OAuthBinding(
            provider="bitbucket",
            scopes=("repository:write", "pullrequest:write"),
            platform_fields=frozenset({"token", "username"}),
            token_fields=frozenset({"token", "username", "accessToken", "refreshToken"}),
            access_summary=(
                "Lets agents read the pages in the repository you pick and open a pull "
                "request for each change. Nothing is merged or published without you."
            ),
            choice_noun="repository",
            choice_fields=("repository",),
        ),
    )

    token_url = "https://bitbucket.org/site/oauth2/access_token"

    @property
    def _signed_in(self) -> bool:
        return from_platform_sign_in(self.credentials.values) and not self.credentials.get("token")

    def auth_headers(self) -> dict[str, str]:
        if self._signed_in:
            return {"Authorization": f"Bearer {self.access_token}"}
        token = self.credentials.require("token")
        user = self.credentials.get("username")
        if user:
            encoded = base64.b64encode(f"{user}:{token}".encode()).decode()
            return {"Authorization": f"Basic {encoded}"}
        return {"Authorization": f"Bearer {token}"}

    # ── Sign-in ────────────────────────────────────────────────────────────
    def list_oauth_choices(self) -> list[OAuthChoice]:
        """Repositories the signed-in account is a member of."""
        choices: list[OAuthChoice] = []
        page: str | None = "/2.0/repositories"
        params: dict | None = {"role": "member", "pagelen": 100, "sort": "-updated_on"}
        for _ in range(10):
            if not page:
                break
            data = self.request("GET", page, params=params) or {}
            choices.extend(parse_repositories(data))
            page, params = data.get("next"), None
        return choices

    @property
    def _repo_path(self) -> str:
        return f"/2.0/repositories/{self.repository}"

    #: The repository's main branch, read once when no branch is configured.
    _default_branch: str = ""

    @property
    def branch(self) -> str:
        """The configured deploy branch, else the repository's main branch.

        Blank is not a guess at ``main``: the repository object carries
        ``mainbranch.name`` (``GET /2.0/repositories/{workspace}/{repo_slug}``).
        https://developer.atlassian.com/cloud/bitbucket/rest/api-group-repositories/#api-repositories-workspace-repo-slug-get
        """
        configured = (self.credentials.get(self.default_branch_field) or "").strip()
        if configured:
            return configured
        if not self._default_branch:
            row = self.request("GET", self._repo_path, params={"fields": "mainbranch.name"}) or {}
            found = str((row.get("mainbranch") or {}).get("name") or "").strip()
            if not found:
                raise ConnectorError(
                    f"Bitbucket did not report a main branch for {self.repository}. "
                    "Enter the deploy branch on this integration."
                )
            self._default_branch = found
        return self._default_branch

    # ── Diagnosis ──────────────────────────────────────────────────────────
    def diagnose(self, error: Exception) -> str | None:
        text = str(error)
        repo = self.credentials.get("repository", "")
        if "404" in text:
            return (
                f"Bitbucket cannot see {repo!r}. Enter it as workspace/repo — "
                "the workspace ID, not its display name, and not the full URL. "
                "If the repository is private, check the token was created in "
                "that workspace and has repository:read at minimum."
            )
        if "401" in text:
            return (
                "Bitbucket rejected the credentials. With an API token the "
                "Atlassian account email is required as well; with a "
                "repository or workspace access token it must be left empty. "
                "If this used to work with an app password, that is why — "
                "they were deprecated on 28 July 2026."
            )
        if "403" in text:
            return (
                "Bitbucket accepted the credentials but refused the action. The "
                "token needs repository:write and pullrequest:write."
            )
        return None

    # ── Vendor primitives ──────────────────────────────────────────────────
    def _list_files(self) -> list[RepoFile]:
        """Breadth-first, because ``/src`` lists one directory per call."""
        files: list[RepoFile] = []
        queue: deque[tuple[str, int]] = deque([(self.content_root, 0)])

        while queue and len(files) < settings.repo_max_files:
            directory, depth = queue.popleft()
            if depth > settings.repo_max_depth:
                continue
            page: str | None = f"{self._repo_path}/src/{self.branch}/{directory}".rstrip("/")
            params: dict | None = {"pagelen": 100}
            while page and len(files) < settings.repo_max_files:
                data = self.request("GET", page, params=params)
                for entry in (data or {}).get("values") or []:
                    kind = entry.get("type")
                    path = str(entry.get("path") or "")
                    if kind == "commit_directory":
                        queue.append((path, depth + 1))
                    elif kind == "commit_file":
                        files.append(
                            RepoFile(
                                path=path,
                                # Bitbucket has no per-blob revision to
                                # compare-and-set against, so writes are
                                # last-writer-wins. The pull request is what
                                # makes that safe: a clobbered edit shows up
                                # in the diff before anything is merged.
                                revision="",
                                size=int(entry.get("size") or 0),
                            )
                        )
                # The next-page link is absolute; params are already in it.
                page, params = (data or {}).get("next"), None
        return files

    def _read_file(self, path: str) -> tuple[str, str]:
        raw = self.request(
            "GET", f"{self._repo_path}/src/{self.branch}/{path}", expect_json=False
        )
        if not isinstance(raw, str):
            raise ConnectorError(f"Bitbucket returned no readable content for {path}")
        return raw, ""

    def _branch_head(self) -> str:
        row = self.request("GET", f"{self._repo_path}/refs/branches/{self.branch}")
        commit = ((row or {}).get("target") or {}).get("hash")
        if not commit:
            raise ConnectorError(f"Branch {self.branch!r} not found in {self.repository}")
        return str(commit)

    def _create_branch(self, name: str, *, from_commit: str) -> None:
        self.request(
            "POST",
            f"{self._repo_path}/refs/branches",
            json_body={"name": name, "target": {"hash": from_commit}},
        )

    def _commit_file(
        self, *, branch: str, path: str, text: str, message: str, revision: str
    ) -> None:
        # The path *is* the field name — that is how this endpoint works.
        self.request(
            "POST",
            f"{self._repo_path}/src",
            form={path: text, "message": message, "branch": branch},
        )

    def _open_pull_request(self, *, branch: str, title: str, body: str) -> str:
        row = self.request(
            "POST",
            f"{self._repo_path}/pullrequests",
            json_body={
                "title": title,
                "description": body,
                "source": {"branch": {"name": branch}},
                "destination": {"branch": {"name": self.branch}},
                "close_source_branch": True,
            },
        )
        links = (row or {}).get("links") or {}
        return str((links.get("html") or {}).get("href") or "")

    def _probe(self) -> str:
        row = self.request("GET", self._repo_path)
        if not self._default_branch:
            self._default_branch = str(((row or {}).get("mainbranch") or {}).get("name") or "").strip()
        name = (row or {}).get("full_name") or self.repository
        # Bitbucket does not report effective permissions on the repository
        # object, so writability cannot be checked without writing. The token
        # scopes are the operator's to get right; a failed pull request will
        # say so plainly when it happens.
        return f"{name} reachable on {self.branch}"

    # ── Live checks ────────────────────────────────────────────────────────
    def _pull_number(self, url: str, branch: str) -> str:
        """The pull request id from its link, else looked up by source branch."""
        match = re.search(r"/pull-requests/(\d+)", url or "")
        if match:
            return match.group(1)
        data = self.request(
            "GET",
            f"{self._repo_path}/pullrequests",
            params={"q": f'source.branch.name="{branch}"', "state": "OPEN"},
        ) or {}
        rows = data.get("values") or []
        return str(rows[0].get("id") or "") if rows else ""

    def _read_pull_request(self, number: str) -> str:
        """The pull request's state, lower case (``open``, ``declined``, ``merged``)."""
        row = self.request("GET", f"{self._repo_path}/pullrequests/{number}") or {}
        return str(row.get("state") or "").lower()

    def _close_pull_request(self, number: str) -> None:
        """Decline it: ``POST …/pullrequests/{id}/decline``."""
        self.request("POST", f"{self._repo_path}/pullrequests/{number}/decline")

    def _delete_branch(self, name: str) -> None:
        """``DELETE …/refs/branches/{name}``; already gone is fine."""
        try:
            self.request("DELETE", f"{self._repo_path}/refs/branches/{name}")
        except ConnectorError as exc:
            if getattr(exc, "http_status", None) != 404:
                raise

    def verify_steps(self) -> list:
        """Read the repository, its deploy branch and content; open and decline one PR.

        The same checks as GitHub's (see ``git_verify_steps``): branch,
        commit and pull request through this connector's own primitives, then
        the pull request is declined and the branch deleted, whatever failed
        in between. Needs a test repository: a pull request notifies
        reviewers and runs pipelines.
        """
        from app.connectors.base import verify_helpers as vh
        from app.connectors.github.connector import git_verify_steps

        return git_verify_steps(self, vh)


CONNECTOR_CLASS = BitbucketConnector
