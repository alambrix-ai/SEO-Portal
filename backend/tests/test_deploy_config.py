"""The self-hosted deployment files say what the security model needs.

Docker is not available where this suite runs, so these tests read the files
instead: docker-compose.yml is parsed as YAML (anchors and merge keys
resolved, as compose does) and checked service by service, and the scripts
and Dockerfiles are checked for the lines that carry a control. The
entrypoint's RUN_MIGRATIONS switch is also run for real under `sh` with
stand-in `alembic` and `uvicorn` commands when a POSIX shell is available.

What is pinned here, and why:
- migrations run once, as the schema owner, in a `migrate` service; nothing
  long-running gets the owner DSN or migrates on start;
- beat runs once, in its own service, and no worker also runs beat (`-B`);
- the render worker is boxed in (no capabilities, read-only root, limits);
- uvicorn never parses X-Forwarded-For (the app does, against
  TRUSTED_PROXIES);
- the app role cannot change the audit log; the image refuses a font that
  differs from fonts.lock.
"""
from __future__ import annotations

import hashlib
import os
import pathlib
import re
import shutil
import subprocess
from typing import Any

import pytest

yaml = pytest.importorskip("yaml")

ROOT = pathlib.Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
COMPOSE = ROOT / "docker-compose.yml"
ENV_EXAMPLE = ROOT / ".env.compose.example"
ENTRYPOINT = BACKEND / "docker-entrypoint.sh"
BACKEND_DOCKERFILE = BACKEND / "Dockerfile"
FRONTEND_DOCKERFILE = ROOT / "frontend" / "Dockerfile"
BOOTSTRAP = BACKEND / "scripts" / "bootstrap.sql"
ALEMBIC_ENV = BACKEND / "alembic" / "env.py"
FONTS = BACKEND / "app" / "services" / "creative" / "assets" / "fonts"

#: Containers that run the backend image for as long as the stack is up.
LONG_RUNNING = ("api", "worker", "render-worker", "beat")
#: Every container that runs the backend image.
BACKEND_SERVICES = ("migrate", *LONG_RUNNING)


@pytest.fixture(scope="module")
def compose() -> dict[str, Any]:
    data = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


@pytest.fixture(scope="module")
def services(compose: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return compose["services"]


def _command(service: dict[str, Any]) -> list[str]:
    command = service.get("command", [])
    if isinstance(command, str):
        return command.split()
    return [str(part) for part in command]


def _healthcheck_text(service: dict[str, Any]) -> str:
    test = service["healthcheck"]["test"]
    return " ".join(test) if isinstance(test, list) else str(test)


def _without_comments(text: str, marker: str) -> str:
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith(marker))


# ── Services ────────────────────────────────────────────────────────────────
def test_the_compose_file_has_every_production_service(services: dict[str, dict[str, Any]]) -> None:
    for name in ("postgres", "redis", "migrate", "api", "worker", "render-worker", "beat", "console", "console-prod"):
        assert name in services, name
    assert services["console-prod"]["build"]["target"] == "production"
    assert services["console-prod"]["profiles"] == ["production"]
    assert services["console"]["build"]["target"] == "development"


def test_no_worker_runs_beat(services: dict[str, dict[str, Any]]) -> None:
    for name, service in services.items():
        command = _command(service)
        assert "-B" not in command, name
        assert "--beat" not in command, name
    assert re.search(r"(^|\s)-B(\s|$)", _without_comments(COMPOSE.read_text(encoding="utf-8"), "#")) is None


def test_the_general_worker_has_a_fixed_name_and_sizing_from_env(services: dict[str, dict[str, Any]]) -> None:
    command = _command(services["worker"])
    assert command[:4] == ["celery", "-A", "app.orchestration.tasks", "worker"]
    assert command[command.index("-n") + 1] == "celery@default"
    assert command[command.index("-Q") + 1] == "celery"
    assert "--concurrency=${CELERY_WORKER_CONCURRENCY:-2}" in command
    assert "--max-tasks-per-child=${CELERY_MAX_TASKS_PER_CHILD:-50}" in command


def test_the_render_worker_is_boxed_in(services: dict[str, dict[str, Any]]) -> None:
    render = services["render-worker"]
    command = _command(render)
    assert command[command.index("-Q") + 1] == "render"
    assert command[command.index("-c") + 1] == "${CELERY_RENDER_CONCURRENCY:-1}"
    assert command[command.index("-P") + 1] == "prefork"
    assert command[command.index("--max-tasks-per-child") + 1] == "50"
    assert command[command.index("--max-memory-per-child") + 1] == "1500000"
    assert command[command.index("-n") + 1] == "celery@render"
    assert render["shm_size"] == "1gb"
    assert render["init"] is True
    assert render["read_only"] is True
    assert any(mount.startswith("/tmp") for mount in render["tmpfs"])
    assert render["cap_drop"] == ["ALL"]
    assert "no-new-privileges:true" in render["security_opt"]
    assert render["pids_limit"] > 0


