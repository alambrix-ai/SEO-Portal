"""X (Twitter) connector — posts through X API v2 (``POST /2/tweets``).

Three ways to authenticate, all user context (posting needs a user):

* **Sign in with X** (the default) — OAuth 2.0 with PKCE through the
  platform's own X app, scopes ``tweet.read tweet.write users.read
  offline.access media.write``. Access tokens last two hours; each refresh
  returns a new single-use refresh token, which :class:`OAuthTokenMixin`
  stores back.
* **OAuth 1.0a** (Advanced) — the customer's own app's API key and secret
  plus the account's access token and secret, from the developer portal's
  "Keys and tokens" page. They do not expire. Each request is signed
  (RFC 5849, HMAC-SHA1).
* **A pasted OAuth 2.0 user token** (Advanced) — used as given; it stops
  working after two hours.

Images are uploaded first, in chunks (X API v2):
``POST /2/media/upload/initialize`` → ``POST /2/media/upload/{id}/append``
(one call per chunk, base64 in JSON) → ``POST /2/media/upload/{id}/finalize``,
then ``POST /2/tweets`` with ``media.media_ids``. JPEG, PNG and WebP up to
5 MB, checked from the bytes themselves. Uploading needs the ``media.write``
scope, which is optional: a connection without it (signed in before it was
asked for, or with it declined) gets a 403 on upload, and the post goes out
as text with :attr:`last_post_note` saying "Reconnect X to allow images".

Metrics: ``GET /2/tweets?ids=…&tweet.fields=public_metrics`` only. X bills
every post read, so one run reads at most ``x_metrics_max_reads_per_run``
posts; the rest wait for the next run.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import time
from typing import Any
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
from app.connectors.base.credentials import OAUTH_ACCOUNT_KEY, from_platform_sign_in, secret, text
from app.connectors.base.interfaces import PostMetrics, count_or_none, empty_post_metrics, sum_or_none
from app.connectors.base.oauth import OAuthTokenMixin
from app.core.config import settings
from app.core.exceptions import ConnectorConfigError, ConnectorError
from app.core.logging import get_logger
from app.db.base import utcnow

log = get_logger(__name__)

#: Largest image X accepts in a post.
MAX_IMAGE_BYTES = 5 * 1024 * 1024
#: Bytes per append call. X allows larger segments; small ones keep each
#: request (and its base64 body) modest.
CHUNK_BYTES = 1024 * 1024
#: Status checks after finalize when X is still processing, and the most
#: one pause may be (X's ``check_after_secs`` is honoured up to this).
PROCESSING_CHECKS = 3
MAX_PROCESSING_PAUSE_SECONDS = 5.0
#: Post ids per metrics call (X's documented maximum for GET /2/tweets).
METRICS_BATCH = 100
#: A post id as X writes it.
_POST_ID = re.compile(r"[0-9]{1,25}")
#: A media id as X writes it.
_MEDIA_ID = re.compile(r"[0-9]{1,25}")
_RECONNECT_FOR_IMAGES = "Reconnect X to allow images."


def _data(reply: object) -> dict:
    """The ``data`` object of an X reply, or {} when the reply is not shaped so."""
    inner = reply.get("data") if isinstance(reply, dict) else None
    return inner if isinstance(inner, dict) else {}


def image_type(data: bytes) -> str | None:
    """The image's type from its first bytes, if X accepts it; else None."""
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _pct(value: str) -> str:
    return quote(str(value), safe="~-._")


def oauth1_header(
    method: str,
    url: httpx.URL,
    *,
    consumer_key: str,
    consumer_secret: str,
    token: str,
    token_secret: str,
    nonce: str | None = None,
    timestamp: str | None = None,
) -> str:
    """The ``Authorization: OAuth …`` header for one request (JSON bodies are not signed)."""
    oauth = {
        "oauth_consumer_key": consumer_key,
        "oauth_nonce": nonce or secrets.token_hex(16),
        "oauth_signature_method": "HMAC-SHA1",
        "oauth_timestamp": timestamp or str(int(time.time())),
        "oauth_token": token,
        "oauth_version": "1.0",
    }
    params = [(k, v) for k, v in url.params.multi_items()] + list(oauth.items())
    normalised = "&".join(f"{k}={v}" for k, v in sorted((_pct(k), _pct(v)) for k, v in params))
    base_url = f"{url.scheme}://{url.host}{url.path}"
    base_string = "&".join((method.upper(), _pct(base_url), _pct(normalised)))
    key = f"{_pct(consumer_secret)}&{_pct(token_secret)}"
    signature = base64.b64encode(hmac.new(key.encode(), base_string.encode(), hashlib.sha1).digest()).decode()
    oauth["oauth_signature"] = signature
    return "OAuth " + ", ".join(f'{_pct(k)}="{_pct(v)}"' for k, v in sorted(oauth.items()))


