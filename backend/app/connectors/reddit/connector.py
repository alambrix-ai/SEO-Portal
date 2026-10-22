"""Reddit connector — posts to one subreddit through Reddit's OAuth API.

Switched off by default. Reddit closed self-service API access in late 2025
and requires its written approval for commercial use, so nothing here runs
until the operator sets ``REDDIT_ENABLED`` (and the sign-in provider stays
unavailable until then, see ``oauth_providers.REDDIT``).

* **Sign in with Reddit** (the default): ``duration=permanent`` gives a
  refresh token; access tokens last an hour. A refresh does *not* return a
  new refresh token, so :class:`OAuthTokenMixin` keeps the original (it only
  replaces a stored one when a different one comes back). The refresh call
  is made here because Reddit needs Basic client auth *and* its User-Agent
  on it.
* **User-Agent**: Reddit throttles or refuses generic agents, so every call
  carries ``settings.reddit_user_agent`` (or the value saved in Portal
  Admin). Unset, the connector refuses with a setup message.
* **Rate limits**: ``X-Ratelimit-Remaining`` / ``X-Ratelimit-Reset`` are
  read from every response; below two remaining calls, or on a 429, the
  call is deferred (:class:`RedditRateLimited`, with ``retry_after_seconds``)
  rather than retried. No fixed per-minute figure is assumed.
* **Posting**: before submitting, ``/api/v1/{sub}/post_requirements`` and
  ``/r/{sub}/about`` are read and the post checked against them, refusing
  with a plain reason. The field names of ``post_requirements`` are not
  documented, so each is parsed defensively and ignored when absent or of an
  unexpected type. Its ``title_regexes`` are deliberately **not** run here:
  they are moderator-written patterns, and running them in this process
  would let a hostile pattern stall it (catastrophic backtracking). Reddit
  enforces them itself on submit, and that refusal is translated.
* **Images**: Reddit's native image upload (``media/asset.json`` plus an
  upload and a websocket) is undocumented, so it is not used. An image is
  posted as a link post to the image's public address; with image bytes
  only, the post goes out without the image and :attr:`last_post_note`
  says so.
* **Spacing**: posts to the same subreddit from one workspace are kept at
  least ``reddit_min_minutes_between_posts`` apart (in this process).
* **Metrics**: ``/api/info`` reports score, upvote ratio and comment count
  only; everything else is ``None`` ("not reported by Reddit").

Tokens and response bodies are never logged.
"""
from __future__ import annotations

import base64
import re
import threading
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential_jitter

from app.connectors.base import verify_helpers as vh
from app.connectors.base.connector import (
    BaseConnector,
    Capability,
    ConnectorSpec,
    HealthReport,
    OAuthBinding,
    OAuthChoice,
)
from app.connectors.base.credentials import text
from app.connectors.base.oauth import OAuthTokenMixin, token_fields
from app.connectors.base.oauth_providers import REDDIT, platform_setting
from app.core.config import settings
from app.core.exceptions import ConnectorConfigError, ConnectorError
from app.core.logging import get_logger
from app.db.base import utcnow
from app.models.social import POST_METRIC_FIELDS

log = get_logger(__name__)

API_HOST = "https://oauth.reddit.com"
#: Scopes (research R10): who signed in, their subreddits, posting, reading.
SIGN_IN_SCOPES = ("identity", "mysubreddits", "submit", "read")

#: Reddit's own rule for subreddit names.
SUBREDDIT_RE = re.compile(r"[A-Za-z0-9_]{3,21}", re.ASCII)
#: A post's "fullname": ``t3_`` plus a base-36 id.
FULLNAME_RE = re.compile(r"t3_[a-z0-9]{1,13}", re.ASCII)
_POST_ID_RE = re.compile(r"[a-z0-9]{1,13}", re.ASCII)
#: Printable ASCII only: the value goes into a header.
_USER_AGENT_RE = re.compile(r"[\x20-\x7e]{8,256}", re.ASCII)
_AFTER_RE = re.compile(r"t5_[a-z0-9]{1,16}", re.ASCII)

