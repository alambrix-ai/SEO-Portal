"""Source guards: properties of the console and the API routes that a
reviewer would otherwise have to re-check by hand on every change.

Each guard reads source files and fails with the file and line that broke
it. They are deliberately narrow, so a failure is a real regression rather
than noise:

* the console never writes raw HTML (no ``innerHTML`` or
  ``dangerouslySetInnerHTML``), so model output and stored copy are always
  escaped by React;
* the old product name is gone, and the screen is called "Integrations";
* production builds publish no sourcemaps;
* every link that opens a new tab cuts the ``window.opener`` link;
* assistant markdown cannot render links or images;
* nginx sends a strict Content-Security-Policy;
* API routes never hand an exception's own text to the client;
* the Render test blueprint sends the console security headers.

The frontend guards skip when the frontend sources are not present (the
backend image is built without them).
"""
from __future__ import annotations

import ast
import re
from collections.abc import Iterator
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
BACKEND = REPO / "backend"
FRONTEND = REPO / "frontend"
SRC = FRONTEND / "src"

needs_frontend = pytest.mark.skipif(not SRC.is_dir(), reason="frontend sources not present")

CODE_SUFFIXES = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")


def _code_files() -> Iterator[Path]:
    for path in sorted(SRC.rglob("*")):
        if path.is_file() and path.suffix in CODE_SUFFIXES:
            yield path


def _rel(path: Path) -> str:
    return path.relative_to(REPO).as_posix()


def _line_of(text: str, index: int) -> int:
    return text.count("\n", 0, index) + 1


def _is_comment(line: str) -> bool:
    stripped = line.lstrip()
    return stripped.startswith(("//", "/*", "*"))


def _jsx_tags(text: str, name: str) -> Iterator[tuple[int, str]]:
    """Yield (offset, source) of every ``<name ...>`` opening tag in ``text``.

    The tag ends at the first ``>`` outside quotes and ``{...}``, so arrow
    functions and generics inside attribute expressions do not cut it short.
    """
    for match in re.finditer(rf"<{re.escape(name)}(?=[\s/>])", text):
        depth = 0
        quote = ""
        i = match.end()
        while i < len(text):
            ch = text[i]
            if quote:
                if ch == "\\":
                    i += 2
                    continue
                if ch == quote:
                    quote = ""
            elif ch in "\"'`":
                quote = ch
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
            elif ch == ">" and depth == 0:
                break
            i += 1
        yield match.start(), text[match.start() : i + 1]


# ── Raw HTML ────────────────────────────────────────────────────────────────

RAW_HTML = re.compile(r"dangerouslySetInnerHTML|\.innerHTML\b|\.outerHTML\b|insertAdjacentHTML")


@needs_frontend
def test_console_never_writes_raw_html():
    offenders = []
    for path in _code_files():
        text = path.read_text(encoding="utf-8")
        for match in RAW_HTML.finditer(text):
            offenders.append(f"{_rel(path)}:{_line_of(text, match.start())}: {match.group()}")
    assert not offenders, "Raw HTML sinks in the console:\n" + "\n".join(offenders)


def test_raw_html_guard_catches_both_forms():
    """Abuse case: the pattern must see the JSX prop and the DOM property."""
    assert RAW_HTML.search("<div dangerouslySetInnerHTML={{ __html: reply }} />")
    assert RAW_HTML.search("node.innerHTML = reply")
    assert not RAW_HTML.search("node.textContent = reply")


# ── Product name and vocabulary ─────────────────────────────────────────────


@needs_frontend
def test_old_product_name_is_gone_from_the_console():
    """Every file under src (images included, as bytes) and index.html."""
    paths = [path for path in SRC.rglob("*") if path.is_file()]
    paths.append(FRONTEND / "index.html")
    offenders = [_rel(path) for path in sorted(paths) if b"alambrix" in path.read_bytes().lower()]
    assert not offenders, "The old product name is still in: " + ", ".join(offenders)


#: Platform-operator screens, where "connector" is the operator's own word
#: for a catalogue entry. Customers never see these pages.
OPERATOR_SCREENS = {
    "frontend/src/pages/PortalAdminPage.tsx",
    "frontend/src/components/PortalVerification.tsx",
    "frontend/src/components/PortalSignInProviders.tsx",
}