class XTwitterConnector(OAuthTokenMixin, BaseConnector):
    spec = ConnectorSpec(
        slug="x_twitter",
        name="X (Twitter)",
        category="Social media",
        description="Publish posts, with an image, to your X account.",
        fields=(
            secret(
                "accessToken",
                "Access token",
                "",
                help_text=(
                    "From your app's Keys and tokens page in the X developer portal, generated "
                    "after setting the app's permissions to Read and write. An OAuth 2.0 user "
                    "access token also works, but X expires those after two hours."
                ),
            ),
            secret(
                "accessTokenSecret",
                "Access token secret",
                "",
                required=False,
                help_text="Shown with the access token. Leave blank only when using an OAuth 2.0 token.",
            ),
            text(
                "apiKey",
                "API key (consumer key)",
                "",
                required=False,
                help_text="From the same Keys and tokens page. Needed with the access token secret.",
            ),
            secret(
                "apiKeySecret",
                "API key secret (consumer secret)",
                "",
                required=False,
                help_text="From the same Keys and tokens page. Needed with the access token secret.",
            ),
        ),
        capabilities=frozenset({Capability.PUBLISH_SOCIAL}),
        requirements=(
            "An X developer app with Read and write permission, on an X API plan that allows "
            "creating posts.",
            "Images are JPEG, PNG or WebP up to 5 MB. Connections made before images were "
            "supported need to sign in again to post them.",
        ),
        docs_url="https://docs.x.com/x-api/posts/create-post",
        base_url="https://api.x.com/2",
        # POST /2/tweets needs tweet.read, tweet.write and users.read;
        # GET /2/users/me needs users.read and tweet.read; offline.access
        # gives the refresh token that keeps the connection working;
        # media.write uploads a post's image (POST /2/media/upload/*).
        oauth=OAuthBinding(
            provider="x",
            scopes=("tweet.read", "tweet.write", "users.read", "offline.access", "media.write"),
            platform_fields=frozenset(),
            access_summary=(
                "Permission to publish posts, with their images, on your X account, so "
                "the social media agent can post what you approve."
            ),
            choice_noun="account",
            token_fields=frozenset({"accessToken", "accessTokenSecret", "apiKey", "apiKeySecret"}),
        ),
    )

    #: One plain sentence when the last post went out differently from what
    #: was asked (text only, because images are not allowed yet).
    last_post_note = ""

    # Used for a signed-in connection: two-hour access tokens, renewed with
    # single-use refresh tokens that are written back after each refresh.
    token_url = "https://api.x.com/2/oauth2/token"
    token_auth = "basic"

    # ── Auth ───────────────────────────────────────────────────────────────
    @property
    def signed_in(self) -> bool:
        return from_platform_sign_in(self.credentials.values)

    def _oauth1_keys(self) -> tuple[str, str, str, str] | None:
        if self.signed_in:
            return None  # OAuth 2.0 through the platform's app
        token = self.credentials.get("accessToken")
        token_secret = self.credentials.get("accessTokenSecret")
        key = self.credentials.get("apiKey")
        key_secret = self.credentials.get("apiKeySecret")
        if not token:
            raise ConnectorConfigError("X has no access token. Enter it on this integration.")
        if not (token_secret or key or key_secret):
            return None  # an OAuth 2.0 user token
        if not (token_secret and key and key_secret):
            raise ConnectorConfigError(
                "X needs all four values from the Keys and tokens page — API key, API key "
                "secret, access token and access token secret — or an OAuth 2.0 access token alone."
            )
        return key, key_secret, token, token_secret

    def auth_headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.signed_in:
            headers["Authorization"] = "Bearer " + self.access_token
        elif self._oauth1_keys() is None:
            headers["Authorization"] = "Bearer " + self.credentials.get("accessToken")
        return headers

    def _send(self, request: httpx.Request) -> httpx.Response:
        keys = self._oauth1_keys()
        if keys is not None:
            # Signed per attempt: each retry needs a fresh nonce and timestamp.
            request.headers["Authorization"] = oauth1_header(
                request.method,
                request.url,
                consumer_key=keys[0],
                consumer_secret=keys[1],
                token=keys[2],
                token_secret=keys[3],
            )
        response = super()._send(request)
        if response.status_code == 403 and "/media/upload" in request.url.path:
            # A sign-in made before media.write was asked for (or an app
            # without it) may post text but not upload.
            raise ConnectorConfigError(_RECONNECT_FOR_IMAGES)
        if response.status_code == 403:
            detail = response.text.lower()
            if "duplicate" in detail:
                raise ConnectorError("X does not allow a post identical to a recent one.")
            if self.signed_in:
                raise ConnectorConfigError(
                    "X refused to post. The platform's X app needs Read and write "
                    "permission and API credit for posting; ask your platform "
                    "administrator. If you removed the app's access on X, sign in again."
                )
            raise ConnectorConfigError(
                "X refused to post. The app needs Read and write permission (generate the access "
                "token again after changing it), and your X API plan must allow creating posts."
            )
        return response

    # ── Sign-in ────────────────────────────────────────────────────────────
    def list_oauth_choices(self) -> list[OAuthChoice]:
        """The signed-in account itself: nothing to pick, so it connects at once."""
        data = self.request("GET", "/users/me") or {}
        user = data.get("data") or {}
        username = str(user.get("username") or "")
        if not username:
            raise ConnectorError("X did not return the signed-in account.")
        return [
            OAuthChoice(
                id=str(user.get("id") or username),
                label=f"@{username}",
                detail=str(user.get("name") or ""),
                values={OAUTH_ACCOUNT_KEY: f"@{username}"},
            )
        ]

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
        """Publish now, with the image when one is given. Returns (post id, post URL).

        ``image_url`` is not used: X takes the image's bytes only.
        """
        self.last_post_note = ""
        body = text if not link or link in text else f"{text} {link}"
        payload: dict[str, Any] = {"text": body}
        if image_bytes:
            try:
                payload["media"] = {"media_ids": [self.upload_image(image_bytes)]}
            except ConnectorConfigError as exc:
                if str(exc) != _RECONNECT_FOR_IMAGES:
                    raise
                # No media.write: the post still goes out, without its image.
                self.last_post_note = "Posted without its image. " + _RECONNECT_FOR_IMAGES
        data = self.request("POST", "/tweets", json_body=payload)
        post_id = str(((data or {}).get("data") or {}).get("id") or "")
        if not post_id:
            raise ConnectorError("X accepted the post but did not return its id.")
        return post_id, f"https://x.com/i/web/status/{post_id}"

    # ── Images ─────────────────────────────────────────────────────────────
    #: Replaced in tests so the processing wait does not actually sleep.
    _sleep = staticmethod(time.sleep)

    def upload_image(self, data: bytes) -> str:
        """Upload one image in chunks. Returns X's media id for ``media.media_ids``."""
        mime = image_type(data)
        if mime is None:
            raise ConnectorConfigError("X accepts JPEG, PNG or WebP images; this picture is another kind.")
        if len(data) > MAX_IMAGE_BYTES:
            raise ConnectorConfigError(
                f"X accepts images up to 5 MB; this one is {len(data) / (1024 * 1024):.1f} MB."
            )
        started = self.request(
            "POST",
            "/media/upload/initialize",
            json_body={"media_type": mime, "total_bytes": len(data), "media_category": "tweet_image"},
        ) or {}
        media_id = str(_data(started).get("id") or "")
        if not _MEDIA_ID.fullmatch(media_id):
            raise ConnectorError("X did not start the image upload.")
        for index, start in enumerate(range(0, len(data), CHUNK_BYTES)):
            chunk = data[start : start + CHUNK_BYTES]
            self.request(
                "POST",
                f"/media/upload/{media_id}/append",
                json_body={"media": base64.b64encode(chunk).decode(), "segment_index": index},
                expect_json=False,
            )
        finished = self.request("POST", f"/media/upload/{media_id}/finalize") or {}
        self._wait_for_processing(media_id, _data(finished).get("processing_info"))
        return media_id

    def _wait_for_processing(self, media_id: str, info: object) -> None:
        """Images are usually ready at once; when X says otherwise, check a few times."""
        for attempt in range(PROCESSING_CHECKS + 1):
            state = str((info or {}).get("state") or "succeeded").lower() if isinstance(info, dict) else "succeeded"
            if state == "succeeded":
                return
            if state == "failed":
                raise ConnectorError("X could not process the image for this post. Try a different picture.")
            if attempt == PROCESSING_CHECKS:
                break
            pause = (info or {}).get("check_after_secs") if isinstance(info, dict) else None
            seconds = float(pause) if isinstance(pause, (int, float)) and not isinstance(pause, bool) else 1.0
            self._sleep(min(max(seconds, 0.5), MAX_PROCESSING_PAUSE_SECONDS))
            status = self.request(
                "GET", "/media/upload", params={"command": "STATUS", "media_id": media_id}
            ) or {}
            info = _data(status).get("processing_info")
        raise ConnectorError("X is still processing the image for this post. Try publishing again in a few minutes.")

    # ── Metrics ────────────────────────────────────────────────────────────
    def read_post_metrics(self, remote_ids: list[str]) -> dict[str, PostMetrics]:
        """Public figures for each post id: impressions, likes, replies, reposts, bookmarks.

        Only ``public_metrics`` is read (private and organic metrics need the
        post to be under 30 days old and cost more). At most
        ``x_metrics_max_reads_per_run`` posts per call, because X bills each
        post read; ids beyond the cap are left for the next run. Reach,
        clicks and engagements are not in the public figures, so they are
        None.
        """
        cap = max(0, settings.x_metrics_max_reads_per_run)
        wanted: list[str] = []
        for raw in remote_ids or []:
            if len(wanted) >= cap:
                break
            value = str(raw or "").strip()
            if _POST_ID.fullmatch(value) and value not in wanted:
                wanted.append(value)
        if len(remote_ids or []) > len(wanted):
            log.info("x metrics: reading %d of %d posts this run", len(wanted), len(remote_ids or []))
        out: dict[str, PostMetrics] = {}
        for start in range(0, len(wanted), METRICS_BATCH):
            batch = wanted[start : start + METRICS_BATCH]
            data = self.request(
                "GET", "/tweets", params={"ids": ",".join(batch), "tweet.fields": "public_metrics"}
            ) or {}
            for post in data.get("data") or []:
                if not isinstance(post, dict) or str(post.get("id") or "") not in batch:
                    continue
                public = post.get("public_metrics") if isinstance(post.get("public_metrics"), dict) else {}
                figures = empty_post_metrics()
                figures["impressions"] = count_or_none(public.get("impression_count"))
                figures["likes"] = count_or_none(public.get("like_count"))
                figures["comments"] = count_or_none(public.get("reply_count"))
                figures["shares"] = sum_or_none(
                    count_or_none(public.get("retweet_count")), count_or_none(public.get("quote_count"))
                )
                figures["saves"] = count_or_none(public.get("bookmark_count"))
                out[str(post["id"])] = figures
        return out

    def verify_steps(self) -> list:
        """Read the connected account; optionally post once and delete it.

        Under X's pay-per-use pricing every call is billed from the app's
        credit (https://docs.x.com/x-api/getting-started/pricing), so the
        checks are kept to the minimum: one read of the signed-in account,
        and a post only when the operator has opted in with
        ``verifyAllowPaidPost = yes`` on the connection. The post goes out
        through the agents' own ``publish_post`` (``POST /2/tweets``) and is
        removed with ``DELETE /2/tweets/{id}``
        (https://docs.x.com/x-api/posts/delete-post).
        """
        from app.connectors.base import verify_helpers as vh

        def account() -> tuple[str, dict]:
            data = self.request("GET", "/users/me") or {}
            user = data.get("data") or {}
            if not user.get("username"):
                raise ConnectorError("X did not return the connected account.")
            return f"Read the connected X account @{user['username']}.", {"user_id": str(user.get("id") or "")}

        def post_and_delete() -> tuple[str, dict]:
            if (self.credentials.get("verifyAllowPaidPost") or "").strip().lower() != "yes":
                from app.connectors.base.verification import VerifySkipped

                raise VerifySkipped(
                    "Not run: X bills every post and every deletion from the app's API credit "
                    "(at the time of writing about $0.015 to post and $0.010 to delete). To run "
                    "this check anyway, run it with verifyAllowPaidPost=yes."
                )

            def create() -> str:
                post_id, _ = self.publish_post(
                    text=vh.marker("post") + ". Posted by a connection check and deleted straight away."
                )
                return post_id

            def delete(post_id: str) -> None:
                data = self.request("DELETE", f"/tweets/{post_id}") or {}
                if (data.get("data") or {}).get("deleted") is not True:
                    raise ConnectorError("X did not confirm the deletion.")

            evidence = vh.create_check_delete(what="post", create=create, check=None, delete=delete)
            return "Published a short text post on the X account and deleted it.", evidence

        return [
            vh.read_step(
                "account",
                "Read the connected X account (one billed read, about one cent)",
                account,
            ),
            vh.write_step(
                "paid_public_post_roundtrip",
                "Post publicly on the X account and delete it immediately. This costs money under "
                "X's pay-per-use pricing and runs only when verifyAllowPaidPost is yes",
                post_and_delete,
            ),
        ]

    def check_health(self) -> HealthReport:
        try:
            data = self.request("GET", "/users/me")
        except ConnectorError as exc:
            return HealthReport(ok=False, detail=str(exc), checked_at=utcnow())
        username = str(((data or {}).get("data") or {}).get("username") or "")
        if not username:
            return HealthReport(ok=False, detail="X did not return the account.", checked_at=utcnow())
        return HealthReport(ok=True, detail=f"Connected to @{username}", checked_at=utcnow())


CONNECTOR_CLASS = XTwitterConnector
