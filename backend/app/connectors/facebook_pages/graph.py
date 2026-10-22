"""What the Facebook Page and Instagram connectors share: the Graph API.

Both talk to ``https://graph.facebook.com/{version}``. The version is a field
with a current default rather than a constant, because Meta retires each
Graph API version about two years after release and an operator should be
able to move forward without a code change.

Graph errors arrive as ``{"error": {"message", "code", "error_subcode"}}``.
The base connector turns any 4xx into a generic sentence (vendor bodies can
carry internals); the codes below are the ones with a specific, safe and
actionable explanation, so they are translated here instead.
"""
from __future__ import annotations

import re

import httpx

from app.connectors.base.connector import BaseConnector
from app.connectors.base.credentials import from_platform_sign_in
from app.connectors.base.meta_graph import GRAPH_API_VERSION, effective_version
from app.core.exceptions import ConnectorConfigError, ConnectorError
from app.core.logging import get_logger

log = get_logger(__name__)

#: Default Graph API version: the one every Meta connector uses
#: (app/connectors/base/meta_graph.py).
DEFAULT_GRAPH_VERSION = GRAPH_API_VERSION
GRAPH_HOST = "https://graph.facebook.com"
_VERSION_FORMAT = re.compile(r"^v\d{1,2}\.\d$")


def graph_error(response: httpx.Response) -> dict:
    try:
        body = response.json()
    except ValueError:
        return {}
    error = body.get("error") if isinstance(body, dict) else None
    return error if isinstance(error, dict) else {}


def multipart(fields: dict[str, str], files: dict[str, tuple[str, bytes, str]]) -> tuple[bytes, str]:
    """A multipart/form-data body and its Content-Type, for ``request(content=...)``."""
    built = httpx.Request("POST", "https://placeholder.invalid/", data=fields, files=files)
    return built.read(), built.headers["Content-Type"]


class GraphConnectorMixin:
    """Version handling and error translation for Graph API connectors.

    Mix in before :class:`BaseConnector`. Subclasses set ``permission_hint``.
    """

    #: Which permissions to name when Graph refuses for lack of one.
    permission_hint: str = ""

    @property
    def api_version(self) -> str:
        # A version the old form saved by itself reads as the current one.
        raw = effective_version(self.credentials.get("apiVersion"))  # type: ignore[attr-defined]
        version = raw if raw.startswith("v") else f"v{raw}"
        if not _VERSION_FORMAT.match(version):
            raise ConnectorConfigError(
                f"{raw!r} is not a Graph API version. Enter it as Meta writes it, for "
                f"example {DEFAULT_GRAPH_VERSION}."
            )
        return version

    @property
    def base_url(self) -> str:
        return f"{GRAPH_HOST}/{self.api_version}"

    def version_message(self) -> str:
        return (
            f"Meta no longer accepts Graph API version {self.api_version}. Enter a "
            "current version (for example "
            f"{DEFAULT_GRAPH_VERSION} or newer) in the Graph API version field on this "
            "integration."
        )

    def _send(self, request: httpx.Request) -> httpx.Response:
        response = BaseConnector._send(self, request)  # type: ignore[arg-type]
        if response.status_code < 400:
            return response
        error = graph_error(response)
        code = int(error.get("code") or 0)
        subcode = int(error.get("error_subcode") or 0)
        message = str(error.get("message") or "")
        name = self.name  # type: ignore[attr-defined]
        if code == 190:
            if from_platform_sign_in(self.credentials.values):  # type: ignore[attr-defined]
                # Signed in with Facebook: the person signs in again; there
                # is nothing to paste.
                raise ConnectorConfigError(
                    f"{name}: the Facebook sign-in has expired or was removed (for "
                    "example after a password change). Open this integration and "
                    "choose Sign in again."
                )
            raise ConnectorConfigError(
                f"{name}: the access token has expired or was revoked. Generate a new "
                "one and enter it on this integration."
            )
        if code == 10 or 200 <= code <= 299:
            raise ConnectorConfigError(
                f"{name}: the access token is missing a permission this needs. "
                f"{self.permission_hint}"
            )
        if code == 12 or code == 2635 or ("version" in message.lower() and "deprecat" in message.lower()):
            raise ConnectorConfigError(self.version_message())
        if code in (4, 17, 32, 613) or code == 80001:
            raise ConnectorError(f"{name} is limiting how fast posts can be made. Try again later.")
        if code == 368:
            raise ConnectorError(
                f"Meta temporarily blocked posting from this account ({name}). Try again later."
            )
        if code == 9004 or subcode == 2207052:
            raise ConnectorError(
                f"{name} could not download the image from this platform's public address. "
                "Check that the address is reachable from the internet over https."
            )
        return response

    def diagnose(self, error: Exception) -> str | None:
        if "Graph API version" in str(error):
            return self.version_message()
        from app.connectors.base.meta_graph import version_problem

        return version_problem(error, self.api_version)

    def user_token_headers(self) -> dict[str, str]:
        """The signed-in person's own token, for listing what they manage.

        Posting uses a Page token; finding the Pages (and the Instagram
        accounts linked to them) needs the user token the sign-in stored.
        """
        token = self.credentials.get("accessToken")  # type: ignore[attr-defined]
        if not token:
            raise ConnectorConfigError("Sign in with Facebook first.")
        return {"Authorization": "Bearer " + token}

    def managed_pages(self, fields: str) -> list[dict]:
        """``GET /me/accounts``: the Pages the person has a role on, all pages
        of results (bounded). Page tokens returned here, from a long-lived
        user token, do not expire."""
        out: list[dict] = []
        data = self.request(  # type: ignore[attr-defined]
            "GET", "/me/accounts", params={"fields": fields, "limit": 100},
            headers=self.user_token_headers(),
        )
        for _ in range(MAX_LIST_PAGES):
            out.extend(row for row in (data or {}).get("data") or [] if isinstance(row, dict))
            next_url = ((data or {}).get("paging") or {}).get("next")
            if not next_url:
                break
            data = self.request("GET", next_url, headers=self.user_token_headers())  # type: ignore[attr-defined]
        return out


