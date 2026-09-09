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
from collections import deque

from app.connectors.base.connector import Capability, ConnectorSpec
from app.connectors.base.credentials import secret, text
from app.connectors.base.git_base import GitHostConnector, RepoFile
from app.core.config import settings
from app.core.exceptions import ConnectorError
from app.core.logging import get_logger

log = get_logger(__name__)

API = "https://api.bitbucket.org"



class BitbucketConnector(GitHostConnector):
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
                help_text="The branch your site deploys from. Pull requests target it.",
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
        ),
        capabilities=frozenset(
            {
                Capability.LIST_PAGES,
                Capability.READ_PAGE,
                Capability.WRITE_PAGE,
                Capability.INJECT_SCHEMA,
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
    )

    def auth_headers(self) -> dict[str, str]:
        token = self.credentials.require("token")
        user = self.credentials.get("username")
        if user:
            encoded = base64.b64encode(f"{user}:{token}".encode()).decode()
            return {"Authorization": f"Basic {encoded}"}
        return {"Authorization": f"Bearer {token}"}

    @property
    def _repo_path(self) -> str:
        return f"/2.0/repositories/{self.repository}"

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
        name = (row or {}).get("full_name") or self.repository
        # Bitbucket does not report effective permissions on the repository
        # object, so writability cannot be checked without writing. The token
        # scopes are the operator's to get right; a failed pull request will
        # say so plainly when it happens.
        return f"{name} reachable on {self.branch}"


CONNECTOR_CLASS = BitbucketConnector