def test_beat_is_a_single_read_only_scheduler(services: dict[str, dict[str, Any]]) -> None:
    beat = services["beat"]
    command = _command(beat)
    assert command[:4] == ["celery", "-A", "app.orchestration.tasks", "beat"]
    assert command[command.index("--schedule") + 1] == "/tmp/celerybeat-schedule"
    assert "--pidfile=" in command
    assert beat["deploy"]["replicas"] == 1
    assert beat["read_only"] is True
    assert any(mount.startswith("/tmp") for mount in beat["tmpfs"])
    check = _healthcheck_text(beat)
    assert "beat:hb" in check and "REDIS_URL" in check and "<120" in check
    assert beat["environment"]["REDIS_URL"].startswith("redis://")


# ── Health and limits ───────────────────────────────────────────────────────
def test_every_backend_service_has_a_healthcheck(services: dict[str, dict[str, Any]]) -> None:
    assert "curl -fsS http://localhost:8000/health" in _healthcheck_text(services["api"])
    for name, node in (("worker", "celery@default"), ("render-worker", "celery@render")):
        check = services[name]["healthcheck"]
        assert _healthcheck_text(services[name]) == (
            f"CMD celery -A app.orchestration.tasks inspect ping -d {node} -t 5"
        )
        assert (check["interval"], check["timeout"], check["retries"], check["start_period"]) == (
            "60s",
            "15s",
            3,
            "30s",
        )
    # A one-shot job must not inherit the image's HTTP healthcheck.
    assert services["migrate"]["healthcheck"] == {"disable": True}
    assert "healthcheck" in services["console-prod"]


@pytest.mark.parametrize("name", [*BACKEND_SERVICES, "console-prod"])
def test_resource_limits_are_set_and_agree(services: dict[str, dict[str, Any]], name: str) -> None:
    service = services[name]
    limits = service["deploy"]["resources"]["limits"]
    assert service["mem_limit"] == limits["memory"]
    assert float(service["cpus"]) == float(limits["cpus"])
    assert service["pids_limit"] == limits["pids"]


@pytest.mark.parametrize("name", BACKEND_SERVICES)
def test_backend_containers_drop_privileges(services: dict[str, dict[str, Any]], name: str) -> None:
    assert services[name]["cap_drop"] == ["ALL"]
    assert "no-new-privileges:true" in services[name]["security_opt"]


# ── Migrations and roles ────────────────────────────────────────────────────
@pytest.mark.parametrize("name", LONG_RUNNING)
def test_long_running_services_do_not_migrate(services: dict[str, dict[str, Any]], name: str) -> None:
    env = services[name]["environment"]
    assert env["RUN_MIGRATIONS"] == "false"
    assert services[name]["depends_on"]["migrate"]["condition"] == "service_completed_successfully"


def test_the_api_and_workers_use_celery(services: dict[str, dict[str, Any]]) -> None:
    for name in LONG_RUNNING:
        assert services[name]["environment"]["CELERY_ENABLED"] == "true", name


def test_the_migrate_service_runs_as_the_owner(services: dict[str, dict[str, Any]]) -> None:
    migrate = services["migrate"]
    assert _command(migrate) == ["alembic", "upgrade", "head"]
    assert migrate["environment"]["MIGRATION_DATABASE_URL"].startswith("${MIGRATION_DATABASE_URL:?")
    assert migrate["restart"] == "no"


@pytest.mark.parametrize("name", LONG_RUNNING)
def test_the_owner_credentials_never_reach_a_long_running_container(
    services: dict[str, dict[str, Any]], name: str
) -> None:
    # They load .env, which holds the owner DSN: it must be blanked over.
    env = services[name]["environment"]
    assert env["MIGRATION_DATABASE_URL"] == ""
    assert env["POSTGRES_PASSWORD"] == ""
    assert env["DATABASE_URL"].startswith("${DATABASE_URL:?")


def test_database_and_redis_are_published_on_loopback_only(services: dict[str, dict[str, Any]]) -> None:
    for name in ("postgres", "redis"):
        for port in services[name]["ports"]:
            assert str(port).startswith("127.0.0.1:"), (name, port)


