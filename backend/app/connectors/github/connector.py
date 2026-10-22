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
import re
import time

import httpx

from app.connectors.base.connector import Capability, ConnectorSpec, OAuthBinding, OAuthChoice
from app.connectors.base.credentials import from_platform_sign_in, secret, text
from app.connectors.base.git_base import GitHostConnector, RepoFile
from app.connectors.base.oauth_providers import platform_setting
from app.core.config import settings
from app.core.exceptions import ConnectorConfigError, ConnectorError
from app.core.logging import get_logger

log = get_logger(__name__)

API = "https://api.github.com"
#: Settings that mint installation tokens, besides the app's client id/secret.
APP_SETTINGS = ("github_app_slug", "github_app_id", "github_app_private_key")
#: What the platform's GitHub App must be granted for this connector.
APP_PERMISSIONS = {"contents": "write", "pull_requests": "write"}


def app_private_key() -> str:
    """The app's PEM key, accepting single-line values with ``\\n`` escapes."""
    raw = platform_setting("github_app_private_key")
    if "\\n" in raw and "\n" not in raw:
        raw = raw.replace("\\n", "\n")
    return raw.strip()


def app_jwt(now: float | None = None) -> str:
    """A ten-minute-at-most RS256 JWT identifying the platform's GitHub App.

    ``iat`` is set a minute in the past against clock drift, ``exp`` nine
    minutes ahead (GitHub refuses more than ten), ``iss`` is the App ID.
    """
    import jwt

    app_id = platform_setting("github_app_id")
    key = app_private_key()
    if not app_id or not key:
        raise ConnectorConfigError(
            "GitHub was connected by installing the platform's app, but the app is no "
            "longer set up here. Ask your platform administrator, or reconnect under Advanced."
        )
    issued = int(now or time.time())
    try:
        return jwt.encode({"iat": issued - 60, "exp": issued + 540, "iss": app_id}, key, algorithm="RS256")
    except (ValueError, TypeError, jwt.PyJWTError) as exc:
        raise ConnectorConfigError(
            "The platform's GitHub App key could not be read. Ask your platform administrator."
        ) from exc