TITLE_LIMIT = 300
BODY_LIMIT = 40_000
URL_LIMIT = 2_000
#: Defer once fewer calls than this remain in Reddit's window.
_LOW_REMAINING = 2.0
#: Longest a deferral is ever stated for (Reddit's window is ten minutes).
_MAX_DEFER_SECONDS = 3_600
#: Pages of subreddits read for the choose step (100 each), per list.
_MAX_LIST_PAGES = 5
#: Posts per ``/api/info`` call, and per ``read_post_metrics`` call overall.
_INFO_BATCH = 100
_MAX_METRIC_IDS = 1_000
#: Bounds on what is read out of ``post_requirements`` lists.
_MAX_RULE_ITEMS = 50
_MAX_RULE_TEXT = 100

_SWITCHED_OFF = (
    "Reddit is switched off on this platform. Reddit has to approve business use before it "
    "can be connected; ask your platform administrator."
)
_NOT_SET_UP = (
    "Reddit isn't fully set up on this platform yet. Ask your platform administrator to "
    "finish the Reddit setup under Sign-in providers."
)
_SIGN_IN_AGAIN = (
    "Reddit no longer accepts this connection's sign-in. Open Reddit under Integrations and "
    "choose Sign in again."
)


# ── Settings ───────────────────────────────────────────────────────────────
def reddit_enabled() -> bool:
    return bool(settings.reddit_enabled)


def user_agent_or_none() -> str:
    """The User-Agent Reddit requires, or ``""`` when unset or malformed."""
    value = (platform_setting("reddit_user_agent") or "").strip()
    return value if _USER_AGENT_RE.fullmatch(value) else ""


def normalise_subreddit(raw: str | None) -> str:
    """``"r/Marketing"`` → ``"Marketing"``; anything else not a valid name is refused."""
    value = (raw or "").strip()
    for prefix in ("/r/", "r/"):
        if value.lower().startswith(prefix):
            value = value[len(prefix):]
            break
    value = value.rstrip("/")
    if not SUBREDDIT_RE.fullmatch(value):
        raise ConnectorConfigError(
            "That isn't a valid subreddit name. Use the name without r/: 3 to 21 letters, "
            "numbers or underscores."
        )
    return value


# ── Errors ─────────────────────────────────────────────────────────────────
class RedditRateLimited(ConnectorError):
    """Reddit asked us to slow down: try again after ``retry_after_seconds``."""

    status_code = 429
    code = "rate_limited"

    def __init__(self, message: str, *, retry_after: float) -> None:
        seconds = int(min(max(retry_after, 1.0), _MAX_DEFER_SECONDS))
        super().__init__(message, details={"retry_after_seconds": seconds})
        self.retry_after_seconds = seconds


class RedditPostRefused(ConnectorConfigError):
    """The post breaks the subreddit's rules; retrying as-is cannot help."""

    code = "post_refused"