def test_alembic_prefers_the_migration_url() -> None:
    source = ALEMBIC_ENV.read_text(encoding="utf-8")
    assert "settings.migration_database_url or settings.sqlalchemy_url" in source
    assert 'set_main_option("sqlalchemy.url", migration_url()' in source
    assert "url=migration_url()" in source
    # The app's own URL is no longer handed to Alembic directly.
    assert 'set_main_option("sqlalchemy.url", settings.sqlalchemy_url)' not in source


# ── Forwarded headers ───────────────────────────────────────────────────────
def test_uvicorn_never_parses_forwarded_headers(services: dict[str, dict[str, Any]]) -> None:
    assert "--no-proxy-headers" in _command(services["api"])
    entrypoint = _without_comments(ENTRYPOINT.read_text(encoding="utf-8"), "#")
    assert "--no-proxy-headers" in entrypoint
    for text in (entrypoint, _without_comments(COMPOSE.read_text(encoding="utf-8"), "#")):
        assert "--proxy-headers" not in text.replace("--no-proxy-headers", "")


# ── Entrypoint behaviour ────────────────────────────────────────────────────
def _run_entrypoint(tmp_path: pathlib.Path, run_migrations: str | None) -> str:
    sh = shutil.which("sh")
    if sh is None:
        pytest.skip("no POSIX shell available")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "calls.log"
    for name in ("alembic", "uvicorn"):
        stub = bin_dir / name
        stub.write_text(f'#!/bin/sh\necho "{name} $*" >> "$CALLS_LOG"\n', encoding="utf-8", newline="\n")
        stub.chmod(0o755)
    env = {key: value for key, value in os.environ.items() if key != "RUN_MIGRATIONS"}
    env["CALLS_LOG"] = log.as_posix()
    env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
    if run_migrations is not None:
        env["RUN_MIGRATIONS"] = run_migrations
    script = f'PATH="$(cd "{bin_dir.as_posix()}" && pwd):$PATH"; . "{ENTRYPOINT.as_posix()}"'
    subprocess.run([sh, "-c", script], env=env, check=True, timeout=30)
    return log.read_text(encoding="utf-8")


@pytest.mark.parametrize("value", [None, "true", "1", "yes"])
def test_the_entrypoint_migrates_by_default(tmp_path: pathlib.Path, value: str | None) -> None:
    calls = _run_entrypoint(tmp_path, value)
    assert "alembic upgrade head" in calls
    assert "--no-proxy-headers" in calls


@pytest.mark.parametrize("value", ["false", "False", "0", "no", "off"])
def test_the_entrypoint_skips_migrations_when_told(tmp_path: pathlib.Path, value: str) -> None:
    calls = _run_entrypoint(tmp_path, value)
    assert "alembic" not in calls
    assert "uvicorn app.main:app" in calls


# ── Database bootstrap ──────────────────────────────────────────────────────
def test_bootstrap_revokes_audit_log_changes_after_the_grants() -> None:
    sql = BOOTSTRAP.read_text(encoding="utf-8")
    revoke = sql.find("REVOKE UPDATE, DELETE, TRUNCATE ON audit_log FROM automarket_app;")
    assert revoke != -1
    assert revoke > sql.find("GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO automarket_app;")
    assert revoke > sql.rfind("ALTER DEFAULT PRIVILEGES")
    # Guarded: on a first run the table does not exist yet.
    assert "to_regclass('public.audit_log')" in sql


# ── Images ──────────────────────────────────────────────────────────────────
def test_the_backend_image_verifies_fonts_before_dropping_root() -> None:
    dockerfile = BACKEND_DOCKERFILE.read_text(encoding="utf-8")
    check = "grep -v '^#' fonts.lock | awk '{print $1\"  \"$2}' | sha256sum -c"
    assert check in dockerfile
    assert "cd /app/app/services/creative/assets/fonts" in dockerfile
    assert dockerfile.index(check) > dockerfile.index("COPY . .")
    assert dockerfile.index(check) < dockerfile.index("USER automarket")
    assert re.search(r"^FROM \$\{PYTHON_IMAGE\}", dockerfile, re.M)


def test_the_shipped_fonts_match_fonts_lock() -> None:
    # What the build step checks, so a stale lock fails here first.
    entries = [
        line.split()
        for line in (FONTS / "fonts.lock").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    ]
    assert entries
    for digest, relative, *_ in entries:
        path = (FONTS / relative).resolve()
        assert path.is_relative_to(FONTS.resolve()), relative
        assert hashlib.sha256(path.read_bytes()).hexdigest() == digest, relative


