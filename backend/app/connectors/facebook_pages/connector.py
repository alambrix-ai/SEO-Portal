"""Facebook Pages connector — posts to a Facebook Page through the Graph API.

* Text and link posts: ``POST /{page-id}/feed`` with ``message`` and ``link``.
* Photo posts: ``POST /{page-id}/photos`` with the image bytes as the
  multipart ``source`` part and the post text as ``caption`` (or ``url`` for
  an image Facebook should fetch itself).

The token is a **Page** access token — a user token cannot publish as the
Page.

Metrics (``read_post_metrics``): ``GET /{post-id}/insights`` with
``post_media_view`` (impressions), ``post_total_media_view_unique`` (reach),
``post_reactions_by_type_total`` (likes: reactions of every kind) and
``post_clicks``, lifetime totals; then the post's ``comments`` summary and
``shares`` count. ``post_impressions*`` and ``post_engaged_users`` were
removed by Meta and are not asked for. Insights need ``read_insights``; a
connection made before that was asked for is told to reconnect.
"""
from __future__ import annotations

import re

from app.connectors.base.connector import (
    BaseConnector,
    Capability,
    ConnectorSpec,
    HealthReport,
    OAuthBinding,
    OAuthChoice,
)
from app.connectors.base.credentials import secret, text
from app.connectors.base.interfaces import (
    InsightsAccessMissing,
    PostMetrics,
    count_or_none,
    empty_post_metrics,
)
from app.connectors.facebook_pages.graph import (
    DEFAULT_GRAPH_VERSION,
    GraphConnectorMixin,
    multipart,
    parse_pages,
)
from app.core.exceptions import ConnectorConfigError, ConnectorError
from app.core.logging import get_logger
from app.db.base import utcnow

log = get_logger(__name__)

#: Posts read per metrics call; each costs two Graph requests.
MAX_METRIC_POSTS = 200
#: The lifetime post insights asked for (research R11, Graph v26.0).
POST_INSIGHTS = (
    "post_media_view",
    "post_total_media_view_unique",
    "post_reactions_by_type_total",
    "post_clicks",
)
#: A Graph object id: digits, or a Page post's ``{page}_{post}``.
_GRAPH_ID = re.compile(r"[0-9]{1,30}(?:_[0-9]{1,30})?")


def _post_refusal(exc: ConnectorError) -> bool:
    """Whether a failed insights read is about that one post (a 4xx other than 429).

    No status (unreachable), 429 (rate limited) and 5xx (a server fault)
    are about the whole read, so the caller stops instead of skipping.
    """
    status = getattr(exc, "http_status", None)
    return isinstance(status, int) and 400 <= status < 500 and status != 429


def is_permission_refusal(error: Exception, connector: object) -> bool:
    """True when the Graph mixin refused for a missing permission (codes 10, 200-299).

    The mixin raises :class:`ConnectorConfigError` with this exact sentence;
    matching it keeps the translation in one place (``graph.py``).
    """
    if not isinstance(error, ConnectorConfigError):
        return False
    return "is missing a permission this needs" in str(error) and str(error).startswith(
        f"{getattr(connector, 'name', '')}:"
    )


def insight_values(data: object) -> dict[str, object]:
    """``{metric name: value}`` from a Graph insights reply.

    Each entry carries ``values: [{value}]`` (lifetime), or ``total_value:
    {value}`` on newer metrics; the first that is present is used.
    """
    out: dict[str, object] = {}
    rows = data.get("data") if isinstance(data, dict) else None
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict) or not isinstance(row.get("name"), str):
            continue
        total = row.get("total_value")
        values = row.get("values")
        if isinstance(total, dict) and "value" in total:
            out[row["name"]] = total["value"]
        elif isinstance(values, list) and values and isinstance(values[-1], dict):
            out[row["name"]] = values[-1].get("value")
    return out