#: Pages of a Graph listing followed during the sign-in choose step.
MAX_LIST_PAGES = 10


def parse_pages(rows: list[dict]) -> list:  # noqa: ANN201
    """Facebook Pages the person can post to, as sign-in choices.

    Only Pages where their role includes creating content, and only with a
    Page token. The token goes into the connector's own field and is never
    sent to the browser (the choose API returns id, label and detail only).
    """
    from app.connectors.base.connector import OAuthChoice
    from app.connectors.base.credentials import SIGN_IN_EXPIRY_KEY

    out = []
    for row in rows:
        page_id, token = str(row.get("id") or ""), str(row.get("access_token") or "")
        tasks = row.get("tasks")
        if not page_id or not token:
            continue
        if isinstance(tasks, list) and tasks and "CREATE_CONTENT" not in tasks:
            continue
        out.append(
            OAuthChoice(
                id=page_id,
                label=str(row.get("name") or f"Page {page_id}"),
                detail=f"Page ID {page_id}",
                # A Page token from a long-lived user token does not expire,
                # so the connection does not carry the user token's end date.
                values={"pageId": page_id, "pageAccessToken": token, SIGN_IN_EXPIRY_KEY: ""},
            )
        )
    return out


def parse_instagram_accounts(rows: list[dict]) -> list:  # noqa: ANN201
    """Instagram professional accounts linked to the person's Pages."""
    from app.connectors.base.connector import OAuthChoice
    from app.connectors.base.credentials import SIGN_IN_EXPIRY_KEY

    out = []
    seen: set[str] = set()
    for row in rows:
        account = row.get("instagram_business_account")
        if not isinstance(account, dict) or not account.get("id"):
            continue
        ig_id = str(account["id"])
        if ig_id in seen:
            continue
        seen.add(ig_id)
        username = str(account.get("username") or "")
        page = str(row.get("name") or row.get("id") or "")
        values = {"igUserId": ig_id}
        if row.get("access_token"):
            # Meta's publishing guide uses the linked Page's token; it does
            # not expire, unlike the person's own (about 60 days).
            values.update({"pageAccessToken": str(row["access_token"]), SIGN_IN_EXPIRY_KEY: ""})
        out.append(
            OAuthChoice(
                id=ig_id,
                label=f"@{username}" if username else f"Instagram account {ig_id}",
                detail=f"Linked to the Facebook Page {page}" if page else "",
                values=values,
            )
        )
    return out
