"""LinkedIn Pages connector — organic posts on a company page (Posts API).

* ``POST /rest/posts`` as ``urn:li:organization:{id}``, published to the main
  feed. The new post's URN comes back in the ``x-restli-id`` response header
  with an empty body.
* An image is uploaded first: ``POST /rest/images?action=initializeUpload``
  returns an upload URL and an image URN; the bytes are PUT to the URL and the
  post references the URN in ``content.media.id``.

Like LinkedIn Ads, the API is versioned by month (``LinkedIn-Version``) and a
retired version answers 426. The version sent is the server's
``LINKEDIN_API_VERSION`` unless the connection names a different one; the
months older forms saved by themselves are read as "not chosen".

Metrics (``read_post_metrics``): ``GET /rest/organizationalEntityShareStatistics``
for the page, with the posts as ``shares=List(...)`` and ``ugcPosts=List(...)``.
Needs the Community Management API and ``rw_organization_admin``; LinkedIn
keeps a rolling 12 months.

Commentary is LinkedIn "little text": reserved characters are escaped, and
hashtags are written in its ``{hashtag|\\#|tag}`` template so they stay
hashtags instead of literal text.
"""
from __future__ import annotations

import re
from urllib.parse import quote

import httpx

from app.connectors.base.connector import (
    BaseConnector,
    Capability,
    ConnectorSpec,
    HealthReport,
    OAuthBinding,
    OAuthChoice,
)
from app.connectors.base.credentials import text
from app.connectors.base.interfaces import (
    InsightsAccessMissing,
    PostMetrics,
    count_or_none,
    empty_post_metrics,
    sum_or_none,
)
from app.connectors.base.oauth import OAuthTokenMixin, token_fields
from app.core.config import settings
from app.core.exceptions import ConnectorConfigError, ConnectorError
from app.db.base import utcnow

#: The version sent unless the connection names another (server setting).
DEFAULT_API_VERSION = settings.linkedin_api_version
#: Months the connect form used to save by itself as its default. A
#: connection holding exactly one of these never chose it.
PREVIOUS_DEFAULT_VERSIONS = frozenset({"202606"})
_VERSION_FORMAT = re.compile(r"\d{6}")
#: Post URNs per statistics call (keeps the query string short).
METRICS_BATCH = 20
#: Posts read per metrics call.
MAX_METRIC_POSTS = 500
_POST_URN = re.compile(r"urn:li:(share|ugcPost):[0-9]{1,30}")

#: Characters LinkedIn's little text format reserves in commentary.
_LITTLE_TEXT_RESERVED = set("\\|{}@[]()<>#*_~")
_HASHTAG = re.compile(r"(?<![\w#])#(\w+)")


def little_text(value: str) -> str:
    """Escape commentary, keeping ``#word`` as real LinkedIn hashtags."""
    out: list[str] = []
    position = 0
    for match in _HASHTAG.finditer(value):
        out.append(_escape(value[position : match.start()]))
        out.append("{hashtag|\\#|" + _escape(match.group(1)) + "}")
        position = match.end()
    out.append(_escape(value[position:]))
    return "".join(out)


#: Page roles that may post as the organization.
POSTING_ROLES = frozenset({"ADMINISTRATOR", "CONTENT_ADMINISTRATOR"})
#: Pages looked up by name in the choose step; the rest are listed by id.
_MAX_NAMED = 25


def organizations_from_acls(elements: list[dict]) -> list[str]:
    """Organization ids the member may post for, from ``organizationAcls``.

    LinkedIn's samples name the organization field both ``organization``
    and ``organizationTarget``; either is read.
    """
    out: list[str] = []
    for row in elements:
        role = str(row.get("role") or "").upper()
        state = str(row.get("state") or "APPROVED").upper()
        urn = str(row.get("organization") or row.get("organizationTarget") or "")
        if role not in POSTING_ROLES or state != "APPROVED":
            continue
        if urn.startswith("urn:li:organization:"):
            org = urn.rsplit(":", 1)[-1]
            if org not in out:
                out.append(org)
    return out


def _escape(value: str) -> str:
    return "".join(f"\\{ch}" if ch in _LITTLE_TEXT_RESERVED else ch for ch in value)


