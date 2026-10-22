"""Instagram connector — the Instagram Graph API's content publishing flow.

Publishing an image is two calls, with a wait between them:

1. ``POST /{ig-user-id}/media`` with ``image_url`` and ``caption`` creates a
   *container*. Instagram downloads the image from that URL itself, so the
   URL must be reachable from the internet — Instagram does not accept image
   bytes.
2. ``POST /{ig-user-id}/media_publish`` with ``creation_id`` publishes it,
   once ``GET /{container-id}?fields=status_code`` reports ``FINISHED``.

Instagram accepts JPEG images only; the social media agent stores the images
it makes for Instagram as JPEG.

Metrics (``read_post_metrics``): ``GET /{ig-media-id}/insights`` with
``reach``, ``views``, ``likes``, ``comments``, ``saved``, ``shares`` and
``total_interactions``. ``impressions`` is deprecated (asking for it fails);
``views`` is reported in its place. Clicks are not reported. Figures lag up
to 48 hours. Needs ``instagram_manage_insights``; a connection made before
that was asked for is told to reconnect.
"""
from __future__ import annotations

import re
import time

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
from app.connectors.facebook_pages.connector import insight_values, is_permission_refusal
from app.connectors.facebook_pages.graph import (
    DEFAULT_GRAPH_VERSION,
    GraphConnectorMixin,
    parse_instagram_accounts,
)
from app.core.exceptions import ConnectorConfigError, ConnectorError
from app.core.logging import get_logger
from app.db.base import utcnow

log = get_logger(__name__)

#: Container status checks before giving up, and the pause between them.
STATUS_CHECKS = 10
STATUS_PAUSE_SECONDS = 3.0
#: A small, long-lived public JPEG (Wikimedia Commons, 500x477) that the live
#: check asks Instagram to download. ``verifyImageUrl`` on the connection
#: replaces it.
VERIFY_IMAGE_URL = "https://upload.wikimedia.org/wikipedia/commons/3/3f/JPEG_example_flower.jpg"
#: Posts read per metrics call (one Graph request each).
MAX_METRIC_POSTS = 200
#: Media insights asked for (research R11). Not ``impressions``: deprecated.
MEDIA_INSIGHTS = ("reach", "views", "likes", "comments", "saved", "shares", "total_interactions")
_MEDIA_ID = re.compile(r"[0-9]{1,30}")


def _post_refusal(exc: ConnectorError) -> bool:
    """Whether a failed insights read is about that one post (a 4xx other than 429).

    No status (unreachable), 429 (rate limited) and 5xx (a server fault)
    are about the whole read, so the caller stops instead of skipping.
    """
    status = getattr(exc, "http_status", None)
    return isinstance(status, int) and 400 <= status < 500 and status != 429


