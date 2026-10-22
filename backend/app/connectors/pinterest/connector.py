"""Pinterest connector — creates Pins on a board through API v5.

``POST /v5/pins`` with ``board_id``, ``title``, ``description``, ``link`` and
the image either inline (``media_source.source_type = "image_base64"``) or by
URL (``"image_url"``). Inline is used whenever the bytes are at hand, so a
Pin does not depend on Pinterest being able to reach this platform.

Connected by signing in with Pinterest (the default), the 30-day access
token is renewed with the refresh token, and the refresh token Pinterest
sends back each time (a "continuous" one, valid 60 days from issue) is
stored in its place. A pasted access token still works, for 30 days.

Metrics (``read_post_metrics``): ``GET /v5/pins/{pin_id}/analytics`` with
``IMPRESSION``, ``SAVE``, ``PIN_CLICK`` and ``OUTBOUND_CLICK`` over the last
:data:`ANALYTICS_DAYS` days (Pinterest allows at most 90). Needs
``pins:read`` and ``user_accounts:read``, which the sign-in already asks for.
"""
from __future__ import annotations

import base64
import re
from datetime import timedelta

from app.connectors.base import verify_helpers as vh
from app.connectors.base.connector import (
    BaseConnector,
    Capability,
    ConnectorSpec,
    HealthReport,
    OAuthBinding,
    OAuthChoice,
)
from app.connectors.base.credentials import from_platform_sign_in, secret, text
from app.connectors.base.interfaces import (
    InsightsAccessMissing,
    PostMetrics,
    count_or_none,
    empty_post_metrics,
)
from app.connectors.base.oauth import OAuthTokenMixin
from app.core.exceptions import ConnectorConfigError, ConnectorError
from app.core.logging import get_logger
from app.db.base import utcnow

log = get_logger(__name__)

TITLE_LIMIT = 100
#: Pinterest's description limit (research R8: 800, not the 500 once used).
DESCRIPTION_LIMIT = 800
#: Days of Pin analytics read; Pinterest refuses a window over 90 days.
ANALYTICS_DAYS = 89
ANALYTICS_METRICS = ("IMPRESSION", "SAVE", "PIN_CLICK", "OUTBOUND_CLICK")
#: Pins read per metrics call (one request each).
MAX_METRIC_POSTS = 200
_PIN_ID = re.compile(r"[0-9]{1,30}")
#: Pages of boards followed during the choose step (up to 250 each).
_MAX_LIST_PAGES = 8

LIVE_HOST = "https://api.pinterest.com/v5"
#: Pinterest's sandbox: same v5 API, its own tokens, and everything made
#: there is visible only to its creator.
#: https://developers.pinterest.com/docs/developer-tools/sandbox/
SANDBOX_HOST = "https://api-sandbox.pinterest.com/v5"
#: A small, long-lived public JPEG (Wikimedia Commons) for the live check's
#: test Pin. ``verifyImageUrl`` on the connection replaces it.
VERIFY_IMAGE_URL = "https://upload.wikimedia.org/wikipedia/commons/3/3f/JPEG_example_flower.jpg"


#: What the sign-in asks for. The four *_secret scopes were added later;
#: a connection signed in before then keeps working for public boards, and
#: the connection check asks for a new sign-in when it needs them.
SIGN_IN_SCOPES = (
    "boards:read",
    "boards:write",
    "boards:read_secret",
    "boards:write_secret",
    "pins:read",
    "pins:write",
    "pins:read_secret",
    "pins:write_secret",
    "user_accounts:read",
)


def parse_boards(items: list[dict]) -> list[OAuthChoice]:
    """Boards out of ``GET /v5/boards`` items."""
    out: list[OAuthChoice] = []
    for board in items:
        board_id = str(board.get("id") or "")
        if not board_id:
            continue
        privacy = str(board.get("privacy") or "").upper()
        parts = []
        if privacy and privacy != "PUBLIC":
            parts.append("Secret board" if privacy == "SECRET" else privacy.capitalize())
        if board.get("pin_count") is not None:
            parts.append(f"{board['pin_count']} Pins")
        out.append(
            OAuthChoice(
                id=board_id,
                label=str(board.get("name") or f"Board {board_id}"),
                detail=" · ".join(parts),
                values={"boardId": board_id},
            )
        )
    return out