class FacebookPagesConnector(GraphConnectorMixin, BaseConnector):
    permission_hint = (
        "A Page access token with pages_manage_posts and pages_read_engagement is needed."
    )

    spec = ConnectorSpec(
        slug="facebook_pages",
        name="Facebook Pages",
        category="Social media",
        description="Publish posts and photos to your Facebook Page.",
        fields=(
            text(
                "pageId",
                "Page ID",
                "1234567890",
                required=False,
                help_text=(
                    "The numeric ID of your Facebook Page, shown in the Page's About "
                    "section. Leave blank to use the Page the access token belongs to."
                ),
            ),
            secret(
                "pageAccessToken",
                "Page access token",
                "",
                help_text=(
                    "A Page access token for that Page (not a user token), from the Graph API "
                    "Explorer or a system user in Business Manager, with pages_manage_posts "
                    "and pages_read_engagement. System-user Page tokens do not expire."
                ),
            ),
            text(
                "apiVersion",
                "Graph API version",
                DEFAULT_GRAPH_VERSION,
                required=False,
                default=DEFAULT_GRAPH_VERSION,
                help_text=(
                    "Meta retires each Graph API version about two years after release. If "
                    "posting starts failing with a version error, enter a current version "
                    "from Meta's changelog."
                ),
            ),
        ),
        capabilities=frozenset({Capability.PUBLISH_SOCIAL}),
        requirements=(
            "You must be an admin of the Facebook Page, using a Meta app that has the "
            "pages_manage_posts and pages_read_engagement permissions (and read_insights "
            "to see how posts are doing).",
            "Use a Page access token. A user access token cannot post as the Page.",
        ),
        docs_url="https://developers.facebook.com/docs/pages-api/posts",
        # pages_manage_posts publishes; pages_read_engagement reads the Page
        # back (health check) and is required with it; pages_show_list lists
        # the Pages to choose from (GET /me/accounts); read_insights reads
        # how each published post is doing (GET /{post-id}/insights).
        oauth=OAuthBinding(
            provider="meta",
            scopes=("pages_show_list", "pages_manage_posts", "pages_read_engagement", "read_insights"),
            platform_fields=frozenset(),
            access_summary=(
                "Permission to publish posts to the Facebook Page you choose and see how "
                "they are doing, so the social media agent can post what you approve. It "
                "cannot message people or change the Page's settings."
            ),
            choice_noun="Page",
            choice_fields=("pageId",),
            token_fields=frozenset({"pageAccessToken", "accessToken"}),
        ),
    )

    _resolved_page_id: str = ""

    @property
    def page_id(self) -> str:
        """The Page to post to: as entered, else the Page token's own Page.

        A Page access token identifies its Page: "/me" resolves to "the person
        or Page whose access token is currently being used".
        https://developers.facebook.com/docs/graph-api/overview
        A person's token (a sign-in before a Page is chosen) is never used
        for this — its /me is the person, not a Page.
        """
        configured = (self.credentials.get("pageId") or "").strip()
        if configured:
            return configured
        if not self._resolved_page_id:
            if not self.credentials.get("pageAccessToken"):
                raise ConnectorConfigError(
                    "Choose the Facebook Page to post to, or enter its Page access token."
                )
            data = self.request("GET", "/me", params={"fields": "id,name"}) or {}
            found = str(data.get("id") or "").strip()
            if not found:
                raise ConnectorError("Facebook did not say which Page this token belongs to.")
            self._resolved_page_id = found
        return self._resolved_page_id

    def auth_headers(self) -> dict[str, str]:
        # Before a Page is chosen, a sign-in has only the person's own token,
        # which can list their Pages but not post as one.
        token = self.credentials.get("pageAccessToken") or self.credentials.get("accessToken")
        if not token:
            raise ConnectorConfigError("Facebook Pages has no Page access token. Enter it on this integration.")
        return {"Authorization": "Bearer " + token}

    def list_oauth_choices(self) -> list[OAuthChoice]:
        return parse_pages(self.managed_pages("id,name,access_token,tasks"))

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
        """Publish now. Returns (post id, post URL)."""
        if image_bytes or image_url:
            # A photo post cannot carry a link preview, so the link goes in the text.
            caption = text if not link or link in text else f"{text}\n\n{link}"
            if image_bytes:
                body, content_type = multipart(
                    {"caption": caption},
                    {"source": ("image" + _extension(image_mime), image_bytes, image_mime or "image/png")},
                )
                data = self.request(
                    "POST", f"/{self.page_id}/photos", content=body, headers={"Content-Type": content_type}
                )
            else:
                data = self.request("POST", f"/{self.page_id}/photos", json_body={"url": image_url, "caption": caption})
            post_id = str((data or {}).get("post_id") or (data or {}).get("id") or "")
        else:
            payload = {"message": text}
            if link:
                payload["link"] = link
            data = self.request("POST", f"/{self.page_id}/feed", json_body=payload)
            post_id = str((data or {}).get("id") or "")
        if not post_id:
            raise ConnectorError("Facebook accepted the post but did not return its id.")
        return post_id, f"https://www.facebook.com/{post_id}"

    # ── Metrics ────────────────────────────────────────────────────────────
    def read_post_metrics(self, remote_ids: list[str]) -> dict[str, PostMetrics]:
        """Lifetime figures for each published post id. See the module docstring.

        A post Facebook will not report on (deleted, or not this Page's) is
        left out; a missing permission stops the whole read with
        :class:`InsightsAccessMissing`.
        """
        out: dict[str, PostMetrics] = {}
        seen: set[str] = set()
        for raw in (remote_ids or [])[:MAX_METRIC_POSTS]:
            post_id = str(raw or "").strip()
            if not _GRAPH_ID.fullmatch(post_id) or post_id in seen:
                continue
            seen.add(post_id)
            try:
                insights = insight_values(
                    self.request(
                        "GET",
                        f"/{post_id}/insights",
                        params={"metric": ",".join(POST_INSIGHTS), "period": "lifetime"},
                    )
                )
            except ConnectorConfigError as exc:
                if is_permission_refusal(exc, self):
                    raise InsightsAccessMissing("Facebook") from None
                raise
            except ConnectorError as exc:
                # A refusal for this one post (deleted, not reportable) skips
                # it; being unreachable, rate limited (429) or a server fault
                # (5xx) stops the read, so a partial read is never recorded.
                if not _post_refusal(exc):
                    raise
                log.info("facebook metrics: post skipped (HTTP %s)", exc.http_status)  # type: ignore[attr-defined]
                continue
            figures = empty_post_metrics()
            figures["impressions"] = count_or_none(insights.get("post_media_view"))
            figures["reach"] = count_or_none(insights.get("post_total_media_view_unique"))
            figures["clicks"] = count_or_none(insights.get("post_clicks"))
            reactions = insights.get("post_reactions_by_type_total")
            if isinstance(reactions, dict):
                counts = [count_or_none(v) for v in reactions.values()]
                figures["likes"] = sum(c for c in counts if c is not None) if counts else 0
            figures.update(self._post_counts(post_id))
            out[post_id] = figures
        return out

    def _post_counts(self, post_id: str) -> PostMetrics:
        """Comments and shares from the post itself. Best effort: unknown stays None."""
        try:
            data = self.request(
                "GET", f"/{post_id}", params={"fields": "shares,comments.limit(0).summary(true)"}
            ) or {}
        except ConnectorError as exc:
            log.info("facebook metrics: post counts unavailable (HTTP %s)", getattr(exc, "http_status", "?"))
            return {}
        comments = data.get("comments") if isinstance(data.get("comments"), dict) else {}
        summary = comments.get("summary") if isinstance(comments.get("summary"), dict) else {}
        shares = data.get("shares") if isinstance(data.get("shares"), dict) else None
        return {
            "comments": count_or_none(summary.get("total_count")),
            # No "shares" field means nobody has shared it yet.
            "shares": count_or_none(shares.get("count")) if shares is not None else 0,
        }

    def verify_steps(self) -> list:
        """Read the chosen Page and its newest posts; make one unpublished post and delete it.

        The write step uses the publishing endpoint (``POST /{page-id}/feed``)
        with ``published=false``: Meta keeps such a post off the Page's
        timeline ("Unpublished Page posts are treated the same as published
        Page posts except that they do not appear in /feed"). It is read back
        through ``GET /{post-id}?fields=is_published`` and removed with
        ``DELETE /{post-id}``.
        https://developers.facebook.com/docs/graph-api/reference/page/feed
        https://developers.facebook.com/docs/pages-api/posts
        """
        from app.connectors.base import verify_helpers as vh

        def page() -> tuple[str, dict]:
            data = self.request("GET", f"/{self.page_id}", params={"fields": "id,name"}) or {}
            if not data.get("id"):
                raise ConnectorError("Facebook did not return the chosen Page.")
            return (
                f"Read the Page '{data.get('name') or data['id']}' this connection posts to.",
                {"page_id": str(data["id"])},
            )

        def recent_posts() -> tuple[str, dict]:
            data = self.request(
                "GET", f"/{self.page_id}/feed", params={"fields": "id,created_time", "limit": 5}
            ) or {}
            rows = [r for r in data.get("data") or [] if isinstance(r, dict)]
            return (
                f"Read the Page's {vh.plural(len(rows), 'newest post')}.",
                {"count": len(rows), "ids": [str(r.get("id") or "") for r in rows]},
            )

        def unpublished_post() -> tuple[str, dict]:
            def create() -> str:
                data = self.request(
                    "POST",
                    f"/{self.page_id}/feed",
                    json_body={
                        "message": vh.marker("unpublished post") + ". Safe to delete.",
                        "published": False,
                    },
                ) or {}
                return str(data.get("id") or "")

            def check(post_id: str) -> None:
                row = self.request("GET", f"/{post_id}", params={"fields": "id,is_published"}) or {}
                if row.get("is_published") is not False:
                    raise ConnectorError("Facebook did not report the test post as unpublished.")

            def delete(post_id: str) -> None:
                self.request("DELETE", f"/{post_id}")

            evidence = vh.create_check_delete(
                what="unpublished Page post", create=create, check=check, delete=delete
            )
            return (
                "Made an unpublished post on the Page, confirmed it is not published, and deleted it.",
                evidence,
            )

        return [
            vh.read_step("page", "Read the Facebook Page this connection posts to", page),
            vh.read_step("recent_posts", "Read the Page's newest posts", recent_posts),
            vh.write_step(
                "unpublished_post_roundtrip",
                "Make an unpublished (hidden) post on the Page, read it back, then delete it",
                unpublished_post,
            ),
        ]

    def check_health(self) -> HealthReport:
        try:
            data = self.request("GET", f"/{self.page_id}", params={"fields": "name"})
        except ConnectorError as exc:
            return HealthReport(ok=False, detail=str(exc), checked_at=utcnow())
        name = str((data or {}).get("name") or "")
        if not name:
            return HealthReport(ok=False, detail="Facebook did not return this Page.", checked_at=utcnow())
        return HealthReport(ok=True, detail=f"Connected to the Page {name}", checked_at=utcnow())


def _extension(mime: str) -> str:
    return {"image/jpeg": ".jpg", "image/webp": ".webp", "image/gif": ".gif"}.get(mime, ".png")


CONNECTOR_CLASS = FacebookPagesConnector