def parse_installation_repositories(data: dict) -> list[OAuthChoice]:
    """Repositories out of ``GET /installation/repositories``."""
    out: list[OAuthChoice] = []
    for row in data.get("repositories") or []:
        full = str(row.get("full_name") or "")
        if not full or row.get("archived"):
            continue
        branch = str(row.get("default_branch") or "")
        out.append(
            OAuthChoice(
                id=full,
                label=full,
                detail=" · ".join(
                    part for part in ("Private" if row.get("private") else "Public",
                                      f"deploys from {branch}" if branch else "") if part
                ),
                values={"repository": full, **({"branch": branch} if branch else {})},
            )
        )
    return out


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
                required=False,
                help_text=(
                    "The branch your site deploys from. Pull requests target it. "
                    "Leave blank to use your repository's default branch."
                ),
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
            "A fine-grained personal access token or a GitHub App "
            "installation token, scoped to this one repository.",
            "Permissions: Contents read and write, and Pull requests "
            "write. A read-only token is refused at connect time.",
        ),
        docs_url="https://docs.github.com/en/rest",
        base_url=API,
        # A GitHub App rather than an OAuth App: the person picks the
        # repositories at install time, the app's own permissions are only
        # Contents and Pull requests (read and write), and the tokens used
        # are hour-long installation tokens limited to those repositories.
        # An OAuth App would need the classic `repo` scope — every private
        # repository the person can reach.
        oauth=OAuthBinding(
            provider="github",
            platform_fields=frozenset({"token"}),
            token_fields=frozenset({"token"}),
            requires_settings=APP_SETTINGS,
            button_label="Install on GitHub",
            access_summary=(
                "You choose which repositories the Synaprank app can see. It reads "
                "their pages and opens a pull request for each change; nothing is "
                "merged or published without you."
            ),
            choice_noun="repository",
            choice_fields=("repository",),
        ),
    )

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self._installation_token = ""
        self._installation_expires = 0.0
        self._installation_permissions: dict[str, str] = {}
        self._default_branch = ""

    @property
    def branch(self) -> str:
        """The configured deploy branch, else the repository's default branch.

        Blank is not a guess at ``main``: the default branch is read from
        ``GET /repos/{owner}/{repo}`` (``default_branch``), once per instance.
        https://docs.github.com/en/rest/repos/repos#get-a-repository
        """
        configured = (self.credentials.get(self.default_branch_field) or "").strip()
        if configured:
            return configured
        if not self._default_branch:
            row = self.request("GET", f"/repos/{self.repository}") or {}
            found = str(row.get("default_branch") or "").strip()
            if not found:
                raise ConnectorError(
                    f"GitHub did not report a default branch for {self.repository}. "
                    "Enter the deploy branch on this integration."
                )
            self._default_branch = found
        return self._default_branch

    @property
    def _installation_id(self) -> str:
        """Set when connected by installing the platform's GitHub App."""
        if self.credentials.get("token") or not from_platform_sign_in(self.credentials.values):
            return ""
        return (self.credentials.get("installationId") or "").strip()

    def installation_token(self, now: float | None = None) -> str:
        """An installation access token, minted with the app's JWT and kept
        until five minutes before its hour is up."""
        current = now or time.time()
        if self._installation_token and current < self._installation_expires:
            return self._installation_token
        installation = self._installation_id
        if not installation.isdigit():
            raise ConnectorConfigError("GitHub has no installation to work with. Install the app again.")
        try:
            with httpx.Client(timeout=settings.connector_timeout_seconds) as client:
                response = client.post(
                    f"{API}/app/installations/{installation}/access_tokens",
                    headers={
                        "Authorization": f"Bearer {app_jwt(current)}",
                        "Accept": "application/vnd.github+json",
                        "X-GitHub-Api-Version": "2022-11-28",
                    },
                )
        except httpx.HTTPError as exc:
            raise ConnectorError("GitHub could not be reached. Try again shortly.") from exc
        if response.status_code in (401, 403, 404):
            raise ConnectorError(
                "GitHub no longer lets the Synaprank app into this account — it may have "
                "been uninstalled or suspended. Install it again from the connect screen."
            )
        if response.status_code >= 400:
            raise ConnectorError(f"GitHub refused an installation token ({response.status_code}).")
        data = response.json() or {}
        token = str(data.get("token") or "")
        if not token:
            raise ConnectorError("GitHub did not return an installation token.")
        self._installation_permissions = dict(data.get("permissions") or {})
        self._installation_token = token
        # Installation tokens last an hour.
        self._installation_expires = current + 3600 - 300
        return token

    def auth_headers(self) -> dict[str, str]:
        token = self.installation_token() if self._installation_id else self.credentials.require("token")
        return {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    # ── Sign-in ────────────────────────────────────────────────────────────
    def list_oauth_choices(self) -> list[OAuthChoice]:
        """The repositories the installation was given."""
        choices: list[OAuthChoice] = []
        for page in range(1, 11):
            data = self.request(
                "GET", "/installation/repositories", params={"per_page": 100, "page": page}
            ) or {}
            batch = parse_installation_repositories(data)
            choices.extend(batch)
            if len(data.get("repositories") or []) < 100:
                break
        return choices

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
        if not self._default_branch:
            self._default_branch = str((row or {}).get("default_branch") or "").strip()
        if self._installation_id:
            # An installation token's rights are the app's, reported when the
            # token was minted; the repository's per-user "permissions" block
            # describes a person and is not a reliable answer here.
            granted = self._installation_permissions
            short = [k for k, v in APP_PERMISSIONS.items() if granted.get(k) != v]
            if short:
                raise ConnectorError(
                    "The Synaprank GitHub App can read this repository but is missing "
                    "write access to contents or pull requests. Ask your platform "
                    "administrator to grant both, then accept the new permissions on GitHub."
                )
            return f"{row.get('full_name')} reachable on {self.branch}"
        permissions = (row or {}).get("permissions") or {}
        if not permissions.get("push", False):
            # Read-only access looks fine until the first rewrite fails hours
            # later, so it is reported as unhealthy now.
            raise ConnectorError(
                f"The token can read {self.repository} but not write to it — "
                "grant Contents: read and write, and Pull requests: write."
            )
        return f"{row.get('full_name')} reachable on {self.branch}"

    # ── Live checks ────────────────────────────────────────────────────────
    def _pull_number(self, url: str, branch: str) -> str:
        """The pull request number from its link, else looked up by branch."""
        match = re.search(r"/pull/(\d+)", url or "")
        if match:
            return match.group(1)
        owner = self.repository.split("/", 1)[0]
        rows = self.request(
            "GET",
            f"/repos/{self.repository}/pulls",
            params={"head": f"{owner}:{branch}", "state": "open"},
        ) or []
        return str(rows[0].get("number") or "") if rows else ""

    def _read_pull_request(self, number: str) -> str:
        """The pull request's state, lower case (``open``, ``closed``)."""
        row = self.request("GET", f"/repos/{self.repository}/pulls/{number}") or {}
        return str(row.get("state") or "").lower()

    def _close_pull_request(self, number: str) -> None:
        """``PATCH /repos/{owner}/{repo}/pulls/{n}`` with ``state: closed``.

        https://docs.github.com/en/rest/pulls/pulls#update-a-pull-request
        """
        self.request("PATCH", f"/repos/{self.repository}/pulls/{number}", json_body={"state": "closed"})

    def _delete_branch(self, name: str) -> None:
        """``DELETE /repos/{owner}/{repo}/git/refs/heads/{branch}``; gone already is fine.

        https://docs.github.com/en/rest/git/refs#delete-a-reference
        """
        try:
            self.request("DELETE", f"/repos/{self.repository}/git/refs/heads/{name}")
        except ConnectorError as exc:
            if getattr(exc, "http_status", None) != 404:
                raise

    def verify_steps(self) -> list:
        """Read the repository, its deploy branch and content; open and close one PR.

        The write step goes through the same primitives as every rewrite —
        branch from the deploy branch head, commit one file, open a pull
        request — then closes the pull request unmerged and deletes the
        branch. The branch is deleted even when a step in between fails.
        It needs a test repository: a pull request notifies watchers and runs CI.
        """
        from app.connectors.base import verify_helpers as vh

        return git_verify_steps(self, vh)


def git_verify_steps(connector, vh) -> list:  # noqa: ANN001
    """The live checks shared in shape by GitHub and Bitbucket.

    ``connector`` supplies the git_base primitives plus ``_pull_number``,
    ``_read_pull_request``, ``_close_pull_request`` and ``_delete_branch``.
    """

    def repository() -> tuple[str, dict]:
        return connector._probe() + ".", {}

    def branch_head() -> tuple[str, dict]:
        head = connector._branch_head()
        return (
            f"Read the latest commit on the deploy branch '{connector.branch}'.",
            {"commit": head[:12]},
        )

    def pages() -> tuple[str, dict]:
        found = connector.list_pages(limit=5)
        return (
            f"Found {vh.plural(len(found), 'content file')} on the deploy branch.",
            {"count": len(found), "paths": [p.remote_id for p in found]},
        )

    def pull_request() -> tuple[str, dict]:
        head = connector._branch_head()
        branch = vh.slug_marker()
        title = vh.marker("pull request")
        opened: dict = {}

        def create() -> str:
            connector._create_branch(branch, from_commit=head)
            return branch

        def check(name: str) -> None:
            connector._commit_file(
                branch=name,
                path=f"synaprank-verification/{name}.md",
                text=(
                    f"# {title}\n\nAdded by an Synaprank.ai connection check on a temporary "
                    "branch. The pull request is closed unmerged and the branch deleted "
                    "straight away.\n"
                ),
                message=f"{title}\n\nTemporary commit from a connection check; never merged.",
                revision="",
            )
            url = connector._open_pull_request(
                branch=name,
                title=title,
                body=(
                    "A connection check by Synaprank.ai. This pull request is closed "
                    "unmerged and its branch deleted within seconds; nothing is merged."
                ),
            )
            number = connector._pull_number(url, name)
            if not number:
                raise ConnectorError("The pull request was opened, but its number did not come back.")
            opened["number"] = number
            try:
                state = connector._read_pull_request(number)
                if state != "open":
                    raise ConnectorError(f"The pull request came back as {state!r}, not open.")
            finally:
                connector._close_pull_request(number)
                opened["closed"] = True

        evidence = vh.create_check_delete(
            what="branch", create=create, check=check, delete=connector._delete_branch
        )
        return (
            "Opened a marked pull request from a temporary branch, closed it unmerged, "
            "and deleted the branch. Nothing was merged.",
            {
                "branch": evidence["created_id"],
                "branch_deleted": evidence["deleted"],
                "pull_request": opened.get("number"),
                "pull_request_closed": bool(opened.get("closed")),
            },
        )

    return [
        vh.read_step("repository", "Reach the repository", repository),
        vh.read_step("branch_head", "Read the deploy branch's latest commit", branch_head),
        vh.read_step("list_pages", "List content files on the deploy branch", pages),
        vh.write_step(
            "pull_request_roundtrip",
            "Open a marked pull request, close it unmerged, delete its branch",
            pull_request,
            requires_test_account=True,
        ),
    ]


CONNECTOR_CLASS = GitHubConnector