class PinterestConnector(vh.EnvironmentChoiceMixin, OAuthTokenMixin, BaseConnector):
    environments = (vh.TEST_ACCOUNT, vh.SANDBOX)

    spec = ConnectorSpec(
        slug="pinterest",
        name="Pinterest",
        category="Social media",
        description="Create Pins with an image and a link on one of your boards.",
        fields=(
            text(
                "boardId",
                "Board ID",
                "549755885175",
                help_text=(
                    "The numeric ID of the board Pins are saved to. GET /v5/boards with your "
                    "token lists your boards and their IDs."
                ),
            ),
            secret(
                "accessToken",
                "Access token",
                "",
                help_text=(
                    "A user access token from your Pinterest app with boards:read, "
                    "boards:write, pins:read, pins:write and user_accounts:read. Pinterest "
                    "access tokens expire after 30 days, then a new one must be entered."
                ),
            ),
            vh.environment_field(
                allowed=(vh.TEST_ACCOUNT, vh.SANDBOX),
                help_text=(
                    "Leave blank for your real Pinterest account. Choose Sandbox to use "
                    "Pinterest's private sandbox, where new boards and Pins are seen only by "
                    "you (it needs a sandbox token from your Pinterest app). Choose Test "
                    "account for a separate Pinterest account kept for testing."
                ),
            ),
        ),
        capabilities=frozenset({Capability.PUBLISH_SOCIAL}),
        requirements=(
            "A Pinterest business account and a Pinterest app with Standard access — apps "
            "with Trial access can only create Pins in Pinterest's sandbox.",
            "Every Pin needs an image and a link to your website.",
        ),
        docs_url="https://developers.pinterest.com/docs/api/v5/pins-create/",
        base_url=LIVE_HOST,
        # POST /v5/pins is documented with boards:read, boards:write,
        # pins:read and pins:write; user_accounts:read names the account
        # (GET /v5/user_account, the health check). The *_secret scopes
        # cover secret boards and their Pins: the connection check makes a
        # secret board so it never posts publicly, and a secret board can
        # also be the one chosen. Scope names:
        # https://developers.pinterest.com/docs/getting-started/set-up-authentication-and-authorization/
        oauth=OAuthBinding(
            provider="pinterest",
            scopes=SIGN_IN_SCOPES,
            platform_fields=frozenset(),
            access_summary=(
                "Permission to create Pins on the board you choose, so the social media "
                "agent can publish what you approve, including secret boards, which the "
                "platform uses to test the connection without posting publicly. It "
                "deletes only the test board and Pin it makes for that check."
            ),
            choice_noun="board",
            choice_fields=("boardId",),
            token_fields=frozenset({"accessToken"}),
        ),
    )

    token_auth = "basic"

    @property
    def base_url(self) -> str:
        return SANDBOX_HOST if self.account_environment() == vh.SANDBOX else LIVE_HOST

    @property
    def token_url(self) -> str:  # type: ignore[override]
        return f"{self.base_url}/oauth/token"

    def auth_headers(self) -> dict[str, str]:
        return {"Authorization": "Bearer " + self.access_token, "Content-Type": "application/json"}

    def list_oauth_choices(self) -> list[OAuthChoice]:
        items: list[dict] = []
        bookmark = ""
        for _ in range(_MAX_LIST_PAGES):
            params: dict = {"page_size": 250}
            if bookmark:
                params["bookmark"] = bookmark
            data = self.request("GET", "/boards", params=params) or {}
            items.extend(b for b in data.get("items") or [] if isinstance(b, dict))
            bookmark = str(data.get("bookmark") or "")
            if not bookmark:
                break
        return parse_boards(items)

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
        """Create a Pin now. Returns (Pin id, Pin URL)."""
        if not link:
            raise ConnectorConfigError("A Pin needs a link to your website, and this post has none.")
        if image_bytes:
            media = {
                "source_type": "image_base64",
                "content_type": "image/jpeg" if image_mime == "image/jpeg" else "image/png",
                "data": base64.b64encode(image_bytes).decode(),
            }
        elif image_url:
            media = {"source_type": "image_url", "url": image_url}
        else:
            raise ConnectorConfigError("A Pin needs an image, and this post has none.")
        data = self.request(
            "POST",
            "/pins",
            json_body={
                "board_id": self.credentials.require("boardId").strip(),
                "title": title[:TITLE_LIMIT],
                "description": text[:DESCRIPTION_LIMIT],
                "link": link,
                "media_source": media,
            },
        )
        pin_id = str((data or {}).get("id") or "")
        if not pin_id:
            raise ConnectorError("Pinterest accepted the Pin but did not return its id.")
        return pin_id, f"https://www.pinterest.com/pin/{pin_id}/"

    # ── Metrics ────────────────────────────────────────────────────────────
    def read_post_metrics(self, remote_ids: list[str]) -> dict[str, PostMetrics]:
        """Figures for each Pin id over the last :data:`ANALYTICS_DAYS` days.

        Impressions, saves and clicks (``PIN_CLICK``, every click on the Pin;
        ``OUTBOUND_CLICK`` is a part of it). Pinterest reports no reach,
        likes, comments or shares here, so they are None. A Pin Pinterest
        will not report on is left out; a 403 means the sign-in lacks the
        permission.

        These are *rolling-window* totals, not lifetime totals: Pinterest
        refuses a window over 90 days, so for a Pin older than that the
        figures cover only its last :data:`ANALYTICS_DAYS` days and can go
        down from one run to the next as old activity leaves the window.
        Other networks report lifetime totals, so Pinterest's are not
        comparable with theirs for older Pins. Only when the window's summary
        is missing does :func:`_analytics_totals` fall back to Pinterest's
        lifetime figures. Social metrics only read posts published within
        ``social_metrics_lookback_days`` (at most 366), so set that to 90 or
        less for Pinterest figures that are whole-life.
        """
        end = utcnow().date()
        start = end - timedelta(days=ANALYTICS_DAYS)
        params = {
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "metric_types": ",".join(ANALYTICS_METRICS),
        }
        out: dict[str, PostMetrics] = {}
        seen: set[str] = set()
        for raw in (remote_ids or [])[:MAX_METRIC_POSTS]:
            pin_id = str(raw or "").strip()
            if not _PIN_ID.fullmatch(pin_id) or pin_id in seen:
                continue
            seen.add(pin_id)
            try:
                data = self.request("GET", f"/pins/{pin_id}/analytics", params=params) or {}
            except ConnectorConfigError:
                raise
            except ConnectorError as exc:
                status = getattr(exc, "http_status", None)
                if status == 403:
                    raise InsightsAccessMissing("Pinterest") from None
                if status in (400, 404):
                    log.info("pinterest metrics: pin skipped (HTTP %s)", status)
                    continue
                raise
            figures = empty_post_metrics()
            totals = _analytics_totals(data)
            figures["impressions"] = count_or_none(totals.get("IMPRESSION"))
            figures["saves"] = count_or_none(totals.get("SAVE"))
            figures["clicks"] = count_or_none(totals.get("PIN_CLICK"))
            out[pin_id] = figures
        return out

    def verify_steps(self) -> list:
        """Read the account and the chosen board; make a secret board with one Pin, then delete both.

        The write step never touches the board the connection posts to: it
        makes its own SECRET board (``POST /v5/boards``), creates one Pin on
        it (``POST /v5/pins``, the agents' call), reads the Pin back, and
        deletes the Pin and then the board. In the sandbox
        (``api-sandbox.pinterest.com``) everything is visible only to its
        creator anyway.
        https://developers.pinterest.com/docs/api/v5/boards-create/
        https://developers.pinterest.com/docs/developer-tools/sandbox/
        """

        def account() -> tuple[str, dict]:
            data = self.request("GET", "/user_account") or {}
            username = str(data.get("username") or "")
            if not username:
                raise ConnectorError("Pinterest did not return the account.")
            return f"Read the Pinterest account {username}.", {"account_type": str(data.get("account_type") or "")}

        def board() -> tuple[str, dict]:
            board_id = (self.credentials.get("boardId") or "").strip()
            if not board_id:
                data = self.request("GET", "/boards", params={"page_size": 5}) or {}
                rows = [b for b in data.get("items") or [] if isinstance(b, dict)]
                return (
                    f"No board is chosen yet; the account has at least {vh.plural(len(rows), 'board')}.",
                    {"count": len(rows)},
                )
            data = self.request("GET", f"/boards/{board_id}") or {}
            if not data.get("id"):
                raise ConnectorError("Pinterest did not return the chosen board.")
            return (
                f"Read the board '{data.get('name') or board_id}' that Pins are saved to.",
                {"board_id": str(data["id"]), "pin_count": data.get("pin_count")},
            )

        def secret_board_and_pin() -> tuple[str, dict]:
            image_url = (self.credentials.get("verifyImageUrl") or VERIFY_IMAGE_URL).strip()
            pin_evidence: dict = {}

            def create_board() -> str:
                data = self.request(
                    "POST",
                    "/boards",
                    json_body={
                        "name": vh.marker("board"),
                        "description": "Secret board made by a connection check. Safe to delete.",
                        "privacy": "SECRET",
                    },
                ) or {}
                return str(data.get("id") or "")

            def check_board(board_id: str) -> None:
                def create_pin() -> str:
                    data = self.request(
                        "POST",
                        "/pins",
                        json_body={
                            "board_id": board_id,
                            "title": vh.marker("Pin")[:TITLE_LIMIT],
                            "description": "Made by a connection check. Safe to delete.",
                            "link": "https://example.com/",
                            "media_source": {"source_type": "image_url", "url": image_url},
                        },
                    ) or {}
                    return str(data.get("id") or "")

                def check_pin(pin_id: str) -> None:
                    row = self.request("GET", f"/pins/{pin_id}") or {}
                    if str(row.get("board_id") or "") != board_id:
                        raise ConnectorError("Pinterest returned the test Pin on a different board.")

                def delete_pin(pin_id: str) -> None:
                    self.request("DELETE", f"/pins/{pin_id}", expect_json=False)

                pin_evidence.update(
                    vh.create_check_delete(what="Pin", create=create_pin, check=check_pin, delete=delete_pin)
                )

            def delete_board(board_id: str) -> None:
                self.request("DELETE", f"/boards/{board_id}", expect_json=False)

            evidence = vh.create_check_delete(
                what="secret board", create=create_board, check=check_board, delete=delete_board
            )
            return (
                "Made a secret board with one Pin, read the Pin back, and deleted the Pin and the board.",
                {
                    "board_id": evidence["created_id"],
                    "pin_id": pin_evidence.get("created_id", ""),
                    "deleted": True,
                },
            )

        return [
            vh.read_step("account", "Read the Pinterest account", account),
            vh.read_step("board", "Read the board Pins are saved to", board),
            vh.write_step(
                "secret_board_pin_roundtrip",
                "Make a secret board with one Pin, read it back, then delete the Pin and the board "
                "(the board Pins are normally saved to is not touched)",
                secret_board_and_pin,
            ),
        ]

    def diagnose(self, error: Exception) -> str | None:
        """A refusal on a sign-in made before the secret-board scopes were
        requested: signing in again grants them."""
        # Pinterest is documented to refuse a missing scope with 403, but some
        # endpoints answer 401 with a scope message; treat both as a scope
        # refusal when the vendor text names a scope (a plain 401 is an
        # expired or revoked sign-in, handled by the generic message).
        status = getattr(error, "http_status", None)
        body = str(getattr(error, "vendor_body", "") or "").lower()
        scope_refusal = status == 403 or (status == 401 and "scope" in body)
        if scope_refusal and from_platform_sign_in(
            self.credentials.values
        ):
            return (
                "Pinterest refused a permission this sign-in was not given (secret "
                "boards need it). Sign in again to grant the new permission."
            )
        return None

    def check_health(self) -> HealthReport:
        try:
            data = self.request("GET", "/user_account")
        except ConnectorError as exc:
            return HealthReport(ok=False, detail=str(exc), checked_at=utcnow())
        username = str((data or {}).get("username") or "")
        if not username:
            return HealthReport(ok=False, detail="Pinterest did not return the account.", checked_at=utcnow())
        return HealthReport(ok=True, detail=f"Connected to {username}", checked_at=utcnow())


def _analytics_totals(data: object) -> dict[str, object]:
    """The window's totals from a Pin analytics reply.

    The reply is ``{"all": {"summary_metrics": {...}, "lifetime_metrics":
    {...}, "daily_metrics": [...]}}``; the window's summary is used, and the
    lifetime figures when the summary is absent.
    """
    block = data.get("all") if isinstance(data, dict) else None
    if not isinstance(block, dict):
        return {}
    for key in ("summary_metrics", "lifetime_metrics"):
        found = block.get(key)
        if isinstance(found, dict) and found:
            return found
    return {}


CONNECTOR_CLASS = PinterestConnector