class LinkedInPagesConnector(OAuthTokenMixin, BaseConnector):
    spec = ConnectorSpec(
        slug="linkedin_pages",
        name="LinkedIn Pages",
        category="Social media",
        description="Publish posts, with images, to your LinkedIn company page.",
        fields=(
            text(
                "organizationId",
                "Organization ID",
                "12345678",
                help_text=(
                    "The number in your company page's admin URL "
                    "(linkedin.com/company/12345678/admin). A urn:li:organization:… is accepted too."
                ),
            ),
            text(
                "apiVersion",
                "LinkedIn API version",
                DEFAULT_API_VERSION,
                required=False,
                help_text=(
                    "Sent as the LinkedIn-Version header (YYYYMM). Leave blank to use the "
                    "platform's current version. LinkedIn retires each version after about a "
                    "year; if posting fails with a version error, enter a recent month from "
                    "LinkedIn's API versioning page."
                ),
            ),
            *token_fields(
                refreshable=False,
                vendor="LinkedIn",
                docs=(
                    "From a LinkedIn app with the Community Management API, authorised by a "
                    "page admin with w_organization_social and r_organization_social. "
                    "LinkedIn access tokens last 60 days, then a new one must be entered."
                ),
            ),
        ),
        capabilities=frozenset({Capability.PUBLISH_SOCIAL}),
        requirements=(
            "A LinkedIn developer app approved for the Community Management API.",
            "The token must belong to an administrator of the company page.",
        ),
        docs_url="https://learn.microsoft.com/en-us/linkedin/marketing/community-management/shares/posts-api",
        base_url="https://api.linkedin.com/rest",
        # Community Management API: w_organization_social posts and uploads
        # images, r_organization_social reads the page's posts (health
        # check), rw_organization_admin lists the pages the member manages
        # (organizationAcls) and their names.
        oauth=OAuthBinding(
            provider="linkedin_pages",
            scopes=("w_organization_social", "r_organization_social", "rw_organization_admin"),
            access_summary=(
                "Permission to publish posts to the LinkedIn company page you choose, "
                "so the social media agent can post what you approve. You need to be "
                "an admin of that page."
            ),
            choice_noun="company page",
            choice_fields=("organizationId",),
        ),
    )

    # Used only when LinkedIn issued a refresh token; otherwise the 60-day
    # access token is used as stored, then "Sign in again".
    token_url = "https://www.linkedin.com/oauth/v2/accessToken"

    # ── Sign-in ────────────────────────────────────────────────────────────
    def list_oauth_choices(self) -> list[OAuthChoice]:
        data = self.request(
            "GET",
            "/organizationAcls",
            params={"q": "roleAssignee", "state": "APPROVED", "count": 100},
        ) or {}
        organizations = organizations_from_acls(
            [e for e in data.get("elements") or [] if isinstance(e, dict)]
        )
        choices: list[OAuthChoice] = []
        for index, org in enumerate(organizations):
            name = ""
            if index < _MAX_NAMED:
                try:
                    found = self.request("GET", f"/organizations/{org}") or {}
                    name = str(found.get("localizedName") or "")
                except ConnectorError:
                    name = ""  # a content admin may not read the page itself
            choices.append(
                OAuthChoice(
                    id=org,
                    label=name or f"Company page {org}",
                    detail=f"Organization ID {org}",
                    values={"organizationId": org},
                )
            )
        return choices

    @property
    def api_version(self) -> str:
        saved = (self.credentials.get("apiVersion") or "").strip()
        if not saved or saved in PREVIOUS_DEFAULT_VERSIONS:
            return DEFAULT_API_VERSION
        if not _VERSION_FORMAT.fullmatch(saved):
            raise ConnectorConfigError(
                f"{saved[:20]!r} is not a LinkedIn API version. Enter it as YYYYMM, for "
                f"example {DEFAULT_API_VERSION}."
            )
        return saved

    @property
    def organization_urn(self) -> str:
        raw = self.credentials.require("organizationId").strip()
        return raw if raw.startswith("urn:") else f"urn:li:organization:{raw}"

    def auth_headers(self) -> dict[str, str]:
        return {
            "Authorization": "Bearer " + self.access_token,
            "LinkedIn-Version": self.api_version,
            "X-Restli-Protocol-Version": "2.0.0",
            "Content-Type": "application/json",
        }

    def _send(self, request: httpx.Request) -> httpx.Response:
        response = super()._send(request)
        if response.status_code == 426 or (
            response.status_code == 400 and "NONEXISTENT_VERSION" in response.text
        ):
            raise ConnectorConfigError(self._version_message())
        return response

    def _version_message(self) -> str:
        return (
            f"LinkedIn no longer accepts API version {self.api_version}. Enter a recent "
            "version (YYYYMM) in the LinkedIn API version field on this integration."
        )

    def diagnose(self, error: Exception) -> str | None:
        if "API version" in str(error):
            return self._version_message()
        return None

    # ── Publishing ─────────────────────────────────────────────────────────
    def publish_post(
        self,
        *,
        text: str,
        link: str = "",
        image_bytes: bytes | None = None,
        image_mime: str = "",
        image_url: str = "",
        title: str = "",
    ) -> tuple[str, str]:
        """Publish now. Returns (post URN, post URL)."""
        body_text = text if not link or link in text else f"{text}\n\n{link}"
        post: dict = {
            "author": self.organization_urn,
            "commentary": little_text(body_text),
            "visibility": "PUBLIC",
            "distribution": {
                "feedDistribution": "MAIN_FEED",
                "targetEntities": [],
                "thirdPartyDistributionChannels": [],
            },
            "lifecycleState": "PUBLISHED",
            "isReshareDisabledByAuthor": False,
        }
        if image_bytes:
            image_urn = self._upload_image(image_bytes, image_mime)
            media: dict = {"id": image_urn}
            if title:
                media["title"] = title[:200]
            post["content"] = {"media": media}
        response = self.request("POST", "/posts", json_body=post, return_response=True)
        urn = response.headers.get("x-restli-id") or response.headers.get("x-linkedin-id", "")
        if not urn:
            raise ConnectorError("LinkedIn created the post but did not return its id.")
        return urn, f"https://www.linkedin.com/feed/update/{urn}/"

    def _upload_image(self, data: bytes, mime: str) -> str:
        started = self.request(
            "POST",
            "/images?action=initializeUpload",
            json_body={"initializeUploadRequest": {"owner": self.organization_urn}},
        )
        value = (started or {}).get("value") or {}
        upload_url, image_urn = value.get("uploadUrl"), value.get("image")
        if not upload_url or not image_urn:
            raise ConnectorError("LinkedIn did not accept the image upload.")
        self.request(
            "PUT",
            upload_url,
            content=data,
            headers={"Content-Type": mime or "application/octet-stream"},
            expect_json=False,
        )
        return str(image_urn)

    # ── Metrics ────────────────────────────────────────────────────────────
    def read_post_metrics(self, remote_ids: list[str]) -> dict[str, PostMetrics]:
        """Lifetime figures for each post URN (``urn:li:share:…`` or ``urn:li:ugcPost:…``).

        Impressions, unique impressions (reach), clicks, reactions (likes),
        comments and reposts (shares). ``engagements`` is the sum of clicks,
        reactions, comments and reposts: LinkedIn's own engagement figure is
        that sum divided by impressions. LinkedIn has no saves. A 403 means
        the sign-in lacks the statistics permission.
        """
        wanted: list[str] = []
        for raw in remote_ids or []:
            urn = str(raw or "").strip()
            if _POST_URN.fullmatch(urn) and urn not in wanted:
                wanted.append(urn)
            if len(wanted) >= MAX_METRIC_POSTS:
                break
        out: dict[str, PostMetrics] = {}
        org = quote(self.organization_urn, safe="")
        for start in range(0, len(wanted), METRICS_BATCH):
            batch = wanted[start : start + METRICS_BATCH]
            shares = [quote(u, safe="") for u in batch if u.startswith("urn:li:share:")]
            ugc = [quote(u, safe="") for u in batch if u.startswith("urn:li:ugcPost:")]
            query = f"q=organizationalEntity&organizationalEntity={org}"
            if shares:
                query += f"&shares=List({','.join(shares)})"
            if ugc:
                query += f"&ugcPosts=List({','.join(ugc)})"
            try:
                data = self.request("GET", f"/organizationalEntityShareStatistics?{query}") or {}
            except ConnectorConfigError:
                raise
            except ConnectorError as exc:
                if getattr(exc, "http_status", None) == 403:
                    raise InsightsAccessMissing("LinkedIn") from None
                raise
            for row in data.get("elements") or []:
                if not isinstance(row, dict):
                    continue
                urn = str(row.get("share") or row.get("ugcPost") or "")
                if urn not in batch:
                    continue
                stats = row.get("totalShareStatistics")
                stats = stats if isinstance(stats, dict) else {}
                figures = empty_post_metrics()
                figures["impressions"] = count_or_none(stats.get("impressionCount"))
                figures["reach"] = count_or_none(stats.get("uniqueImpressionsCount"))
                figures["clicks"] = count_or_none(stats.get("clickCount"))
                figures["likes"] = count_or_none(stats.get("likeCount"))
                figures["comments"] = count_or_none(stats.get("commentCount"))
                figures["shares"] = count_or_none(stats.get("shareCount"))
                figures["engagements"] = sum_or_none(
                    figures["clicks"], figures["likes"], figures["comments"], figures["shares"]  # type: ignore[arg-type]
                )
                out[urn] = figures
        return out

    def verify_steps(self) -> list:
        """Confirm the page and the posting role; read its newest posts; post once and delete.

        LinkedIn's Posts API accepts only ``lifecycleState: PUBLISHED`` when a
        post is created ("PUBLISHED is the only accepted field during
        creation"), and its hidden "dark" posts need an ad account, so the
        write step publishes a real post through the agents' own
        ``publish_post`` and deletes it at once with
        ``DELETE /rest/posts/{urn}``.
        https://learn.microsoft.com/en-us/linkedin/marketing/community-management/shares/posts-api
        """
        from app.connectors.base import verify_helpers as vh

        def page_role() -> tuple[str, dict]:
            org = self.organization_urn.rsplit(":", 1)[-1]
            allowed = [choice.id for choice in self.list_oauth_choices()]
            if org not in allowed:
                raise ConnectorConfigError(
                    f"This sign-in is not an administrator or content administrator of company "
                    f"page {org}, so it cannot post there."
                )
            return (
                f"This sign-in may post to company page {org} "
                f"(it manages {vh.plural(len(allowed), 'page')}).",
                {"organization_id": org, "pages": len(allowed)},
            )

        def recent_posts() -> tuple[str, dict]:
            data = self.request(
                "GET",
                f"/posts?author={quote(self.organization_urn, safe='')}&q=author&count=5",
                headers={"X-RestLi-Method": "FINDER"},
            ) or {}
            rows = [r for r in data.get("elements") or [] if isinstance(r, dict)]
            return (
                f"Read the page's {vh.plural(len(rows), 'newest post')}.",
                {"count": len(rows), "ids": [str(r.get("id") or "") for r in rows]},
            )

        def post_and_delete() -> tuple[str, dict]:
            def create() -> str:
                urn, _ = self.publish_post(
                    text=vh.marker("post") + ". Posted by a connection check and deleted straight away."
                )
                return urn

            def check(urn: str) -> None:
                row = self.request(
                    "GET", f"/posts/{quote(urn, safe='')}", params={"viewContext": "AUTHOR"}
                ) or {}
                if row.get("author") != self.organization_urn:
                    raise ConnectorError("LinkedIn returned the test post under a different author.")

            def delete(urn: str) -> None:
                self.request(
                    "DELETE",
                    f"/posts/{quote(urn, safe='')}",
                    headers={"X-RestLi-Method": "DELETE"},
                    expect_json=False,
                )

            evidence = vh.create_check_delete(what="post", create=create, check=check, delete=delete)
            return (
                "Published a short text post on the company page, read it back, and deleted it.",
                evidence,
            )

        return [
            vh.read_step("page_role", "Confirm this sign-in may post to the chosen company page", page_role),
            vh.read_step("recent_posts", "Read the company page's newest posts", recent_posts),
            vh.write_step(
                "public_post_roundtrip",
                "Post publicly on the company page and delete it immediately (LinkedIn has no "
                "draft or private post for pages)",
                post_and_delete,
            ),
        ]

    def check_health(self) -> HealthReport:
        """Lists the page's newest post: proves the token can read the page's posts."""
        try:
            self.request(
                "GET",
                f"/posts?author={quote(self.organization_urn, safe='')}&q=author&count=1",
                headers={"X-RestLi-Method": "FINDER"},
            )
        except ConnectorError as exc:
            return HealthReport(ok=False, detail=str(exc), checked_at=utcnow())
        page = self.organization_urn.rsplit(":", 1)[-1]
        return HealthReport(ok=True, detail=f"Connected to company page {page}", checked_at=utcnow())


CONNECTOR_CLASS = LinkedInPagesConnector
