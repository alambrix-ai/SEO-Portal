"""Recorded real HTTP exchanges ("cassettes") from live verification runs.

``manage.py verify <slug> --record`` captures every HTTP exchange a
connector's ``verify_steps()`` make, at the httpx layer (``httpx.Client.send``,
which ``BaseConnector.request`` and any connector-owned client go through),
scrubs it, and writes it under ``tests/cassettes/<slug>/``:

* ``_meta.json`` — the slug, when and against what environment it was
  recorded, the step order, and the **fake** credentials a replay builds the
  connector with.
* ``<step>.json`` — the exchanges of one step, in order.

A replay (``tests/support/cassettes.py``) builds the connector with the fake
credentials and serves the recorded responses. It is strict: a request that
is not the next one recorded fails the test, naming both.

Scrubbing
    Real values are replaced by consistent fakes, so a replay still makes
    sense (an id the vendor returned in step one and the connector sends back
    in step two is the same fake in both places):

    * every credential value: secrets -> ``test-<field>``; URLs and hosts ->
      ``https://<field>.example.test``; emails -> ``<field>@example.test``;
      all-digit ids -> a fake of the same length; anything else -> ``test-<field>``.
      Kept verbatim only: short values (under 6 characters), the field's own
      default, paths (``/sitemap.xml``), numbers (budgets), model names and
      the ``accountEnvironment`` / ``keySource`` markers;
    * request headers are dropped except Content-Type and Accept, so
      Authorization, api-key headers and cookies are never written; response
      headers are kept only for Content-Type, Location and Link (Set-Cookie
      is dropped);
    * query and form parameters that carry secrets (``key=``,
      ``access_token=``, ``token=``, ``client_secret=``, ``code=`` ...) ->
      ``REDACTED``; so are JSON values under keys that name a secret
      (``access_token``, ``password``, ``apiKey`` ...);
    * email addresses -> ``user<n>@example.test``;
    * ids the vendor sent (runs of 9 or more digits found in a response:
      account, customer, campaign and property ids) -> same-length fakes,
      wherever they appear.

    Before a file is written it is checked for every secret credential value
    once more; if one is still there, nothing is written.
"""
from __future__ import annotations

import json
import re
import threading
from collections import deque
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit

import httpx

CASSETTE_VERSION = 1
META_FILE = "_meta.json"
#: backend/tests/cassettes
DEFAULT_DIR = Path(__file__).resolve().parents[3] / "tests" / "cassettes"

REDACTED = "REDACTED"

#: Credential keys that are markers, not account data.
PASSTHROUGH_KEYS = frozenset({"accountEnvironment", "keySource"})

#: A JSON key or credential key that names a secret.
SECRET_KEY = re.compile(
    r"(token|secret|password|passwd|api[_-]?key|apikey|authorization|cookie|"
    r"session|signature|private[_-]?key|credential|code_verifier)",
    re.I,
)
#: Query / form parameters whose value is a secret.
SECRET_PARAMS = frozenset(
    {
        "key",
        "api_key",
        "apikey",
        "api-key",
        "access_token",
        "refresh_token",
        "id_token",
        "token",
        "client_secret",
        "code",
        "code_verifier",
        "signature",
        "sig",
        "password",
        "secret",
        "auth",
        "hapikey",
        "oauth_token",
        "oauth_signature",
        "x-amz-signature",
        "x-amz-credential",
        "x-amz-security-token",
    }
)
KEPT_REQUEST_HEADERS = frozenset({"content-type", "accept"})
KEPT_RESPONSE_HEADERS = frozenset({"content-type", "location", "link"})

EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
LONG_ID = re.compile(r"(?<!\d)\d{9,}(?!\d)")
HOST = re.compile(r"^[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+$")
#: Dates and times, masked when comparing a replayed request with the
#: recorded one: a "last 30 days" report asks for different dates every day.
DATE = re.compile(r"\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2})?(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?)?")


class CassetteError(RuntimeError):
    """A cassette could not be written safely."""


class CassetteMismatch(AssertionError):
    """A replayed connector made a request the cassette does not have."""


