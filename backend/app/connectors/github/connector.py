"""GitHub connector — a repository as the content system.

For a site with no CMS, whose pages are Markdown or HTML committed next to the
code, this is the CMS. Writes arrive as pull requests rather than commits to
the deploy branch; the reasoning is in
:mod:`app.connectors.base.git_base`.

Auth is a fine-grained personal access token or a GitHub App installation
token, needing **Contents: read and write** and **Pull requests: write** on
the one repository. Nothing here needs organisation-wide access, and asking
for a classic token with `repo` scope — which grants everything on every
repository the user can see — would be asking for far more than the job
requires.
"""
from __future__ import annotations

import base64
import binascii

from app.connectors.base.connector import Capability, ConnectorSpec
from app.connectors.base.credentials import secret, text
from app.connectors.base.git_base import GitHostConnector, RepoFile
from app.core.config import settings
from app.core.exceptions import ConnectorError
from app.core.logging import get_logger

log = get_logger(__name__)

API = "https://api.github.com"


class GitHubConnector(GitHostConnector):
    spec = ConnectorSpec(
        slug="github",
        name="GitHub",
        category="CMS",
        description=(
            "Treat a GitHub repository as the CMS: read Markdown, MDX and HTML "
            "pages, and open a pull request for every rewrite."
        ),
        fields=(
            text("repository", "Repository", "your-org/your-website"),
            secret(
                "token",
                "Personal access token",
                "github_pat_… or ghs_…",
                help_text=(
                    "A fine-grained personal access token with Contents and "
                    "Pull requests set to Read and write, for this "
                    "repository. Classic tokens work with the repo scope. An "
                    "installation token (ghs_…) is accepted too."
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
                    "Where the pages live — content for Hugo, src/pages for "
                    "Astro or Next, _posts for Jekyll. Leave empty if the "
                    "whole repository is content."
                ),
            ),
            text(
                "urlPrefix",
                "URL prefix",
                "",
                required=False,
                help_text="Prepended to every derived page URL, e.g. a locale segment.",
            ),
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
            "A fine-grained personal access token or a GitHub App "
            "installation token, scoped to this one repository.",
            "Permissions: Contents read and write, and Pull requests "
            "write. A read-only token is refused at connect time.",
        ),
        docs_url="https://docs.github.com/en/rest",
        base_url=API,
    )

    def auth_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.credentials.require('token')}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    # ── Diagnosis ──────────────────────────────────────────────────────────
    def diagnose(self, error: Exception) -> str | None:
        """Explain GitHub's answers, particularly its 404.

        GitHub returns **404 rather than 403** for a repository the token
        cannot see. That is deliberate on their part — a 403 would confirm the
        repository exists — and it means the same status covers four different
        problems, only one of which is a wrong name. Forwarding it verbatim
        tells the operator to check the thing that is most likely correct.
        """
        text = str(error)
        repo = self.credentials.get("repository", "")

        if "404" in text:
            return (
                f"GitHub cannot see {repo!r}. It answers 404 rather than 403 for "
                "a repository a token has no access to, so this is one of four "
                "things:\n\n"
                "1. The repository is private and this token was never granted "
                "it. A fine-grained token lists its repositories explicitly — "
                "check that this one is in the list.\n"
                "2. The token belongs to a different account or organisation "
                "than the repository.\n"
                "3. The token is a fine-grained token on an organisation "
                "repository and an owner has not approved it yet — it stays "
                "invisible until they do.\n"
                "4. The owner or repository name is wrong. Enter it as "
                "owner/repo, not as a URL."
            )
        if "401" in text:
            return (
                "GitHub rejected the token itself. Check it has not expired or "
                "been revoked — fine-grained tokens expire, and the default is "
                "30 days."
            )
        if "403" in text:
            return (
                "GitHub accepted the token but refused the action. Grant the "
                "token Contents: read and write, and Pull requests: write, on "
                f"{repo!r}. If the repository belongs to an organisation, an "
                "owner may also need to approve the token."
            )
        return None

    # ── Vendor primitives ──────────────────────────────────────────────────
    def _list_files(self) -> list[RepoFile]:
        """One recursive tree call, which is why GitHub is the cheap case."""
        data = self.request(
            "GET",
            f"/repos/{self.repository}/git/trees/{self.branch}",
            params={"recursive": "1"},
        )
        if data.get("truncated"):
            # GitHub caps the tree response. Saying so beats silently
            # reporting on part of a repository as though it were all of it.
            log.warning(
                "github: the tree for %s was truncated; narrow the content folder",
                self.repository,
            )
        root = self.content_root
        files: list[RepoFile] = []
        for entry in (data.get("tree") or [])[: settings.repo_max_files]:
            if entry.get("type") != "blob":
                continue
            path = str(entry.get("path") or "")
            if root and not path.startswith(f"{root}/"):
                continue
            files.append(
                RepoFile(
                    path=path,
                    revision=str(entry.get("sha") or ""),
                    size=int(entry.get("size") or 0),
                )
            )
        return files

    def _read_file(self, path: str) -> tuple[str, str]:
        row = self.request(
            "GET", f"/repos/{self.repository}/contents/{path}", params={"ref": self.branch}
        )
        if row.get("encoding") != "base64" or not row.get("content"):
            raise ConnectorError(f"GitHub returned no readable content for {path}")
        try:
            raw = base64.b64decode(row["content"])
        except (binascii.Error, ValueError) as exc:
            raise ConnectorError(f"GitHub returned undecodable content for {path}") from exc
        try:
            text_content = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            # A binary file matched the content extensions. Rewriting it would
            # corrupt it, so this stops here rather than guessing an encoding.
            raise ConnectorError(f"{path} is not UTF-8 text") from exc
        return text_content, str(row.get("sha") or "")

    def _branch_head(self) -> str:
        ref = self.request("GET", f"/repos/{self.repository}/git/ref/heads/{self.branch}")
        sha = ((ref or {}).get("object") or {}).get("sha")
        if not sha:
            raise ConnectorError(f"Branch {self.branch!r} not found in {self.repository}")
        return str(sha)

    def _create_branch(self, name: str, *, from_commit: str) -> None:
        self.request(
            "POST",
            f"/repos/{self.repository}/git/refs",
            json_body={"ref": f"refs/heads/{name}", "sha": from_commit},
        )

    def _commit_file(
        self, *, branch: str, path: str, text: str, message: str, revision: str
    ) -> None:
        body = {
            "message": message,
            "content": base64.b64encode(text.encode()).decode(),
            "branch": branch,
        }
        # The blob sha makes this a compare-and-set: if somebody edited the
        # file since it was read, GitHub rejects the write instead of
        # overwriting their change.
        if revision:
            body["sha"] = revision
        self.request("PUT", f"/repos/{self.repository}/contents/{path}", json_body=body)

    def _open_pull_request(self, *, branch: str, title: str, body: str) -> str:
        row = self.request(
            "POST",
            f"/repos/{self.repository}/pulls",
            json_body={"title": title, "head": branch, "base": self.branch, "body": body},
        )
        return str((row or {}).get("html_url") or "")

    def _probe(self) -> str:
        row = self.request("GET", f"/repos/{self.repository}")
        permissions = (row or {}).get("permissions") or {}
        if not permissions.get("push", False):
            # Read-only access looks fine until the first rewrite fails hours
            # later, so it is reported as unhealthy now.
            raise ConnectorError(
                f"The token can read {self.repository} but not write to it — "
                "grant Contents: read and write, and Pull requests: write."
            )
        return f"{row.get('full_name')} reachable on {self.branch}"


CONNECTOR_CLASS = GitHubConnector