def test_the_console_image_needs_no_writable_nginx_config() -> None:
    dockerfile = FRONTEND_DOCKERFILE.read_text(encoding="utf-8")
    assert "COPY nginx.conf /etc/nginx/console/default.conf.template" in dockerfile
    assert "COPY nginx.conf /etc/nginx/conf.d/default.conf" not in dockerfile
    for target in (
        "/etc/nginx/conf.d/default.conf",
        "/etc/nginx/snippets/security-headers.conf",
        "/usr/share/nginx/html/config.js",
    ):
        assert re.search(rf"ln -sf /tmp/\S+ {re.escape(target)}", dockerfile), target
    entrypoint = (ROOT / "frontend" / "docker-entrypoint.sh").read_text(encoding="utf-8")
    assert 'TEMPLATE="$TEMPLATE_DIR/default.conf.template"' in entrypoint
    assert "CSP_REPORT_ONLY" in entrypoint


# ── Example settings ────────────────────────────────────────────────────────
def _env_lines(path: pathlib.Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip() and not line.lstrip().startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip()
    return values


def test_the_compose_example_holds_no_real_looking_secret() -> None:
    values = _env_lines(ENV_EXAMPLE)
    for key in ("MIGRATION_DATABASE_URL", "DATABASE_URL"):
        password = re.match(r"postgresql\+psycopg://[^:]+:([^@]+)@", values[key])
        assert password is not None, key
        assert re.fullmatch(r"<[a-z-]+>", password.group(1)), key
    assert "automarket_owner" in values["MIGRATION_DATABASE_URL"]
    assert "automarket_app" in values["DATABASE_URL"]
    for key in ("POSTGRES_PASSWORD", "SECRET_KEY", "MASTER_ENCRYPTION_KEY", "BLIND_INDEX_KEY", "RESEND_API_KEY"):
        assert values[key] == "", key


def test_the_compose_example_documents_the_deployment_settings() -> None:
    text = ENV_EXAMPLE.read_text(encoding="utf-8")
    values = _env_lines(ENV_EXAMPLE)
    recommended = _env_lines(BACKEND / ".env.example")
    for key in (
        "CREATIVE_ALLOWED_TEXT_MODELS",
        "CREATIVE_DENIED_TEXT_MODELS",
        "CREATIVE_ALLOWED_IMAGE_MODELS",
        "CREATIVE_DENIED_IMAGE_MODELS",
    ):
        assert values[key] == recommended[key], key
    assert values["CELERY_RENDER_CONCURRENCY"] == "1"
    assert "SELF_HOSTED_LLM_ALLOWED_HOSTS" not in values
    assert "# SELF_HOSTED_LLM_ALLOWED_HOSTS=" in text
    assert values["TRUSTED_PROXIES"] == ""
    assert "TRUSTED_PROXIES=172.28.0.1/32" in text
    assert "0.0.0.0/0" in text  # named only to forbid it


# ── Compose hardening ──────────────────────────────────────────────────────
RENDER_YAML = ROOT / "render.yaml"


def test_redis_requires_a_password_kept_out_of_argv(services: dict[str, dict[str, Any]]) -> None:
    redis = services["redis"]
    assert redis["environment"]["REDIS_PASSWORD"].startswith("${REDIS_PASSWORD:?")
    assert redis["environment"]["REDISCLI_AUTH"].startswith("${REDIS_PASSWORD:?")
    script = _command(redis)[-1]
    assert "requirepass $$REDIS_PASSWORD" in script
    # Config arrives on stdin (trailing "-"), never as a --requirepass argument.
    assert "--requirepass" not in script
    assert re.search(r"redis-server .* -\s*<<EOF", script)
    assert "docker-entrypoint.sh redis-server" in script  # still drops to the redis user
    assert "*[!A-Za-z0-9]*" in script


@pytest.mark.parametrize("name", BACKEND_SERVICES)
def test_backend_redis_urls_carry_the_password(services: dict[str, dict[str, Any]], name: str) -> None:
    env = services[name]["environment"]
    for key in ("CELERY_BROKER_URL", "CELERY_RESULT_BACKEND", "REDIS_URL"):
        assert env[key].startswith("redis://:${REDIS_PASSWORD:?"), (name, key)
        assert env[key].endswith(("@redis:6379/0", "@redis:6379/1")), (name, key)


def test_the_render_worker_has_its_own_network(compose: dict[str, Any], services: dict[str, dict[str, Any]]) -> None:
    assert services["render-worker"]["networks"] == ["render"]
    assert "render" in compose["networks"]
    assert compose["networks"]["render"].get("internal") is not True  # needs the model APIs
    on_render = {name for name, service in services.items() if "render" in (service.get("networks") or [])}
    assert on_render == {"render-worker", "postgres", "redis"}
    for name in ("postgres", "redis"):
        assert "default" in services[name]["networks"], name


@pytest.mark.parametrize("name", BACKEND_SERVICES)
def test_backend_containers_have_a_read_only_root(services: dict[str, dict[str, Any]], name: str) -> None:
    service = services[name]
    assert service["read_only"] is True
    assert any(mount.startswith("/tmp") for mount in service["tmpfs"])
    assert service["environment"]["HOME"] == "/tmp"


def test_the_compose_stack_declares_its_render_isolation(services: dict[str, dict[str, Any]]) -> None:
    assert services["render-worker"]["environment"]["CREATIVE_RENDER_ISOLATED"] == "true"
    for name in BACKEND_SERVICES:
        if name == "render-worker":
            continue
        assert "CREATIVE_RENDER_ISOLATED" not in services[name]["environment"], name
    # migrate imports Settings and never renders.
    assert services["migrate"]["environment"]["CREATIVE_RENDERER"] == "pillow"


def test_the_dev_console_binds_loopback_only(services: dict[str, dict[str, Any]]) -> None:
    assert services["console"]["ports"] == ["127.0.0.1:5173:5173"]


def test_postgres_uses_the_image_that_has_the_tls_certificate(services: dict[str, dict[str, Any]]) -> None:
    postgres = services["postgres"]
    assert postgres["image"] == "postgres:17"
    command = _command(postgres)
    assert "ssl_cert_file=/etc/ssl/certs/ssl-cert-snakeoil.pem" in command


def test_the_backend_image_does_not_hand_app_to_the_runtime_user() -> None:
    dockerfile = _without_comments(BACKEND_DOCKERFILE.read_text(encoding="utf-8"), "#")
    assert "chown -R automarket" not in dockerfile
    assert "chown -R root:root /app" in dockerfile
    assert "go=rX" in dockerfile
    assert dockerfile.index("chown -R root:root /app") < dockerfile.index("USER automarket")


def test_bootstrap_tolerates_log_settings_it_may_not_change() -> None:
    sql = BOOTSTRAP.read_text(encoding="utf-8")
    off = sql.index(r"\set ON_ERROR_STOP off")
    on = sql.index(r"\set ON_ERROR_STOP on", off)
    for statement in ("SET log_statement = 'none';", "SET log_min_duration_statement = -1;", "SET log_min_error_statement = panic;"):
        assert off < sql.index(statement) < on, statement
    assert r"\if :log_settings_refused" in sql
    # Everything that carries a password runs with ON_ERROR_STOP back on.
    assert on < sql.index("CREATE ROLE automarket_owner")


def test_the_health_checks_hit_the_exempt_path(services: dict[str, dict[str, Any]]) -> None:
    from app.api.middleware import HEALTH_PATH

    assert f"http://localhost:8000{HEALTH_PATH} " in _healthcheck_text(services["api"]) + " "
    assert f'{HEALTH_PATH}"' in BACKEND_DOCKERFILE.read_text(encoding="utf-8")


def test_render_yaml_sets_the_edge_and_role_flags() -> None:
    blueprint = yaml.safe_load(RENDER_YAML.read_text(encoding="utf-8"))
    api = next(s for s in blueprint["services"] if s["name"] == "automarket-api")
    env = {item["key"]: item.get("value") for item in api["envVars"]}
    assert env["APP_ENV"] == "production"
    assert env["FORCE_HTTPS"] == "true"
    assert "TRUSTED_PROXIES" not in env
    assert env["EDGE_TLS_TERMINATED"] == "true"
    assert env["ALLOW_PRIVILEGED_DB_ROLE"] == "true"
    assert env["CREATIVE_RENDERER"] == "pillow"
    text = RENDER_YAML.read_text(encoding="utf-8")
    assert "TEST ENVIRONMENT ONLY" in text and "no RLS" in text


def test_the_compose_example_asks_for_a_redis_password() -> None:
    values = _env_lines(ENV_EXAMPLE)
    assert values["REDIS_PASSWORD"] == ""
    assert "EDGE_TLS_TERMINATED" in ENV_EXAMPLE.read_text(encoding="utf-8")


def test_the_console_runs_with_no_capabilities(services: dict[str, dict[str, Any]]) -> None:
    console = services["console-prod"]
    assert console["cap_drop"] == ["ALL"]
    assert "cap_add" not in console
    assert "nginx-unprivileged" in FRONTEND_DOCKERFILE.read_text(encoding="utf-8")