# ── Scrubbing ──────────────────────────────────────────────────────────────
def _slugish(key: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", key.lower()).strip("-") or "value"


@dataclass(slots=True)
class Scrubber:
    """Replaces real values with consistent fakes across one recording."""

    #: original -> fake, longest first when applied.
    replacements: dict[str, str] = field(default_factory=dict)
    #: The credentials a replay builds the connector with.
    fake_credentials: dict[str, str] = field(default_factory=dict)
    #: Secret credential values: checked for once more before writing.
    secrets: set[str] = field(default_factory=set)
    #: Every fake handed out, so it is never scrubbed a second time.
    fakes: set[str] = field(default_factory=set)
    vendor_ids: set[str] = field(default_factory=set)
    _ids: dict[str, str] = field(default_factory=dict)
    _emails: dict[str, str] = field(default_factory=dict)
    _counter: int = 0

    # Construction
    @classmethod
    def for_credentials(
        cls,
        values: dict[str, Any],
        *,
        secret_keys: frozenset[str] | set[str] = frozenset(),
        keep_keys: frozenset[str] | set[str] = frozenset(),
        defaults: dict[str, str] | None = None,
    ) -> Scrubber:
        """``secret_keys``: fields declared secret. ``keep_keys``: fields
        whose values are not account data (numbers, model names)."""
        scrubber = cls()
        defaults = defaults or {}
        for key, raw in values.items():
            if not isinstance(raw, str) or not raw:
                continue
            value = raw
            secret = key in secret_keys or bool(SECRET_KEY.search(key))
            if secret:
                scrubber.secrets.add(value)
                fake = f"test-{key}"
            elif (
                key in PASSTHROUGH_KEYS
                or key in keep_keys
                or len(value.strip()) < 6
                or value == defaults.get(key)
                or value.strip().startswith("/")
            ):
                scrubber.fake_credentials[key] = value
                continue
            else:
                fake = scrubber._fake_for(key, value)
            scrubber.fake_credentials[key] = fake
            scrubber._add(value, fake)
        return scrubber

    def _next(self) -> int:
        self._counter += 1
        return self._counter

    def _same_length_digits(self, original: str) -> str:
        n = self._next()
        width = len(original)
        fake = "9" + str(n).rjust(width - 1, "0") if width > 1 else str(n % 10)
        return fake[-width:] if len(fake) > width else fake

    def _fake_for(self, key: str, value: str) -> str:
        stripped = value.strip()
        name = _slugish(key)
        if stripped.lower().startswith(("http://", "https://")):
            parts = urlsplit(stripped)
            host = f"{name}.example.test"
            if parts.hostname:
                self._add(parts.hostname, host)
            return urlunsplit(("https", host, parts.path, "", ""))
        if EMAIL.fullmatch(stripped):
            return f"{name}@example.test"
        if stripped.isdigit():
            return self._same_length_digits(stripped)
        if HOST.match(stripped) and "." in stripped:
            return f"{name}.example.test"
        return f"test-{key}"

    def _add(self, original: str, fake: str) -> None:
        if original and original != fake:
            self.replacements[original] = fake
            stripped = original.rstrip("/")
            if stripped != original and len(stripped) >= 6:
                self.replacements[stripped] = fake.rstrip("/")
        self.fakes.add(fake)

    # Ids the vendor sent
    def collect_ids(self, text: str) -> None:
        for match in LONG_ID.findall(text or ""):
            self.vendor_ids.add(match)

    def _id(self, original: str) -> str:
        if original in self.fakes:
            return original
        if original not in self._ids:
            fake = self._same_length_digits(original)
            self._ids[original] = fake
            self.fakes.add(fake)
        return self._ids[original]

    def _email(self, original: str) -> str:
        if original in self.fakes or original.endswith("@example.test"):
            return original
        if original not in self._emails:
            fake = f"user{len(self._emails) + 1}@example.test"
            self._emails[original] = fake
            self.fakes.add(fake)
        return self._emails[original]

    # Applying
    def text(self, value: str) -> str:
        """One string with every rule applied (not URL-aware)."""
        if not value:
            return value
        for original in sorted(self.replacements, key=len, reverse=True):
            if original in value:
                value = value.replace(original, self.replacements[original])
        value = EMAIL.sub(lambda m: self._email(m.group(0)), value)
        if self.vendor_ids:
            value = LONG_ID.sub(
                lambda m: self._id(m.group(0)) if m.group(0) in self.vendor_ids else m.group(0),
                value,
            )
        return value

    def params(self, pairs: list[tuple[str, str]]) -> list[tuple[str, str]]:
        out: list[tuple[str, str]] = []
        for key, value in pairs:
            scrubbed = self.text(value)
            if key.lower() in SECRET_PARAMS and scrubbed not in self.fakes and scrubbed:
                scrubbed = REDACTED
            out.append((key, scrubbed))
        return out

    def url(self, url: str) -> str:
        parts = urlsplit(url)
        query = parts.query
        if query:
            query = urlencode(self.params(parse_qsl(query, keep_blank_values=True)), safe="{}:/@")
        fragment = self.text(parts.fragment)
        if fragment and "=" in fragment:
            fragment = urlencode(self.params(parse_qsl(fragment, keep_blank_values=True)))
        return urlunsplit(
            (parts.scheme, self.text(parts.netloc), self.text(unquote(parts.path)), query, fragment)
        )

    def json(self, value: Any, *, key: str = "") -> Any:  # noqa: ANN401
        if isinstance(value, dict):
            return {self.text(str(k)): self.json(v, key=str(k)) for k, v in value.items()}
        if isinstance(value, list):
            return [self.json(v, key=key) for v in value]
        if isinstance(value, str):
            scrubbed = self.text(value)
            if key and SECRET_KEY.search(key) and scrubbed and scrubbed not in self.fakes:
                return REDACTED
            return scrubbed
        return value

    def body(self, raw: bytes, content_type: str) -> dict[str, Any] | None:
        """A request or response body, scrubbed, as ``{json|text|base64: ...}``."""
        if not raw:
            return None
        ctype = (content_type or "").lower()
        try:
            decoded = raw.decode("utf-8")
        except UnicodeDecodeError:
            # Binary (an image upload). Nothing in it can be scrubbed, so it
            # is not kept: only its size.
            return {"binary_bytes": len(raw)}
        if "json" in ctype or decoded.lstrip().startswith(("{", "[")):
            try:
                return {"json": self.json(json.loads(decoded))}
            except ValueError:
                pass
        if "x-www-form-urlencoded" in ctype:
            return {"form": self.params(parse_qsl(decoded, keep_blank_values=True))}
        return {"text": self.text(decoded)}

    def headers(self, headers: httpx.Headers, kept: frozenset[str]) -> dict[str, str]:
        out: dict[str, str] = {}
        for name, value in headers.items():
            if name.lower() in kept:
                out[name.lower()] = self.url(value) if name.lower() == "location" else self.text(value)
        return out


def body_of(request_or_response: httpx.Request | httpx.Response) -> bytes:
    try:
        return request_or_response.read() or b""
    except Exception:  # noqa: BLE001 - a streamed body that cannot be re-read
        return b""


def scrub_exchanges(
    exchanges: dict[str, list[tuple[httpx.Request, httpx.Response]]], scrubber: Scrubber
) -> dict[str, list[dict[str, Any]]]:
    """Every step's exchanges, scrubbed with one consistent mapping."""
    for pairs in exchanges.values():
        for _, response in pairs:
            scrubber.collect_ids(body_of(response).decode("utf-8", "replace"))
            for value in response.headers.values():
                scrubber.collect_ids(value)
    out: dict[str, list[dict[str, Any]]] = {}
    for step, pairs in exchanges.items():
        items: list[dict[str, Any]] = []
        for request, response in pairs:
            items.append(
                {
                    "request": {
                        "method": request.method,
                        "url": scrubber.url(str(request.url)),
                        "headers": scrubber.headers(request.headers, KEPT_REQUEST_HEADERS),
                        "body": scrubber.body(
                            body_of(request), request.headers.get("content-type", "")
                        ),
                    },
                    "response": {
                        "status": response.status_code,
                        "headers": scrubber.headers(response.headers, KEPT_RESPONSE_HEADERS),
                        "body": scrubber.body(
                            body_of(response), response.headers.get("content-type", "")
                        ),
                    },
                }
            )
        out[step] = items
    return out


def assert_no_secrets(document: str, scrubber: Scrubber) -> None:
    for secret in scrubber.secrets:
        if len(secret) >= 4 and secret in document:
            raise CassetteError(
                "A credential value was still present after scrubbing; nothing was written."
            )


# ── Recording ──────────────────────────────────────────────────────────────
class Recorder:
    """Captures the HTTP exchanges of one verification run, step by step.

    Patches ``httpx.Client.send`` for the duration of :meth:`active`, and only
    records calls made on the thread that started it, so a recording on a
    server never captures another workspace's traffic.
    """

    def __init__(self) -> None:
        self.exchanges: dict[str, list[tuple[httpx.Request, httpx.Response]]] = {}
        self._current: str | None = None
        self._thread: int | None = None

    @contextmanager
    def active(self) -> Iterator[Recorder]:
        original = httpx.Client.send
        recorder = self

        def send(client: httpx.Client, request: httpx.Request, *args: Any, **kwargs: Any) -> httpx.Response:  # noqa: ANN401
            if recorder._current is not None and threading.get_ident() == recorder._thread:
                body_of(request)
            response = original(client, request, *args, **kwargs)
            if recorder._current is not None and threading.get_ident() == recorder._thread:
                body_of(response)
                recorder.exchanges.setdefault(recorder._current, []).append((request, response))
            return response

        self._thread = threading.get_ident()
        httpx.Client.send = send  # type: ignore[method-assign]
        try:
            yield self
        finally:
            httpx.Client.send = original  # type: ignore[method-assign]
            self._thread = None

    @contextmanager
    def step(self, key: str) -> Iterator[None]:
        self._current = key
        self.exchanges.setdefault(key, [])
        try:
            yield
        finally:
            self._current = None

    def write(
        self,
        *,
        slug: str,
        steps: list[dict[str, Any]],
        environment: str,
        scrubber: Scrubber,
        recorded_at: str,
        oauth_completed: bool,
        directory: Path | None = None,
    ) -> list[Path]:
        """Write ``_meta.json`` and one file per recorded step. Returns the paths."""
        folder = (directory or DEFAULT_DIR) / slug
        recorded = {s["key"] for s in steps}
        scrubbed = scrub_exchanges(
            {k: v for k, v in self.exchanges.items() if k in recorded}, scrubber
        )
        documents: dict[Path, str] = {}
        for step in steps:
            key = step["key"]
            if key not in scrubbed:
                continue
            documents[folder / f"{key}.json"] = json.dumps(
                {
                    "version": CASSETTE_VERSION,
                    "slug": slug,
                    "step": key,
                    "label": scrubber.text(step.get("label", "")),
                    "kind": step.get("kind", "read"),
                    "recorded_at": recorded_at,
                    "interactions": scrubbed[key],
                },
                indent=2,
                ensure_ascii=False,
            )
        documents[folder / META_FILE] = json.dumps(
            {
                "version": CASSETTE_VERSION,
                "slug": slug,
                "recorded_at": recorded_at,
                "environment": environment,
                "steps": [s["key"] for s in steps if s["key"] in scrubbed],
                "oauth_completed": oauth_completed,
                "credentials": scrubber.fake_credentials,
            },
            indent=2,
            ensure_ascii=False,
        )
        for text in documents.values():
            assert_no_secrets(text, scrubber)
        folder.mkdir(parents=True, exist_ok=True)
        for path, text in documents.items():
            path.write_text(text + "\n", encoding="utf-8")
        return list(documents)


# ── Replaying ──────────────────────────────────────────────────────────────
def _comparable_url(url: str) -> str:
    return DATE.sub("<date>", unquote(url))


def _comparable_body(body: dict[str, Any] | None) -> Any:  # noqa: ANN401
    if not body:
        return None
    return json.loads(DATE.sub("<date>", json.dumps(body, sort_keys=True)))


def _plain_body(raw: bytes, content_type: str) -> dict[str, Any] | None:
    """A replayed request's body in the cassette's shape, without scrubbing
    (a replay runs on the cassette's fake values already)."""
    return Scrubber().body(raw, content_type)


@dataclass(slots=True)
class Cassette:
    slug: str
    folder: Path
    meta: dict[str, Any]
    steps: dict[str, dict[str, Any]]

    @property
    def credentials(self) -> dict[str, str]:
        return dict(self.meta.get("credentials") or {})

    @property
    def step_order(self) -> list[str]:
        return [k for k in self.meta.get("steps") or [] if k in self.steps]


def load(folder: Path) -> Cassette:
    meta_path = folder / META_FILE
    if not meta_path.exists():
        raise FileNotFoundError(f"{folder} has no {META_FILE}; record it with manage.py verify --record")
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    steps = {
        path.stem: json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(folder.glob("*.json"))
        if path.name != META_FILE
    }
    return Cassette(slug=meta.get("slug") or folder.name, folder=folder, meta=meta, steps=steps)


def discover(directory: Path | None = None) -> list[Path]:
    """Every cassette folder (one with a ``_meta.json``)."""
    root = directory or DEFAULT_DIR
    if not root.is_dir():
        return []
    return sorted(p for p in root.iterdir() if p.is_dir() and (p / META_FILE).exists())


class Player:
    """Serves a cassette's responses and fails on any request it does not expect."""

    def __init__(self, cassette: Cassette) -> None:
        self.cassette = cassette
        self.errors: list[str] = []
        self._queue: deque[dict[str, Any]] | None = None
        self._step = ""
        self._served = 0

    def _fail(self, message: str) -> CassetteMismatch:
        full = f"[{self.cassette.slug}/{self._step or '-'}] {message}"
        self.errors.append(full)
        return CassetteMismatch(full)

    def _respond(self, request: httpx.Request) -> httpx.Response:
        method = request.method
        url = str(request.url)
        if self._queue is None:
            raise self._fail(f"Unexpected request outside a recorded step: {method} {url}")
        if not self._queue:
            raise self._fail(
                f"Unexpected request #{self._served + 1}: {method} {url}. The cassette has "
                "no more exchanges for this step: the connector now makes a call it did not "
                "make when this was recorded."
            )
        expected = self._queue[0]
        want = expected["request"]
        got_body = _plain_body(body_of(request), request.headers.get("content-type", ""))
        problems = []
        if want["method"] != method:
            problems.append(f"method {method} (recorded {want['method']})")
        if _comparable_url(want["url"]) != _comparable_url(url):
            problems.append(f"URL {unquote(url)} (recorded {unquote(want['url'])})")
        if _comparable_body(want.get("body")) != _comparable_body(got_body):
            problems.append(
                f"body {json.dumps(got_body)[:300]} (recorded {json.dumps(want.get('body'))[:300]})"
            )
        if problems:
            raise self._fail(
                f"Request #{self._served + 1} differs from the recording: "
                + "; ".join(problems)
                + ". Either the connector's call changed (check it against the vendor's docs) "
                "or the cassette needs recording again."
            )
        self._queue.popleft()
        self._served += 1
        recorded = expected["response"]
        body = recorded.get("body") or {}
        headers = dict(recorded.get("headers") or {})
        if "json" in body:
            content = json.dumps(body["json"]).encode("utf-8")
            headers.setdefault("content-type", "application/json")
        elif "text" in body:
            content = body["text"].encode("utf-8")
        elif "form" in body:
            content = urlencode(body["form"]).encode("utf-8")
        else:
            content = b"\0" * int(body.get("binary_bytes") or 0)
        return httpx.Response(recorded["status"], headers=headers, content=content, request=request)

    @contextmanager
    def active(self) -> Iterator[Player]:
        original = httpx.Client.send
        player = self

        def send(client: httpx.Client, request: httpx.Request, *args: Any, **kwargs: Any) -> httpx.Response:  # noqa: ANN401, ARG001
            return player._respond(request)

        httpx.Client.send = send  # type: ignore[method-assign]
        try:
            yield self
        finally:
            httpx.Client.send = original  # type: ignore[method-assign]

    @contextmanager
    def step(self, key: str) -> Iterator[None]:
        if key not in self.cassette.steps:
            raise self._fail(f"No recording for step {key!r}")
        self._step = key
        self._queue = deque(self.cassette.steps[key].get("interactions") or [])
        self._served = 0
        try:
            yield
        finally:
            left = len(self._queue or ())
            if left and not self.errors:
                self._fail(
                    f"The connector made {self._served} request(s) but {self._served + left} "
                    "were recorded: it stopped making a call it made when this was recorded."
                )
            self._queue = None