# "Connectors" as a word inside a string literal or as JSX text. Identifiers
# such as ConnectorsPage or announceConnectorsChanged are not words, so
# \b keeps them out.
CONNECTORS_IN_STRING = re.compile(r"""(['"`])[^'"`\n]*\bConnectors\b[^'"`\n]*\1""")
CONNECTORS_IN_JSX_TEXT = re.compile(r">[^<>{}\n]*\bConnectors\b[^<>{}\n]*<")


def _connectors_labels(path: Path) -> list[str]:
    hits = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if _is_comment(line) or line.lstrip().startswith("import "):
            continue
        if CONNECTORS_IN_STRING.search(line) or CONNECTORS_IN_JSX_TEXT.search(line):
            hits.append(f"{_rel(path)}:{number}: {line.strip()}")
    return hits


@needs_frontend
def test_customers_see_integrations_not_connectors():
    offenders = []
    for path in _code_files():
        if _rel(path) in OPERATOR_SCREENS:
            continue
        offenders.extend(_connectors_labels(path))
    assert not offenders, 'Say "Integrations", not "Connectors":\n' + "\n".join(offenders)


def test_connectors_label_guard_ignores_identifiers():
    """The pattern flags labels and leaves code names alone."""
    assert CONNECTORS_IN_JSX_TEXT.search('<div className="x">Connectors</div>')
    assert CONNECTORS_IN_STRING.search("{ value: 'connector', label: 'Connectors' }")
    assert not CONNECTORS_IN_STRING.search("import { ConnectorsPage } from '@/pages/ConnectorsPage'")
    assert not CONNECTORS_IN_JSX_TEXT.search("<ConnectorsPage />")
    assert not CONNECTORS_IN_STRING.search("announceConnectorsChanged()")


@needs_frontend
def test_no_internal_storage_wording():
    offenders = []
    for path in _code_files():
        text = path.read_text(encoding="utf-8")
        for match in re.finditer(r"payload stored", text, re.IGNORECASE):
            offenders.append(f"{_rel(path)}:{_line_of(text, match.start())}")
    assert not offenders, "Internal wording shown to users:\n" + "\n".join(offenders)


# ── Build output ────────────────────────────────────────────────────────────

# true, "inline" and "hidden" all write source maps into dist, which would
# publish the whole source tree from the static host.
SOURCEMAP_ON = re.compile(r"""sourcemap\s*:\s*(true|['"](inline|hidden)['"])""")


@needs_frontend
def test_production_build_has_no_sourcemaps():
    text = (FRONTEND / "vite.config.ts").read_text(encoding="utf-8")
    assert not SOURCEMAP_ON.search(text)
    assert re.search(r"sourcemap\s*:\s*false", text), "vite.config.ts should say sourcemap: false"


def test_sourcemap_guard_catches_hidden_maps():
    assert SOURCEMAP_ON.search("build: { sourcemap: true }")
    assert SOURCEMAP_ON.search("build: { sourcemap: 'hidden' }")
    assert not SOURCEMAP_ON.search("build: { sourcemap: false }")


# ── New-tab links ───────────────────────────────────────────────────────────

BLANK_TARGET = re.compile(r"""target\s*=\s*(\{\s*)?['"]_blank['"]""")
REL_ATTR = re.compile(r"""\brel\s*=\s*(\{\s*)?(['"])([^'"]*)\2""")
WINDOW_OPEN = re.compile(r"window\.open\(((?:[^()]|\([^()]*\))*)\)")


def _blank_anchor_problems(text: str) -> list[int]:
    """Lines of ``<a target="_blank">`` tags whose rel lacks noopener."""
    bad = []
    for offset, tag in _jsx_tags(text, "a"):
        if not BLANK_TARGET.search(tag):
            continue
        rel = REL_ATTR.search(tag)
        if rel is None or "noopener" not in rel.group(3).split():
            bad.append(_line_of(text, offset))
    return bad


def _window_open_problems(text: str) -> list[int]:
    return [
        _line_of(text, match.start())
        for match in WINDOW_OPEN.finditer(text)
        if "_blank" in match.group(1) and "noopener" not in match.group(1)
    ]