class InstagramBusinessConnector(GraphConnectorMixin, BaseConnector):
    permission_hint = (
        "The token needs instagram_basic, instagram_content_publish and pages_read_engagement."
    )

    spec = ConnectorSpec(
        slug="instagram_business",
        name="Instagram",
        category="Social media",
        description="Publish image posts to an Instagram professional account.",
        fields=(
            text(
                "igUserId",
                "Instagram account ID",
                "17841400000000000",
                help_text=(
                    "The Instagram professional account's ID (not its @username). In the Graph "
                    "API Explorer, GET /{page-id}?fields=instagram_business_account shows it."
                ),
            ),
            secret(
                "accessToken",
                "Access token",
                "",
                help_text=(
                    "A long-lived token for the Facebook Page linked to the account, with "
                    "instagram_basic, instagram_content_publish and pages_read_engagement. "
                    "A system user token from Business Manager does not expire."
                ),
            ),
            secret(
                "pageAccessToken",
                "Page access token",
                "",
                required=False,
                help_text=(
                    "Filled in when you connect by signing in with Facebook: the token "
                    "of the Page linked to the account. Leave blank otherwise."
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
                    "posting starts failing with a version error, enter a current version."
                ),
            ),
        ),
        capabilities=frozenset({Capability.PUBLISH_SOCIAL}),
        requirements=(
            "An Instagram Business or Creator account linked to a Facebook Page.",
            "Every post needs an image. Instagram downloads it from this platform's public "
            "web address, so the platform must be reachable from the internet over https.",
            "Seeing how posts are doing needs the instagram_manage_insights permission.",
        ),
        docs_url="https://developers.facebook.com/docs/instagram-platform/content-publishing",
        # Instagram API with Facebook Login: instagram_basic finds the
        # account, instagram_content_publish posts, and Meta requires
        # pages_show_list and pages_read_engagement with them;
        # instagram_manage_insights reads how each post is doing.
        oauth=OAuthBinding(
            provider="meta",
            scopes=(
                "instagram_basic",
                "instagram_content_publish",
                "pages_show_list",
                "pages_read_engagement",
                "instagram_manage_insights",
            ),
            platform_fields=frozenset(),
            access_summary=(
                "Permission to publish image posts to the Instagram account you choose "
                "(through the Facebook Page it is linked to) and see how they are doing. "
                "It cannot read or send messages."
            ),
            choice_noun="Instagram account",
            choice_fields=("igUserId",),
            token_fields=frozenset({"accessToken", "pageAccessToken"}),
        ),
    )

    #: Replaced in tests so the status wait does not actually sleep.
    _sleep = staticmethod(time.sleep)

    @property
    def ig_user_id(self) -> str:
        return self.credentials.require("igUserId").strip()

    def auth_headers(self) -> dict[str, str]:
        # Signed in: the linked Page's token, which does not expire.
        token = self.credentials.get("pageAccessToken") or self.credentials.get("accessToken")
        if not token:
            raise ConnectorConfigError("Instagram has no access token. Enter it on this integration.")
        return {"Authorization": "Bearer " + token}

    def list_oauth_choices(self) -> list[OAuthChoice]:
        return parse_instagram_accounts(
            self.managed_pages("id,name,access_token,instagram_business_account{id,username}")
        )

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
        """Publish an image post now. Returns (media id, permalink).

        ``link`` is not used: Instagram captions cannot contain clickable links.
        """
        from app.services import images

        if not image_url:
            raise ConnectorConfigError("Instagram posts need an image, and this post has none.")
        if not images.is_publicly_reachable():
            raise ConnectorConfigError(
                "Instagram downloads each image from a public web address, and this platform "
                "is not reachable from the internet over https. Download this post and publish "
                "it in the Instagram app instead."
            )
        container = self.request(
            "POST", f"/{self.ig_user_id}/media", json_body={"image_url": image_url, "caption": text}
        )
        container_id = str((container or {}).get("id") or "")
        if not container_id:
            raise ConnectorError("Instagram did not create the post.")
        self._wait_until_ready(container_id)
        published = self.request(
            "POST", f"/{self.ig_user_id}/media_publish", json_body={"creation_id": container_id}
        )
        media_id = str((published or {}).get("id") or "")
        if not media_id:
            raise ConnectorError("Instagram accepted the post but did not return its id.")
        return media_id, self._permalink(media_id)

    def _wait_until_ready(self, container_id: str) -> None:
        for attempt in range(STATUS_CHECKS):
            data = self.request("GET", f"/{container_id}", params={"fields": "status_code"})
            status = str((data or {}).get("status_code") or "").upper()
            if status in ("FINISHED", "PUBLISHED"):
                return
            if status in ("ERROR", "EXPIRED"):
                raise ConnectorError(
                    "Instagram could not process the image for this post (it accepts JPEG "
                    "images up to 8 MB). Try again, or download the post and publish it in the app."
                )
            if attempt < STATUS_CHECKS - 1:
                self._sleep(STATUS_PAUSE_SECONDS)
        raise ConnectorError(
            "Instagram is still processing the image for this post. Try publishing again in a few minutes."
        )

    def _permalink(self, media_id: str) -> str:
        try:
            data = self.request("GET", f"/{media_id}", params={"fields": "permalink"})
        except ConnectorError:
            return ""  # the post is live; only its link is missing
        return str((data or {}).get("permalink") or "")

    # ── Metrics ────────────────────────────────────────────────────────────
    def read_post_metrics(self, remote_ids: list[str]) -> dict[str, PostMetrics]:
        """Lifetime figures for each published media id. See the module docstring.

        A post Instagram will not report on (deleted, an album child) is left
        out; a missing permission stops the whole read with
        :class:`InsightsAccessMissing`.
        """
        out: dict[str, PostMetrics] = {}
        seen: set[str] = set()
        for raw in (remote_ids or [])[:MAX_METRIC_POSTS]:
            media_id = str(raw or "").strip()
            if not _MEDIA_ID.fullmatch(media_id) or media_id in seen:
                continue
            seen.add(media_id)
            try:
                values = insight_values(
                    self.request("GET", f"/{media_id}/insights", params={"metric": ",".join(MEDIA_INSIGHTS)})
                )
            except ConnectorConfigError as exc:
                if is_permission_refusal(exc, self):
                    raise InsightsAccessMissing("Instagram") from None
                raise
            except ConnectorError as exc:
                # A refusal for this one post (deleted, not reportable) skips
                # it; being unreachable, rate limited (429) or a server fault
                # (5xx) stops the read, so a partial read is never recorded.
                if not _post_refusal(exc):
                    raise
                log.info("instagram metrics: post skipped (HTTP %s)", exc.http_status)  # type: ignore[attr-defined]
                continue
            figures = empty_post_metrics()
            # "views" replaced impressions: the times the post was shown.
            figures["impressions"] = count_or_none(values.get("views"))
            figures["reach"] = count_or_none(values.get("reach"))
            figures["engagements"] = count_or_none(values.get("total_interactions"))
            figures["likes"] = count_or_none(values.get("likes"))
            figures["comments"] = count_or_none(values.get("comments"))
            figures["shares"] = count_or_none(values.get("shares"))
            figures["saves"] = count_or_none(values.get("saved"))
            out[media_id] = figures
        return out

    def verify_steps(self) -> list:
        """Read the account and its newest posts; prepare one post without publishing it.

        Instagram has no private or draft post. The write step does only the
        first half of the agents' publishing flow: ``POST /{ig-user-id}/media``
        makes a *container*, and its status is read until Instagram has
        processed the image. ``media_publish`` is never called, so nothing
        appears on the account. Containers cannot be deleted through the API;
        an unpublished one expires after 24 hours ("The container was not
        published within 24 hours and has expired").
        https://developers.facebook.com/docs/instagram-platform/content-publishing
        """
        from app.connectors.base import verify_helpers as vh

        def account() -> tuple[str, dict]:
            data = self.request(
                "GET", f"/{self.ig_user_id}", params={"fields": "id,username,media_count"}
            ) or {}
            if not data.get("id"):
                raise ConnectorError("Instagram did not return the chosen account.")
            username = str(data.get("username") or "")
            return (
                f"Read the Instagram account {'@' + username if username else data['id']}.",
                {"ig_user_id": str(data["id"]), "media_count": data.get("media_count")},
            )

        def recent_media() -> tuple[str, dict]:
            data = self.request(
                "GET", f"/{self.ig_user_id}/media", params={"fields": "id,timestamp", "limit": 5}
            ) or {}
            rows = [r for r in data.get("data") or [] if isinstance(r, dict)]
            return (
                f"Read the account's {vh.plural(len(rows), 'newest post')}.",
                {"count": len(rows), "ids": [str(r.get("id") or "") for r in rows]},
            )

        def container() -> tuple[str, dict]:
            image_url = (self.credentials.get("verifyImageUrl") or VERIFY_IMAGE_URL).strip()
            data = self.request(
                "POST",
                f"/{self.ig_user_id}/media",
                json_body={
                    "image_url": image_url,
                    "caption": vh.marker("unpublished container") + ". Never published.",
                },
            ) or {}
            container_id = str(data.get("id") or "")
            if not container_id:
                raise ConnectorError("Instagram did not create the unpublished post container.")
            self._wait_until_ready(container_id)
            return (
                "Prepared a post container and Instagram processed its image; it was not "
                "published and expires by itself within 24 hours.",
                {"container_id": container_id, "published": False},
            )

        return [
            vh.read_step("account", "Read the Instagram account this connection posts to", account),
            vh.read_step("recent_media", "Read the account's newest posts", recent_media),
            vh.write_step(
                "unpublished_container",
                "Prepare a post without publishing it (Instagram has no private post; the "
                "unpublished container cannot be deleted and expires by itself within 24 hours)",
                container,
            ),
        ]

    def check_health(self) -> HealthReport:
        try:
            data = self.request("GET", f"/{self.ig_user_id}", params={"fields": "username"})
        except ConnectorError as exc:
            return HealthReport(ok=False, detail=str(exc), checked_at=utcnow())
        username = str((data or {}).get("username") or "")
        if not username:
            return HealthReport(ok=False, detail="Instagram did not return this account.", checked_at=utcnow())
        return HealthReport(ok=True, detail=f"Connected to @{username}", checked_at=utcnow())


CONNECTOR_CLASS = InstagramBusinessConnector
