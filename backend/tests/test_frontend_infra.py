"""The console's container start-up and its CSP violation endpoint.

* frontend/docker-entrypoint.sh is run for real under ``sh`` with stand-in
  ``nginx`` and ``envsubst`` commands and its absolute paths pointed into a
  temporary directory. It must render the config for safe values and refuse
  to start (non-zero exit, nginx never started, nothing written) for hostile
  VITE_API_BASE_URL values or an unknown CSP_REPORT_ONLY.
* The style hash in the CSP is recomputed from ContentPage.tsx's inline
  ``<style>``, so a changed preview style fails here instead of in browsers.
* The console image runs nginx as a non-root user.
* ``POST /csp-report`` (no login) accepts only small report bodies of the two
  report content types, answers with an empty body, and logs only the
  directive, the blocked host and the page path.

No database is needed.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import pathlib
import re
import shutil
import subprocess
from dataclasses import dataclass

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.v1 import csp
from app.core.config import settings

ROOT = pathlib.Path(__file__).resolve().parents[2]
FRONTEND = ROOT / "frontend"
ENTRYPOINT = FRONTEND / "docker-entrypoint.sh"
NGINX_CONF = FRONTEND / "nginx.conf"
DOCKERFILE = FRONTEND / "Dockerfile"
CONTENT_PAGE = FRONTEND / "src" / "pages" / "ContentPage.tsx"
RENDER_YAML = ROOT / "render.yaml"

needs_frontend = pytest.mark.skipif(not ENTRYPOINT.is_file(), reason="frontend sources not present")

PREFIX = settings.api_v1_prefix.rstrip("/")
REPORT_PATH = f"{PREFIX}/csp-report"


# ── Entrypoint behaviour ────────────────────────────────────────────────────
_NGINX_STUB = '#!/bin/sh\necho "nginx $*" >> "$CALLS_LOG"\n'
# Substitutes exactly the two placeholders the entrypoint asks for, and logs
# which ones it was asked for.
_ENVSUBST_STUB = (
    "#!/bin/sh\n"
    'echo "envsubst $*" >> "$CALLS_LOG"\n'
    "sed -e 's|\\${API_ORIGIN}|'\"$API_ORIGIN\"'|g' "
    "-e 's|\\${CSP_REPORT_URI}|'\"$CSP_REPORT_URI\"'|g'\n"
)


@dataclass
class _Run:
    returncode: int
    stderr: str
    calls: str
    root: pathlib.Path

    def read(self, relative: str) -> str:
        return (self.root / relative).read_text(encoding="utf-8")

    def exists(self, relative: str) -> bool:
        return (self.root / relative).exists()


def _run_entrypoint(tmp_path: pathlib.Path, env_overrides: dict[str, str]) -> _Run:
    sh = shutil.which("sh")
    if sh is None:
        pytest.skip("no POSIX `sh` on PATH (install Git Bash or run on Linux) to run the entrypoint")

    root = tmp_path / "fs"
    (root / "etc" / "nginx" / "console").mkdir(parents=True)
    (root / "etc" / "nginx" / "conf.d").mkdir(parents=True)
    (root / "html").mkdir()
    shutil.copyfile(NGINX_CONF, root / "etc" / "nginx" / "console" / "default.conf.template")

    # The script's absolute paths, moved under the temporary root.
    script = ENTRYPOINT.read_text(encoding="utf-8")
    script = script.replace("/usr/share/nginx/html", f"{root.as_posix()}/html")
    script = script.replace("/etc/nginx", f"{root.as_posix()}/etc/nginx")
    script_path = tmp_path / "entrypoint.sh"
    script_path.write_text(script, encoding="utf-8", newline="\n")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in (("nginx", _NGINX_STUB), ("envsubst", _ENVSUBST_STUB)):
        stub = bin_dir / name
        stub.write_text(body, encoding="utf-8", newline="\n")
        stub.chmod(0o755)

    log = tmp_path / "calls.log"
    log.write_text("", encoding="utf-8")
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in {"VITE_API_BASE_URL", "CSP_REPORT_ONLY", "API_ORIGIN", "CSP_REPORT_URI"}
    }
    env["CALLS_LOG"] = log.as_posix()
    env.update(env_overrides)
    command = f'PATH="$(cd "{bin_dir.as_posix()}" && pwd):$PATH"; . "{script_path.as_posix()}"'
    result = subprocess.run(
        [sh, "-c", command], env=env, capture_output=True, text=True, timeout=30, check=False
    )
    return _Run(result.returncode, result.stderr, log.read_text(encoding="utf-8"), root)


def _policy(rendered_conf: str) -> str:
    match = re.search(r'map \$request_uri \$console_csp \{\s*default "([^"]+)"', rendered_conf)
    assert match, "the rendered config should define $console_csp"
    return match.group(1)


@needs_frontend
def test_entrypoint_renders_same_origin_defaults(tmp_path: pathlib.Path) -> None:
    run = _run_entrypoint(tmp_path, {})
    assert run.returncode == 0, run.stderr
    assert "nginx -g daemon off;" in run.calls
    assert "envsubst ${API_ORIGIN} ${CSP_REPORT_URI}" in run.calls

    conf = run.read("etc/nginx/conf.d/default.conf")
    assert "${" not in conf.split("map $request_uri", 1)[1]
    policy = _policy(conf)
    assert "report-uri /api/v1/csp-report;" in policy
    assert "report-to csp-endpoint" in policy
    assert "connect-src 'self' ;" in policy
    # nginx's own variables are left alone.
    assert "$console_hsts" in conf and "$uri" in conf

    headers = run.read("etc/nginx/snippets/security-headers.conf")
    assert "add_header Content-Security-Policy-Report-Only $console_csp always;" in headers
    assert "add_header Reporting-Endpoints 'csp-endpoint=\"/api/v1/csp-report\"' always;" in headers
    assert 'VITE_API_BASE_URL: "/api/v1"' in run.read("html/config.js")


@needs_frontend
def test_entrypoint_uses_the_api_origin_and_enforces_when_told(tmp_path: pathlib.Path) -> None:
    run = _run_entrypoint(
        tmp_path,
        {"VITE_API_BASE_URL": "https://api.example.com/api/v1", "CSP_REPORT_ONLY": "false"},
    )
    assert run.returncode == 0, run.stderr
    policy = _policy(run.read("etc/nginx/conf.d/default.conf"))
    assert "connect-src 'self' https://api.example.com;" in policy
    assert "report-uri https://api.example.com/api/v1/csp-report;" in policy
    headers = run.read("etc/nginx/snippets/security-headers.conf")
    assert "add_header Content-Security-Policy $console_csp always;" in headers
    assert "Report-Only" not in headers


HOSTILE_BASE_URLS = [
    'https://evil.example";alert(1);//',
    "javascript:alert(1)",
    "data:text/html,x",
    "//evil.example/api/v1",
    "https://user@evil.example/api/v1",
    "https://api.example.com/api v1",
    "/api/v1'; script-src *",
    "/api/v1; report-uri https://evil.example",
    "https://a.example/$(id)",
    "https://a.example/`id`",
    "https://a.example/api/v1?x=<script>",
    "/api/v1\\x",
    "/api/v1\nadd_header X-Evil 1;",
]


@needs_frontend
@pytest.mark.parametrize("value", HOSTILE_BASE_URLS)
def test_entrypoint_refuses_a_hostile_api_base_url(tmp_path: pathlib.Path, value: str) -> None:
    run = _run_entrypoint(tmp_path, {"VITE_API_BASE_URL": value})
    assert run.returncode != 0
    assert "VITE_API_BASE_URL" in run.stderr
    assert "nginx" not in run.calls and "envsubst" not in run.calls
    assert not run.exists("html/config.js")
    assert not run.exists("etc/nginx/conf.d/default.conf")


@needs_frontend
@pytest.mark.parametrize("value", ["maybe", "TRUE", "1", "yes"])
def test_entrypoint_refuses_an_unknown_csp_mode(tmp_path: pathlib.Path, value: str) -> None:
    run = _run_entrypoint(tmp_path, {"CSP_REPORT_ONLY": value})
    assert run.returncode != 0
    assert "CSP_REPORT_ONLY must be true or false" in run.stderr
    assert "nginx" not in run.calls


# ── CSP style hash ──────────────────────────────────────────────────────────
@needs_frontend
def test_csp_style_hash_matches_the_content_page_preview() -> None:
    styles = re.findall(r"<style>(.*?)</style>", CONTENT_PAGE.read_text(encoding="utf-8"), re.S)
    assert styles, "ContentPage.tsx no longer has an inline preview <style>"
    policy = _policy(NGINX_CONF.read_text(encoding="utf-8"))
    for style in styles:
        # A template expression would make the hash change at runtime.
        assert "${" not in style, "the preview <style> must be a constant string"
        digest = base64.b64encode(hashlib.sha256(style.encode("utf-8")).digest()).decode("ascii")
        assert f"'sha256-{digest}'" in policy
        if RENDER_YAML.is_file():
            assert f"'sha256-{digest}'" in RENDER_YAML.read_text(encoding="utf-8")


# ── Non-root image ──────────────────────────────────────────────────────────
@needs_frontend
def test_console_image_runs_nginx_unprivileged() -> None:
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")
    production = dockerfile[dockerfile.index("AS production") :]
    assert re.search(r"^FROM nginxinc/nginx-unprivileged:1\.27-alpine AS production$", dockerfile, re.M)
    users = re.findall(r"^USER (\S+)$", production, re.M)
    assert users and users[-1] in {"101", "nginx"}, "the final image user must not be root"
    assert re.search(r"^\s*listen 10000;", NGINX_CONF.read_text(encoding="utf-8"), re.M)


# ── POST /csp-report ────────────────────────────────────────────────────────
@pytest.fixture()
def report_client() -> TestClient:
    app = FastAPI()
    app.include_router(csp.router, prefix=PREFIX)
    return TestClient(app)


def _legacy(**overrides: str) -> bytes:
    body = {
        "document-uri": "https://console.example.com/content/42?token=s3cr3t-token#frag",
        "violated-directive": "style-src-elem 'self' https://fonts.googleapis.com",
        "effective-directive": "style-src-elem",
        "blocked-uri": "https://evil.example/x.css?session=abc123",
        "script-sample": "alert('person@example.com')",
        "original-policy": "default-src 'self'",
    }
    body.update(overrides)
    return json.dumps({"csp-report": body}).encode("utf-8")


def _warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == csp.__name__ and r.levelno >= logging.WARNING]


def test_csp_report_endpoint_is_registered_without_auth() -> None:
    from app.api.v1.router import api_router
    from tests.support.routes import routes_by_path

    route = routes_by_path(api_router)["/csp-report"]
    assert getattr(route, "methods", set()) == {"POST"}
    assert not getattr(route, "dependencies", [])


def test_legacy_report_logs_only_directive_host_and_path(
    report_client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger=csp.__name__)
    response = report_client.post(
        REPORT_PATH, content=_legacy(), headers={"Content-Type": "application/csp-report"}
    )
    assert response.status_code == 204
    assert response.content == b""
    assert _warnings(caplog) == [
        "CSP violation: directive=style-src-elem blocked=evil.example path=/content/42"
    ]
    logged = caplog.text
    for secret in ("s3cr3t", "abc123", "person@example.com", "frag", "default-src"):
        assert secret not in logged


def test_reporting_api_batch_is_accepted_and_capped(
    report_client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger=csp.__name__)
    one = {
        "type": "csp-violation",
        "url": "https://console.example.com/reports",
        "body": {
            "documentURL": "https://console.example.com/reports?x=1",
            "blockedURL": "inline",
            "effectiveDirective": "script-src-elem",
            "sample": "secret-sample",
        },
    }
    other = {"type": "deprecation", "body": {"id": "x"}}
    batch = [one] * 30 + [other]
    response = report_client.post(
        REPORT_PATH,
        content=json.dumps(batch).encode("utf-8"),
        headers={"Content-Type": "application/reports+json"},
    )
    assert response.status_code == 204
    warnings = _warnings(caplog)
    assert len(warnings) == csp.MAX_REPORTS_PER_BATCH
    assert set(warnings) == {"CSP violation: directive=script-src-elem blocked=inline path=/reports"}
    assert "secret-sample" not in caplog.text


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"effective-directive": "img-src\r\nFAKE LOG LINE"}, "directive=unknown"),
        ({"blocked-uri": "https://ev%0Ail.example/x"}, "blocked=unknown"),
        # urlsplit drops raw newlines; what is left is a plain host.
        ({"blocked-uri": "https://EVIL.example\n/x"}, "blocked=evil.example"),
        ({"blocked-uri": "chrome-extension://abc/x.js"}, "blocked=abc"),
        ({"blocked-uri": "data"}, "blocked=data"),
        ({"blocked-uri": "https://[::1"}, "blocked=unknown"),
        ({"document-uri": "https://console.example.com/a b<script>"}, "path=unknown"),
    ],
)
def test_hostile_fields_are_reduced_before_logging(
    report_client: TestClient,
    caplog: pytest.LogCaptureFixture,
    overrides: dict[str, str],
    expected: str,
) -> None:
    caplog.set_level(logging.INFO, logger=csp.__name__)
    response = report_client.post(
        REPORT_PATH, content=_legacy(**overrides), headers={"Content-Type": "application/csp-report"}
    )
    assert response.status_code == 204
    (warning,) = _warnings(caplog)
    assert expected in warning
    assert "\n" not in warning and "\r" not in warning and "<script>" not in warning


@pytest.mark.parametrize(
    "content_type", ["application/json", "text/plain", "application/x-www-form-urlencoded", ""]
)
def test_other_content_types_are_refused(report_client: TestClient, content_type: str) -> None:
    response = report_client.post(REPORT_PATH, content=_legacy(), headers={"Content-Type": content_type})
    assert response.status_code == 415
    assert response.content == b""


def test_oversized_bodies_are_refused_without_reflection(
    report_client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger=csp.__name__)
    big = _legacy(**{"script-sample": "A" * (csp.MAX_REPORT_BYTES + 1)})
    declared = report_client.post(
        REPORT_PATH, content=big, headers={"Content-Type": "application/csp-report"}
    )
    assert declared.status_code == 413 and declared.content == b""

    def chunks():  # noqa: ANN202 - a streamed body with no Content-Length
        for _ in range(4):
            yield b"A" * 4096

    streamed = report_client.post(
        REPORT_PATH, content=chunks(), headers={"Content-Type": "application/csp-report"}
    )
    assert streamed.status_code == 413 and streamed.content == b""
    assert not _warnings(caplog)


@pytest.mark.parametrize(
    ("content_type", "body"),
    [
        ("application/csp-report", b"not json"),
        ("application/csp-report", b"\xff\xfe"),
        ("application/csp-report", b'{"csp-report": "x"}'),
        ("application/csp-report", b'{"csp-report": {"blocked-uri": 5}}'),
        ("application/reports+json", b'{"type": "csp-violation"}'),
    ],
)
def test_malformed_reports_are_dropped_quietly(
    report_client: TestClient, caplog: pytest.LogCaptureFixture, content_type: str, body: bytes
) -> None:
    caplog.set_level(logging.INFO, logger=csp.__name__)
    response = report_client.post(REPORT_PATH, content=body, headers={"Content-Type": content_type})
    assert response.status_code == 204 and response.content == b""
    assert not _warnings(caplog)
    assert "not json" not in caplog.text