@needs_frontend
def test_every_new_tab_link_sets_noopener():
    offenders = []
    checked = 0
    for path in _code_files():
        text = path.read_text(encoding="utf-8")
        checked += len(BLANK_TARGET.findall(text))
        for line in _blank_anchor_problems(text) + _window_open_problems(text):
            offenders.append(f"{_rel(path)}:{line}")
    assert checked, "expected new-tab links in the console; has the guard lost its target?"
    assert not offenders, 'New-tab links without rel="noopener":\n' + "\n".join(offenders)


def test_new_tab_guard_catches_missing_rel():
    """Abuse cases: no rel, rel without noopener, multi-line tags, window.open."""
    assert _blank_anchor_problems('<a href={url} target="_blank">x</a>') == [1]
    assert _blank_anchor_problems('<a href={url} target="_blank" rel="noreferrer">x</a>') == [1]
    assert _blank_anchor_problems(
        '<a\n  onClick={() => go(a > b)}\n  target="_blank"\n>x</a>'
    ) == [1]
    assert _blank_anchor_problems('<a href={u} target="_blank" rel="noreferrer noopener">x</a>') == []
    assert _window_open_problems("window.open(url, '_blank')") == [1]
    assert _window_open_problems("window.open(url, '_blank', 'noopener')") == []


# ── Assistant markdown ──────────────────────────────────────────────────────

ASSISTANT_DOCK = SRC / "components" / "AssistantDock.tsx"


@needs_frontend
def test_every_markdown_renderer_drops_urls_and_html():
    found = 0
    offenders = []
    for path in _code_files():
        text = path.read_text(encoding="utf-8")
        for offset, tag in _jsx_tags(text, "ReactMarkdown"):
            found += 1
            if "urlTransform=" not in tag or not re.search(r"\bskipHtml\b", tag):
                offenders.append(f"{_rel(path)}:{_line_of(text, offset)}")
    assert found, "expected a ReactMarkdown renderer (AssistantDock)"
    assert not offenders, "ReactMarkdown without urlTransform and skipHtml:\n" + "\n".join(offenders)


@needs_frontend
def test_assistant_markdown_allows_no_links_or_images():
    text = ASSISTANT_DOCK.read_text(encoding="utf-8")
    match = re.search(r"ASSISTANT_MD_ELEMENTS[^=]*=\s*\[(.*?)\]", text, re.DOTALL)
    assert match, "ASSISTANT_MD_ELEMENTS not found in AssistantDock.tsx"
    elements = set(re.findall(r"""['"]([A-Za-z0-9]+)['"]""", match.group(1)))
    assert elements, "ASSISTANT_MD_ELEMENTS is empty or unreadable"
    assert not elements & {"a", "img", "iframe", "script", "style"}
    assert re.search(r"allowedElements=\{ASSISTANT_MD_ELEMENTS\}", text)


# ── Console CSP ─────────────────────────────────────────────────────────────


@needs_frontend
def test_nginx_sends_a_strict_csp():
    conf = (FRONTEND / "nginx.conf").read_text(encoding="utf-8")
    entrypoint = (FRONTEND / "docker-entrypoint.sh").read_text(encoding="utf-8")
    policy = re.search(r'map \$request_uri \$console_csp \{\s*default "([^"]+)"', conf)
    assert policy, "nginx.conf should define $console_csp"
    csp = policy.group(1)
    for directive in (
        "default-src 'self'",
        "script-src 'self'",
        "object-src 'none'",
        "base-uri 'none'",
        "frame-ancestors 'none'",
    ):
        assert directive in csp
    assert "unsafe-inline" not in csp and "unsafe-eval" not in csp
    # Every location includes the snippet that actually sends it.
    locations = re.findall(r"location [^{]+\{([^}]*)\}", conf)
    assert locations and all("security-headers.conf" in body for body in locations)
    assert "add_header ${CSP_HEADER_NAME} \\$console_csp always;" in entrypoint
    assert 'CSP_HEADER_NAME="Content-Security-Policy"' in entrypoint


# ── Exception text in API responses ─────────────────────────────────────────