def _defer_message(seconds: float) -> str:
    minutes = max(1, int((seconds + 59) // 60))
    return (
        f"Reddit asked us to slow down. The post will go out once it allows more, in about "
        f"{minutes} minute{'s' if minutes != 1 else ''}."
    )


# ── Shared, per-process state ──────────────────────────────────────────────
_state_lock = threading.Lock()
#: connection key -> (calls remaining, epoch seconds when the window resets)
_rate_windows: dict[str, tuple[float, float]] = {}
#: (connection key, subreddit lower-case) -> epoch seconds of the last post
_last_posts: dict[tuple[str, str], float] = {}


def reset_state() -> None:
    """Forget remembered rate windows and post times (tests)."""
    with _state_lock:
        _rate_windows.clear()
        _last_posts.clear()


def _header_float(response: httpx.Response, name: str) -> float | None:
    raw = response.headers.get(name)
    if raw is None:
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    return value if value == value and value >= 0 else None  # drops NaN


# ── Post requirements ──────────────────────────────────────────────────────
def _int_or_none(value: Any) -> int | None:  # noqa: ANN401
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value) if value >= 0 else None


def _strings(value: Any) -> tuple[str, ...]:  # noqa: ANN401
    if not isinstance(value, list):
        return ()
    out = [str(v).strip()[:_MAX_RULE_TEXT] for v in value[:_MAX_RULE_ITEMS] if isinstance(v, str)]
    return tuple(v for v in out if v)


def _domains(value: Any) -> tuple[str, ...]:  # noqa: ANN401
    return tuple(d.lower().removeprefix("www.") for d in _strings(value))


@dataclass(frozen=True, slots=True)
class PostRules:
    """What ``post_requirements`` and ``about`` say, parsed defensively."""

    title_min: int | None = None
    title_max: int | None = None
    title_required: tuple[str, ...] = ()
    title_banned: tuple[str, ...] = ()
    body_min: int | None = None
    body_max: int | None = None
    body_banned: tuple[str, ...] = ()
    #: ``required``, ``notallowed`` or ``""`` (no rule).
    body_policy: str = ""
    #: ``whitelist``, ``blacklist`` or ``""``.
    link_policy: str = ""
    domains_allowed: tuple[str, ...] = ()
    domains_banned: tuple[str, ...] = ()
    flair_required: bool = False
    #: Counted for the log only; never run (see the module docstring).
    title_regex_count: int = 0
    # From /about.
    exists: bool = True
    #: ``any``, ``link``, ``self`` or ``""`` (unknown).
    submission_type: str = ""
    allow_images: bool | None = None
    banned: bool = False
    members_only: bool = False


def parse_rules(requirements: Any, about: Any) -> PostRules:  # noqa: ANN401
    req = requirements if isinstance(requirements, dict) else {}
    body_policy = str(req.get("body_restriction_policy") or "").strip().lower()
    link_policy = str(req.get("link_restriction_policy") or "").strip().lower()

    exists, submission_type, allow_images, banned, members_only = True, "", None, False, False
    if isinstance(about, dict):
        data = about.get("data") if isinstance(about.get("data"), dict) else {}
        # A missing subreddit comes back as a search listing, not a "t5".
        exists = about.get("kind") == "t5" and bool(data)
        kind = str(data.get("submission_type") or "").strip().lower()
        submission_type = kind if kind in ("any", "link", "self") else ""
        if isinstance(data.get("allow_images"), bool):
            allow_images = data["allow_images"]
        banned = data.get("user_is_banned") is True
        restricted = str(data.get("subreddit_type") or "").lower() in (
            "restricted", "private", "archived", "gold_restricted", "employees_only",
        )
        approved = data.get("user_is_contributor") is True or data.get("user_is_moderator") is True
        members_only = restricted and not approved

    return PostRules(
        title_min=_int_or_none(req.get("title_text_min_length")),
        title_max=_int_or_none(req.get("title_text_max_length")),
        title_required=_strings(req.get("title_required_strings")),
        title_banned=_strings(req.get("title_blacklisted_strings")),
        body_min=_int_or_none(req.get("body_text_min_length")),
        body_max=_int_or_none(req.get("body_text_max_length")),
        body_banned=_strings(req.get("body_blacklisted_strings")),
        body_policy=body_policy if body_policy in ("required", "notallowed") else "",
        link_policy=link_policy if link_policy in ("whitelist", "blacklist") else "",
        domains_allowed=_domains(req.get("domain_whitelist")),
        domains_banned=_domains(req.get("domain_blacklist")),
        flair_required=req.get("is_flair_required") is True,
        title_regex_count=len(req.get("title_regexes") or []) if isinstance(req.get("title_regexes"), list) else 0,
        exists=exists,
        submission_type=submission_type,
        allow_images=allow_images,
        banned=banned,
        members_only=members_only,
    )


def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower().removeprefix("www.")


def _domain_in(host: str, domains: tuple[str, ...]) -> bool:
    return any(host == d or host.endswith("." + d) for d in domains)


def _quoted(items: tuple[str, ...]) -> str:
    return ", ".join(f"“{i}”" for i in items[:5])


def check_post(rules: PostRules, *, subreddit: str, title: str, kind: str, url: str, body: str) -> str:
    """The plain reason this post breaks the subreddit's rules, or ``""``."""
    where = f"r/{subreddit}"
    if not rules.exists:
        return f"{where} doesn't exist or can't be seen by this Reddit account. Choose another subreddit."
    if rules.banned:
        return f"This Reddit account is banned from {where}, so it can't post there."
    if rules.members_only:
        return f"{where} only accepts posts from approved members. Choose another subreddit."
    if rules.flair_required:
        return (
            f"{where} requires a post flair, which can't be chosen here yet. Post it on Reddit "
            "yourself, or choose another subreddit."
        )
    if kind == "link" and rules.submission_type == "self":
        return f"{where} accepts text posts only."
    if kind == "self" and rules.submission_type == "link":
        return f"{where} accepts link posts only, and this post has no link."
    if rules.title_min is not None and len(title) < rules.title_min:
        return f"{where} needs titles of at least {rules.title_min} characters; this one has {len(title)}."
    if rules.title_max is not None and len(title) > rules.title_max:
        return f"{where} allows titles of up to {rules.title_max} characters; this one has {len(title)}."
    lowered = title.lower()
    missing = tuple(s for s in rules.title_required if s.lower() not in lowered)
    if rules.title_required and len(missing) == len(rules.title_required):
        return f"{where} requires the title to include one of: {_quoted(rules.title_required)}."
    banned = tuple(s for s in rules.title_banned if s.lower() in lowered)
    if banned:
        return f"{where} does not allow {_quoted(banned)} in titles."
    if kind == "self":
        if rules.body_policy == "notallowed" and body:
            return f"{where} does not allow text in posts. Shorten it to a title, or choose another subreddit."
        if rules.body_policy == "required" and not body:
            return f"{where} requires text in every post, and this one has none."
        if rules.body_min is not None and len(body) < rules.body_min:
            return f"{where} needs post text of at least {rules.body_min} characters."
        if rules.body_max is not None and len(body) > rules.body_max:
            return f"{where} allows post text of up to {rules.body_max} characters."
        hit = tuple(s for s in rules.body_banned if s.lower() in body.lower())
        if hit:
            return f"{where} does not allow {_quoted(hit)} in posts."
    if kind == "link":
        if rules.body_policy == "required":
            return f"{where} requires text in every post, so link posts can't be made there."
        host = _host(url)
        if rules.link_policy == "whitelist" and rules.domains_allowed and not _domain_in(host, rules.domains_allowed):
            return f"{where} only accepts links to certain websites, and {host or 'this link'} isn't one of them."
        if rules.link_policy == "blacklist" and _domain_in(host, rules.domains_banned):
            return f"{where} does not accept links to {host}."
    return ""


#: Reddit's submit error codes, in plain words. ``{where}`` is r/<name>.
_SUBMIT_ERRORS = {
    "SUBREDDIT_NOEXIST": "{where} doesn't exist. Choose another subreddit.",
    "SUBREDDIT_NOTALLOWED": "This Reddit account isn't allowed to post in {where}.",
    "SUBREDDIT_REQUIRES_BODY": "{where} requires text in every post.",
    "NO_SELFS": "{where} accepts link posts only.",
    "NO_LINKS": "{where} accepts text posts only.",
    "ALREADY_SUB": "This link has already been posted to {where}.",
    "TOO_LONG": "The title or text is longer than {where} allows.",
    "NO_TEXT": "Reddit needs a title for this post.",
    "BAD_URL": "Reddit did not accept the link in this post.",
    "BANNED_FROM_SUBREDDIT": "This Reddit account is banned from {where}.",
    "SUBMIT_VALIDATION_FLAIR_REQUIRED": "{where} requires a post flair, which can't be chosen here yet.",
    "SUBMIT_VALIDATION_TITLE_REGEX_REQUIREMENT": "The title doesn't follow {where}'s title rules.",
}
_RATELIMIT_WAIT_RE = re.compile(r"(\d{1,4})\s*(second|minute|hour)", re.I)


def _valid_url(url: str) -> bool:
    if not url or len(url) > URL_LIMIT:
        return False
    parts = urlsplit(url)
    return parts.scheme in ("http", "https") and bool(parts.hostname)


class RedditConnector(OAuthTokenMixin, BaseConnector):
    spec = ConnectorSpec(
        slug="reddit",
        name="Reddit",
        category="Social media",
        description=(
            "Post to a subreddit you choose. Needs Reddit's approval: Reddit only allows business "
            "use once it has approved it in writing, so Reddit stays switched off until your "
            "platform administrator turns it on."
        ),
        fields=(
            text(
                "subreddit",
                "Subreddit",
                "marketing",
                help_text="The community posts go to, without r/: 3 to 21 letters, numbers or underscores.",
            ),
            *token_fields(
                refreshable=True,
                vendor="Reddit",
                docs="Scopes: identity, mysubreddits, submit, read.",
            ),
        ),
        capabilities=frozenset({Capability.PUBLISH_SOCIAL}),
        requirements=(
            "Reddit's written approval for business use. Reddit no longer gives this access "
            "automatically.",
            "Each post goes to one subreddit and must follow that community's posting rules.",
            "Images are shared as a link to the picture; Reddit's own image upload is not used.",
        ),
        docs_url="https://www.reddit.com/dev/api/",
        base_url=API_HOST,
        oauth=OAuthBinding(
            provider="reddit",
            scopes=SIGN_IN_SCOPES,
            access_summary=(
                "Permission to see the communities you belong to and to post in the one you "
                "choose, so the social media agent can publish what you approve."
            ),
            choice_noun="subreddit",
            choice_fields=("subreddit",),
        ),
    )

    token_url = REDDIT.token_url
    token_auth = "basic"

    def __init__(self, *args: Any, **kwargs: Any) -> None:  # noqa: ANN401
        super().__init__(*args, **kwargs)
        #: One plain sentence about how the last post went out, when it
        #: differs from what was asked (no image, text post instead of link).
        self.last_post_note = ""

    # ── Availability and auth ──────────────────────────────────────────────
    def _user_agent(self) -> str:
        """Refuse while Reddit is off or cannot be called; else the User-Agent."""
        if not reddit_enabled():
            raise ConnectorConfigError(_SWITCHED_OFF)
        agent = user_agent_or_none()
        if not agent:
            log.warning("reddit: reddit_user_agent is unset or malformed")
            raise ConnectorConfigError(_NOT_SET_UP)
        return agent

    def auth_headers(self) -> dict[str, str]:
        agent = self._user_agent()
        return {"Authorization": "Bearer " + self.access_token, "User-Agent": agent}

    def _refresh_call(self, refresh: str) -> dict:
        """Reddit's refresh: Basic client auth plus its User-Agent.

        No new refresh token comes back; the mixin keeps the original.
        """
        agent = self._user_agent()
        client_id, client_secret = self._client_credentials()
        if not client_id or not client_secret:
            raise ConnectorConfigError(
                "Reddit has a saved sign-in but not the app details that go with it. "
                "Sign in again under Integrations."
            )
        raw = f"{client_id}:{client_secret}".encode()
        headers = {
            "Authorization": "Basic " + base64.b64encode(raw).decode(),
            "User-Agent": agent,
            "Accept": "application/json",
        }
        try:
            with httpx.Client(timeout=settings.connector_timeout_seconds) as client:
                response = client.post(
                    self.token_url,
                    data={"grant_type": "refresh_token", "refresh_token": refresh},
                    headers=headers,
                )
        except httpx.HTTPError as exc:
            log.warning("reddit token refresh unreachable: %s", type(exc).__name__)
            from app.core.user_messages import message_for_unreachable

            raise ConnectorError(message_for_unreachable(self.name)) from None
        try:
            data = response.json()
        except ValueError:
            data = {}
        data = data if isinstance(data, dict) else {}
        if response.status_code >= 400 or not data.get("access_token"):
            # The error code only, bounded; never the body or a token.
            log.warning(
                "reddit token refresh refused (%s): %s",
                response.status_code,
                str(data.get("error") or "no access_token")[:80],
            )
            raise ConnectorConfigError(_SIGN_IN_AGAIN)
        return data

    # ── Rate limits ────────────────────────────────────────────────────────
    @property
    def _window_key(self) -> str:
        return self.org_id or "-"

    def _respect_rate_limit(self) -> None:
        with _state_lock:
            window = _rate_windows.get(self._window_key)
        if window is None:
            return
        remaining, reset_at = window
        wait = reset_at - time.time()
        if remaining < _LOW_REMAINING and wait > 0:
            log.info("reddit: deferring, %.0f calls left for %.0fs", remaining, wait)
            raise RedditRateLimited(_defer_message(wait), retry_after=wait)

    def _note_rate_limit(self, response: httpx.Response) -> None:
        remaining = _header_float(response, "X-Ratelimit-Remaining")
        reset = _header_float(response, "X-Ratelimit-Reset")
        if remaining is None or reset is None:
            return
        reset_at = time.time() + min(reset, _MAX_DEFER_SECONDS)
        with _state_lock:
            _rate_windows[self._window_key] = (remaining, reset_at)

    @retry(
        retry=retry_if_exception_type((httpx.TransportError, httpx.HTTPStatusError)),
        stop=stop_after_attempt(settings.connector_max_attempts),
        wait=wait_exponential_jitter(initial=0.5, max=8),
        reraise=True,
    )
    def _send(self, request: httpx.Request) -> httpx.Response:
        """Like the base, but a 429 is deferred, never retried in a loop."""
        self._respect_rate_limit()
        response = self._http().send(request)
        self._note_rate_limit(response)
        if response.status_code == 429:
            wait = (
                _header_float(response, "X-Ratelimit-Reset")
                or _header_float(response, "Retry-After")
                or 60.0
            )
            with _state_lock:
                _rate_windows[self._window_key] = (0.0, time.time() + min(wait, _MAX_DEFER_SECONDS))
            log.info("reddit: 429 on %s, deferring %.0fs", request.url.path, wait)
            raise RedditRateLimited(_defer_message(wait), retry_after=wait)
        if response.status_code in (500, 502, 503, 504):
            response.raise_for_status()
        return response

    # ── Choose a subreddit ─────────────────────────────────────────────────
    def list_oauth_choices(self) -> list[OAuthChoice]:
        """Subreddits the account moderates, then those it has joined."""
        seen: dict[str, OAuthChoice] = {}
        for where, detail in (("moderator", "You moderate it"), ("subscriber", "")):
            after = ""
            for _ in range(_MAX_LIST_PAGES):
                params: dict[str, Any] = {"limit": 100, "raw_json": 1}
                if after:
                    params["after"] = after
                data = self.request("GET", f"/subreddits/mine/{where}", params=params) or {}
                listing = data.get("data") if isinstance(data, dict) and isinstance(data.get("data"), dict) else {}
                for child in listing.get("children") or []:
                    sub = child.get("data") if isinstance(child, dict) else None
                    if not isinstance(sub, dict) or sub.get("user_is_banned") is True:
                        continue
                    name = str(sub.get("display_name") or "")
                    if not SUBREDDIT_RE.fullmatch(name) or name.lower() in seen:
                        continue
                    count = _int_or_none(sub.get("subscribers"))
                    parts = [p for p in (detail, f"{count:,} members" if count is not None else "") if p]
                    seen[name.lower()] = OAuthChoice(
                        id=name,
                        label=f"r/{name}",
                        detail=" · ".join(parts),
                        values={"subreddit": name},
                    )
                after = str(listing.get("after") or "")
                if not _AFTER_RE.fullmatch(after):
                    break
        return list(seen.values())

    # ── Publishing ─────────────────────────────────────────────────────────
    def _check_spacing(self, subreddit: str) -> None:
        gap = settings.reddit_min_minutes_between_posts * 60
        with _state_lock:
            last = _last_posts.get((self._window_key, subreddit.lower()))
        if last is None:
            return
        wait = last + gap - time.time()
        if wait > 0:
            minutes = max(1, int((wait + 59) // 60))
            raise RedditRateLimited(
                f"Posts to r/{subreddit} are kept at least {settings.reddit_min_minutes_between_posts} "
                f"minutes apart. This one can go out in about {minutes} minute{'s' if minutes != 1 else ''}.",
                retry_after=wait,
            )

    def _record_post(self, subreddit: str) -> None:
        with _state_lock:
            _last_posts[(self._window_key, subreddit.lower())] = time.time()

    def read_rules(self, subreddit: str) -> PostRules:
        """The subreddit's posting rules. ``post_requirements`` is best effort."""
        about = self.request("GET", f"/r/{subreddit}/about", params={"raw_json": 1})
        try:
            requirements = self.request("GET", f"/api/v1/{subreddit}/post_requirements")
        except (RedditRateLimited, ConnectorConfigError):
            raise
        except ConnectorError as exc:
            # Undocumented endpoint: its absence is not a reason to refuse.
            log.info("reddit: post_requirements unavailable (%s)", getattr(exc, "http_status", "?"))
            requirements = {}
        rules = parse_rules(requirements, about)
        if rules.title_regex_count:
            log.info("reddit: %d title patterns left for Reddit to check", rules.title_regex_count)
        return rules

    def publish_post(
        self,
        *,
        text: str,
        link: str = "",
        image_bytes: bytes | None = None,
        image_mime: str = "",
        image_url: str = "",
        title: str = "",
        subreddit: str = "",
    ) -> tuple[str, str]:
        """Submit one post now. Returns (post fullname ``t3_…``, post URL)."""
        self.last_post_note = ""
        self._user_agent()
        sub = normalise_subreddit(subreddit or self.credentials.get("subreddit"))
        self._check_spacing(sub)

        body = (text or "").strip()
        # The title as given; failing that, the first line of the text.
        heading = " ".join((title or "").split())
        if not heading and body:
            heading = " ".join(body.splitlines()[0].split())
        if not heading:
            raise RedditPostRefused("A Reddit post needs a title, and this one has none.")
        if len(heading) > TITLE_LIMIT:
            raise RedditPostRefused(
                f"Reddit titles can be up to {TITLE_LIMIT} characters; this one has {len(heading)}. Shorten it."
            )
        if len(body) > BODY_LIMIT:
            raise RedditPostRefused(f"Reddit post text can be up to {BODY_LIMIT:,} characters. Shorten it.")
        link = (link or "").strip()
        if link and not _valid_url(link):
            raise RedditPostRefused("The link in this post isn't a web address Reddit can use.")
        image_link = (image_url or "").strip()
        if image_link and not (image_link.startswith("https://") and _valid_url(image_link)):
            image_link = ""

        rules = self.read_rules(sub)
        notes: list[str] = []
        if image_link and rules.allow_images is False:
            image_link = ""
            notes.append(f"r/{sub} does not accept images, so the post went out without its image.")
        elif image_bytes and not image_link:
            notes.append("Reddit takes images only as a web address, so the post went out without its image.")

        target = image_link or link
        if target and rules.submission_type == "self":
            kind, url = "self", ""
            if link and link not in body:
                body = f"{body}\n\n{link}".strip()
            notes.append(f"r/{sub} accepts text posts only, so the link is in the post text.")
        elif target:
            kind, url = "link", target
            if image_link and link:
                notes.append("Reddit link posts carry one address, so the post links to the image.")
        else:
            kind, url = "self", ""

        problem = check_post(rules, subreddit=sub, title=heading, kind=kind, url=url, body=body)
        if problem:
            raise RedditPostRefused(problem)

        form: dict[str, Any] = {"sr": sub, "kind": kind, "title": heading, "api_type": "json"}
        if kind == "link":
            form["url"] = url
        else:
            form["text"] = body
        data = self.request("POST", "/api/submit", form=form) or {}
        result = data.get("json") if isinstance(data, dict) and isinstance(data.get("json"), dict) else {}
        self._raise_submit_errors(result.get("errors"), sub)

        info = result.get("data") if isinstance(result.get("data"), dict) else {}
        name = str(info.get("name") or "")
        if not FULLNAME_RE.fullmatch(name):
            post_id = str(info.get("id") or "")
            name = f"t3_{post_id}" if _POST_ID_RE.fullmatch(post_id) else ""
        if not name:
            raise ConnectorError("Reddit accepted the post but did not return its id.")
        self._record_post(sub)
        self.last_post_note = " ".join(notes)
        permalink = str(info.get("url") or "")
        host = _host(permalink)
        if not (_valid_url(permalink) and (host == "reddit.com" or host.endswith(".reddit.com"))):
            permalink = f"https://www.reddit.com/r/{sub}/comments/{name[3:]}/"
        log.info("reddit: posted %s (%s)", name, kind)
        return name, permalink

    def _raise_submit_errors(self, errors: Any, subreddit: str) -> None:  # noqa: ANN401
        """Reddit reports refusals as ``[[code, message, field], …]`` with a 200."""
        if not isinstance(errors, list) or not errors:
            return
        first = errors[0] if isinstance(errors[0], list) else []
        code = str(first[0]).upper()[:60] if first else ""
        message = str(first[1]) if len(first) > 1 else ""
        log.warning("reddit: submit refused with %s", code if re.fullmatch(r"[A-Z_]{1,60}", code) else "?")
        if code == "RATELIMIT":
            match = _RATELIMIT_WAIT_RE.search(message)
            wait = 600.0
            if match:
                unit = match.group(2).lower()
                wait = float(match.group(1)) * (3600 if unit == "hour" else 60 if unit == "minute" else 1)
            raise RedditRateLimited(_defer_message(wait), retry_after=wait)
        where = f"r/{subreddit}"
        if code.startswith("SUBMIT_VALIDATION") and code not in _SUBMIT_ERRORS:
            raise RedditPostRefused(f"The post doesn't meet {where}'s posting rules.")
        template = _SUBMIT_ERRORS.get(code)
        if template:
            raise RedditPostRefused(template.format(where=where))
        raise ConnectorError(f"Reddit did not accept the post to {where}. Try again later.")

    # ── Metrics ────────────────────────────────────────────────────────────
    def read_post_metrics(self, remote_ids: list[str]) -> dict[str, dict[str, int | float | None]]:
        """Per post id given: score, upvote ratio and comment count.

        Reddit reports nothing else, so every other figure in
        ``POST_METRIC_FIELDS`` is ``None`` (not reported, which is not 0).
        Ids may be fullnames (``t3_abc``) or bare ids; others are skipped.
        """
        wanted: dict[str, str] = {}
        for raw in (remote_ids or [])[:_MAX_METRIC_IDS]:
            value = str(raw or "").strip().lower()
            fullname = value if FULLNAME_RE.fullmatch(value) else (f"t3_{value}" if _POST_ID_RE.fullmatch(value) else "")
            if fullname and fullname not in wanted:
                wanted[fullname] = str(raw)
        out: dict[str, dict[str, int | float | None]] = {}
        names = list(wanted)
        for start in range(0, len(names), _INFO_BATCH):
            batch = names[start : start + _INFO_BATCH]
            data = self.request("GET", "/api/info", params={"id": ",".join(batch), "raw_json": 1}) or {}
            listing = data.get("data") if isinstance(data, dict) and isinstance(data.get("data"), dict) else {}
            for child in listing.get("children") or []:
                post = child.get("data") if isinstance(child, dict) and child.get("kind") == "t3" else None
                if not isinstance(post, dict):
                    continue
                name = str(post.get("name") or "")
                if name not in wanted:
                    continue
                ratio = post.get("upvote_ratio")
                score = post.get("score")
                figures: dict[str, int | float | None] = {key: None for key in POST_METRIC_FIELDS}
                figures["comments"] = _int_or_none(post.get("num_comments"))
                figures["score"] = int(score) if isinstance(score, int) and not isinstance(score, bool) else None
                figures["upvote_ratio"] = (
                    float(ratio)
                    if isinstance(ratio, (int, float)) and not isinstance(ratio, bool) and 0 <= ratio <= 1
                    else None
                )
                out[wanted[name]] = figures
        return out

    # ── Checks ─────────────────────────────────────────────────────────────
    def _account(self) -> str:
        data = self.request("GET", "/api/v1/me") or {}
        name = str(data.get("name") or "") if isinstance(data, dict) else ""
        if not name:
            raise ConnectorError("Reddit did not return the account.")
        return name[:100]

    def verify_steps(self) -> list:
        """Read the account and the chosen subreddit's rules. Nothing is posted."""

        def account() -> tuple[str, dict]:
            name = self._account()
            return f"Read the Reddit account u/{name}.", {"account": f"u/{name}"}

        def subreddit() -> tuple[str, dict]:
            chosen = (self.credentials.get("subreddit") or "").strip()
            if not chosen:
                data = self.request("GET", "/subreddits/mine/subscriber", params={"limit": 5}) or {}
                listing = data.get("data") if isinstance(data, dict) and isinstance(data.get("data"), dict) else {}
                count = len(listing.get("children") or [])
                return (
                    f"No subreddit is chosen yet; the account has joined at least {vh.plural(count, 'subreddit')}.",
                    {"count": count},
                )
            sub = normalise_subreddit(chosen)
            rules = self.read_rules(sub)
            if not rules.exists:
                raise ConnectorError(f"Reddit did not return r/{sub}.")
            return (
                f"Read r/{sub} and its posting rules.",
                {"subreddit": sub, "submission_type": rules.submission_type, "flair_required": rules.flair_required},
            )

        return [
            vh.read_step("account", "Read the Reddit account", account),
            vh.read_step("subreddit", "Read the chosen subreddit and its posting rules", subreddit),
        ]

    def diagnose(self, error: Exception) -> str | None:
        status = getattr(error, "http_status", None)
        if status == 401:
            return _SIGN_IN_AGAIN
        if status == 403:
            return (
                "Reddit refused this request. The account may not be allowed to post in that "
                "subreddit, or Reddit's approval for this platform may have changed."
            )
        return None

    def check_health(self) -> HealthReport:
        try:
            name = self._account()
        except ConnectorError as exc:
            return HealthReport(ok=False, detail=self.diagnose(exc) or str(exc), checked_at=utcnow())
        return HealthReport(ok=True, detail=f"Connected to u/{name}", checked_at=utcnow())


CONNECTOR_CLASS = RedditConnector