def _exception_text_leaks(source: str) -> list[int]:
    """Lines inside ``except ... as name`` blocks that turn ``name`` into text.

    Catches str(name), repr(name), f"...{name}...", "%s" % name and
    name.args. ``name.message`` is allowed: the app's own errors carry a
    message written for users.
    """
    leaks: list[int] = []
    for handler in ast.walk(ast.parse(source)):
        if not isinstance(handler, ast.ExceptHandler) or not handler.name:
            continue
        name = handler.name

        def is_exc(node: ast.AST, name: str = name) -> bool:
            return isinstance(node, ast.Name) and node.id == name

        for stmt in handler.body:
            for node in ast.walk(stmt):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id in {"str", "repr", "format"}
                    and any(is_exc(arg) for arg in node.args)
                ):
                    leaks.append(node.lineno)
                elif isinstance(node, ast.FormattedValue) and is_exc(node.value):
                    leaks.append(node.lineno)
                elif (
                    isinstance(node, ast.BinOp)
                    and isinstance(node.op, ast.Mod)
                    and isinstance(node.left, ast.Constant)
                    and isinstance(node.left.value, str)
                    and any(is_exc(n) for n in ast.walk(node.right))
                ):
                    leaks.append(node.lineno)
                elif isinstance(node, ast.Attribute) and is_exc(node.value) and node.attr == "args":
                    leaks.append(node.lineno)
    return sorted(set(leaks))


def test_api_routes_never_return_exception_text():
    offenders = []
    for path in sorted((BACKEND / "app" / "api").rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        for line in _exception_text_leaks(source):
            offenders.append(f"{_rel(path)}:{line}")
        for match in re.finditer(r"detail\s*=\s*(str|repr)\(", source):
            offenders.append(f"{_rel(path)}:{_line_of(source, match.start())}: detail=str(...)")
    assert not offenders, "Exception text reaches API responses:\n" + "\n".join(offenders)


def test_exception_text_guard_catches_the_usual_forms():
    source = (
        "try:\n    x()\nexcept ValueError as exc:\n    raise HTTPException(400, detail=str(exc))\n"
        "try:\n    x()\nexcept OSError as err:\n    return {'error': f'failed: {err}'}\n"
        "try:\n    x()\nexcept KeyError as e:\n    msg = 'bad %s' % (e,)\n"
        "try:\n    x()\nexcept TypeError as e2:\n    return e2.args[0]\n"
    )
    assert _exception_text_leaks(source) == [4, 8, 12, 16]
    safe = (
        "try:\n    x()\nexcept AppError as exc:\n    errors[slug] = exc.message\n"
        "try:\n    x()\nexcept ValueError as exc:\n    log_failure(log, 'x', exc)\n    raise Bad('Plain text') from exc\n"
    )
    assert _exception_text_leaks(safe) == []


# ── Render test blueprint ───────────────────────────────────────────────────

RENDER_YAML = REPO / "render.yaml"


@pytest.mark.skipif(not RENDER_YAML.is_file(), reason="render.yaml not present")
def test_render_blueprint_is_test_only_and_sends_console_headers():
    yaml = pytest.importorskip("yaml")
    text = RENDER_YAML.read_text(encoding="utf-8")
    assert "TEST ONLY" in text
    blueprint = yaml.safe_load(text)
    services = {service["name"]: service for service in blueprint["services"]}

    api_env = {
        item["key"]: item.get("value") for item in services["automarket-api"]["envVars"]
    }
    assert api_env.get("CREATIVE_RENDERER") == "pillow"
    for value in api_env.values():
        assert value is None or "0.0.0.0/0" not in str(value)

    headers = {
        header["name"]: header["value"]
        for header in services["automarket-console"].get("headers", [])
        if header.get("path") == "/*"
    }
    csp = headers.get("Content-Security-Policy") or headers.get(
        "Content-Security-Policy-Report-Only"
    )
    assert csp and "script-src 'self'" in csp and "frame-ancestors 'none'" in csp
    assert "unsafe-inline" not in csp and "unsafe-eval" not in csp
    assert headers.get("X-Content-Type-Options") == "nosniff"
    assert headers.get("X-Frame-Options") == "DENY"
    assert headers.get("Referrer-Policy") == "strict-origin-when-cross-origin"
    assert "Strict-Transport-Security" in headers
    assert "Permissions-Policy" in headers
