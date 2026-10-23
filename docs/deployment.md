# Deploying Synaprank.ai (self-hosted)

For platform administrators and DevOps teams running Synaprank.ai inside their
own infrastructure.

- **Production** is the self-hosted Docker stack in
  [`docker-compose.yml`](../docker-compose.yml), or the same containers on
  your orchestrator.
- **Render** ([`render.yaml`](../render.yaml),
  [`RENDER_DEPLOY.md`](../RENDER_DEPLOY.md)) is a **test environment only**.
  It has no workers, no Redis, no role split and no Chromium, and the free
  instance sleeps.

Every setting below is an environment variable read by
[`backend/app/core/config.py`](../backend/app/core/config.py). With
`APP_ENV=production`, the API refuses to start on an unsafe value and lists
every problem at once.

---

## 1. Architecture

| Service | What it does | Network | Notes |
|---|---|---|---|
| `migrate` | Runs `alembic upgrade head` once as the schema owner, then exits. | DB | Everything else waits for it to exit successfully. Read-only root, tmpfs `/tmp`. |
| `api` | FastAPI on port 8000. | DB, Redis, model vendors, connected platforms | uvicorn runs with `--no-proxy-headers` (see §4). Read-only root, tmpfs `/tmp`. |
| `worker` | Celery, default queue (`celery@default`): agent runs, publishing, metrics, retention. | Same as `api` | Read-only root, tmpfs `/tmp`. |
| `render-worker` | Celery, `render` queue (`celery@render`): Creative Studio jobs with headless Chromium. | Its own `render` network: DB, Redis, AI vendors only | Read-only root, tmpfs `/tmp`, no capabilities, memory/CPU/PID limits. Cannot reach `api`, `worker`, `beat` or the console. See §7 for egress. |
| `beat` | The only scheduler. Exactly one replica. | Redis, DB | Healthy while the `beat:hb` heartbeat in Redis is under 120 s old. That heartbeat is run by a worker, so beat also reports unhealthy when no worker is up. |
| `redis` | Celery broker and results, heartbeat. | Internal (`default` and `render`) | Requires `REDIS_PASSWORD` (letters and digits, 24+). The password reaches Redis on stdin, never the command line. |
| `console-prod` | nginx serving the built console, with CSP and security headers. | Public, behind your proxy | Profile `production`. (`console` is the Vite dev server, profile `dev`, published on `127.0.0.1:5173` only.) |
| `postgres` | Bundled PostgreSQL 17 (Debian image) with TLS. | Internal (`default` and `render`) | Optional. Replace with a managed database (see §2). |

The compose network is pinned to `172.28.0.0/24` (gateway `172.28.0.1`); the
`render` network is `172.28.1.0/24`. PostgreSQL, Redis, the API and the
console publish on `127.0.0.1` only (`PUBLISH_ADDR`). Put a TLS-terminating
reverse proxy in front: route `/` to `console-prod:10000` (host port 8080) and
`/api` to `api:8000`.

The bundled PostgreSQL uses the Debian `postgres:17` image because it ships
the self-signed "snakeoil" certificate that `ssl=on` points at (the Alpine
image has none). That certificate is only good for `sslmode=require`. For
`verify-ca` / `verify-full`, mount your own certificate and key read-only
and change `ssl_cert_file` / `ssl_key_file` in `docker-compose.yml`.

The backend image keeps `/app` owned by root and read-only to the service
user, so a compromised process cannot rewrite the code it runs. In compose,
`migrate`, `api`, `worker`, `render-worker` and `beat` also run with a
read-only root filesystem; only `/tmp` (a tmpfs, `HOME=/tmp`) is writable.

Start the stack with:

```bash
docker compose --profile production up -d --build
```

---

## 2. First-time setup

### 2.1 Database roles

[`backend/scripts/bootstrap.sql`](../backend/scripts/bootstrap.sql) creates
two roles and the `automarket` database. Run it once, as a superuser, before
the first migration.

| Role | Used by | Can |
|---|---|---|
| `automarket_owner` | `migrate` only, via `MIGRATION_DATABASE_URL` | Own the schema and run migrations. |
| `automarket_app` | `api`, `worker`, `render-worker`, `beat`, via `DATABASE_URL` | Read and write rows. It owns nothing, so it cannot change the schema, bypass row-level security, or change the audit log. |

The passwords are psql variables. There are no defaults. The script stops
before changing anything if either password is missing, shorter than 16
characters, or the two are the same. Generate them, keep them in your secret
manager, and pass them from environment variables so they stay out of shell
history:

```bash
export AUTOMARKET_OWNER_PASSWORD="$(openssl rand -hex 32)"   # store in your secret manager
export AUTOMARKET_APP_PASSWORD="$(openssl rand -hex 32)"

# Bundled PostgreSQL (prompts for the POSTGRES_PASSWORD superuser password):
docker compose up -d postgres
docker compose exec postgres psql -U automarket -d postgres \
  -v owner_password="$AUTOMARKET_OWNER_PASSWORD" \
  -v app_password="$AUTOMARKET_APP_PASSWORD" \
  -f /bootstrap/bootstrap.sql

# Managed PostgreSQL (RDS, Cloud SQL, Azure, ...), as its administrator:
psql -h <db-host> -U <admin-user> -d postgres \
  -v owner_password="$AUTOMARKET_OWNER_PASSWORD" \
  -v app_password="$AUTOMARKET_APP_PASSWORD" \
  -f backend/scripts/bootstrap.sql
```

Hex passwords need no URL-encoding in the DSNs. Other passwords must be
URL-encoded (for example `@` becomes `%40`). The script turns statement
logging off for its own session, so the passwords do not reach the server
log. That needs a superuser: on managed PostgreSQL, where the administrator
is not one, the script prints a warning and carries on. If you see it,
confirm the server does not log statements (`log_statement` is not `all` or
`ddl`), or create the roles and then set their passwords with psql's
`\password`. The passwords are also briefly visible in the local process
list while psql runs (accepted, low risk), so run it from an admin host.

The last query prints both roles. `bypasses_rls` must be `false` for both.

### 2.2 Connection settings

| Setting | Default | Meaning |
|---|---|---|
| `DATABASE_URL` | (built from `POSTGRES_*`) | The `automarket_app` DSN. Every long-running service uses it. Production needs `sslmode` `require`, `verify-ca` or `verify-full` (`prefer` is refused). Without `?sslmode=` in the URL, `POSTGRES_SSLMODE` is used. |
| `MIGRATION_DATABASE_URL` | blank (falls back to `DATABASE_URL` with a warning) | The `automarket_owner` DSN. Give it **only** to the migration job. Compose blanks it for every other container. Alias: `DATABASE_ADMIN_URL`. In production it must carry its own `?sslmode=require` (or `verify-ca` / `verify-full`). |
| `ALLOW_PRIVILEGED_DB_ROLE` | `false` | At startup the API **and** each Celery worker child check the role in `DATABASE_URL` and refuse to start if it is a superuser, has `BYPASSRLS`, or owns (directly or through role membership) any RLS-protected table. Fail closed in every `APP_ENV`; set `true` only for a throwaway local/test environment with no separate app role (loud warning). Never for real customer data. |
| `RUN_MIGRATIONS` | `true` | Whether the image's entrypoint runs `alembic upgrade head` on start. Compose sets `false` for `api`, workers and `beat`, because `migrate` owns migrations. Set `false` anywhere a separate release job migrates. |
| `AUTOMARKET_OWNER_PASSWORD` / `AUTOMARKET_APP_PASSWORD` | (unset) | Only needed when migration `0049_app_role_grants` must *create* missing roles. Prefer `scripts/bootstrap.sql` for first-time create. Never commit real values. |

### 2.3 Secrets

Keep secrets in a secret manager (Vault, AWS Secrets Manager, Azure Key Vault,
GCP Secret Manager, Kubernetes Secrets with encryption at rest), injected as
environment variables at start. The `.env` file in the compose example is for
evaluation. Do not keep production secrets in `.env` files on disk, in images,
or in git.

Required secrets: `SECRET_KEY`, `MASTER_ENCRYPTION_KEY`, `BLIND_INDEX_KEY`
(must differ from the master key), both DSNs, mail credentials
(`RESEND_API_KEY` or `SMTP_*`), and the `OAUTH_*` client secrets you use.
Losing `MASTER_ENCRYPTION_KEY` loses every customer's encrypted data; back it
up separately from the database.

---

## 3. Creative Studio

| Setting | Default | Meaning |
|---|---|---|
| `CREATIVE_ENABLED` | `true` | Switches Creative Studio on or off for every workspace. |
| `CREATIVE_ALLOWED_TEXT_MODELS` | empty (**deny all**) | Glob patterns of text models Creative Studio may use, comma-separated. A pattern may start with `<connector>:` to apply to one connector. Until you set it, the picker tells users that an administrator needs to approve AI models. |
| `CREATIVE_DENIED_TEXT_MODELS` | empty | Patterns to refuse. The deny list wins over the allow list. |
| `CREATIVE_ALLOWED_IMAGE_MODELS` | empty (**deny all**) | As above, for image models. "No image" is always allowed. |
| `CREATIVE_DENIED_IMAGE_MODELS` | empty | As above. |
| `CREATIVE_JOBS_PER_MONTH` | `100` | Jobs per workspace per month. `0` = no cap. A portal admin can override it per organisation. Cancelled jobs still count. |
| `CREATIVE_IMAGES_PER_MONTH` | `200` | Generated images per workspace per month. `0` = no cap. |
| `CREATIVE_TOKENS_PER_MONTH` | `0` | Model tokens per workspace per month. `0` = no cap. |
| `CREATIVE_MAX_FORMATS_PER_JOB` / `CREATIVE_MAX_REVISIONS` / `CREATIVE_MAX_IMAGES_PER_JOB` / `CREATIVE_MAX_TOKENS_PER_CALL` | `8` / `5` / `4` / `2000` | Per-job ceilings. |
| `CREATIVE_UPLOAD_MAX_BYTES` / `CREATIVE_UPLOAD_MAX_PIXELS` / `CREATIVE_SVG_MAX_BYTES` | 5 MiB / 40 M / 1 MiB | Upload limits for logos and product photos. SVGs are converted to PNG. |
| `CREATIVE_STORAGE_MAX_BYTES_PER_TENANT` | 2 GiB | Stored files per workspace. `0` disables uploads. |
| `CREATIVE_RENDERER` | `chromium` | `chromium` (full layouts, PDF print output) or `pillow` (simpler, no browser). Use `pillow` on images built with `INSTALL_CHROMIUM=false`. |
| `CREATIVE_CHROMIUM_SANDBOX` | `false` | Chromium's own sandbox needs user namespaces, which most containers lack. Off, the container is the sandbox (read-only root, no capabilities, limits). Turn it on only where user namespaces are available. Production refuses `false` unless `CELERY_ENABLED=true` (renders leave this process) or `CREATIVE_RENDERER=pillow`. `CREATIVE_RENDER_ISOLATED=true` does not count without Celery: Chromium would still run in this process. |
| `CREATIVE_RENDER_ISOLATED` | `false` (compose: `true` on `render-worker` only) | You confirm Chromium runs only in the dedicated `render-worker`: own network shared only with PostgreSQL and Redis, no capabilities, read-only root, limits. Compose sets this on `render-worker` only, not on `api` / `worker` / `beat` / `migrate`. Production treats it as a valid excuse for unsandboxed Chromium only together with `CELERY_ENABLED`. **Residual risk:** that container still holds the database credentials and encryption keys (it stores the files it renders), so a Chromium escape there reaches data. Restrict its egress (§7) and prefer turning the sandbox on where your runtime allows user namespaces. |
| `CREATIVE_RENDER_TIMEOUT_SECONDS` / `CREATIVE_RENDER_SOFT_TIME_LIMIT` / `CREATIVE_RENDER_TIME_LIMIT` | `30` / `600` / `660` | Per-render timeout, and the soft and hard limits for one whole creative job (several formats). The soft limit must be below the hard one. |
| `CREATIVE_MAX_CONCURRENT_RENDERS` / `CREATIVE_JOB_MAX_CONCURRENT` | `2` / `2` | Concurrency caps inside one process. |
| `CREATIVE_BANNER_MAX_KB` | `150` | Banner file-size check (Google Display rejects larger files). |
| `STORAGE_BACKEND` | `postgres` | Where files live. `s3` is not implemented; production refuses it. |
| `IMAGE_FETCH_ALLOWED_HOSTS` / `IMAGE_FETCH_MAX_BYTES` | empty (off) / 20 MB | Hosts an image may be downloaded from when a model returns a link. Empty turns link downloads off. |

Recommended starting lists are in
[`.env.compose.example`](../.env.compose.example). Review them against each
vendor's current model list before going live.

### Chromium memory sizing

- One render process holds one Chromium. Budget about **1.5 GB per process**.
- Compose defaults: `CELERY_RENDER_CONCURRENCY=1`, `mem_limit: 2g`,
  `shm_size: 1gb`, `--max-memory-per-child 1500000` (KiB) and
  `--max-tasks-per-child 50`, so a process that grows is recycled.
- Raising `CELERY_RENDER_CONCURRENCY` to N: set the render-worker memory limit
  to about `N × 1.5 GB + 0.5 GB` and raise `pids_limit`. Keep `shm_size` at
  1 GB or more.
- On Kubernetes, mount an `emptyDir` with `medium: Memory` at `/dev/shm`, and
  set the memory request equal to the limit.

### OpenAI image models

OpenAI retires **`gpt-image-1` on 2026-10-23**. It is on the recommended deny
list. Every OpenAI connection that saved it as its image model must be
updated: **Integrations → OpenAI → Image model**, then pick a current model
that your allow list approves. Until then, image generation through that
connection fails.

---

## 4. Client addresses behind proxies (`TRUSTED_PROXIES`)

The API works out each caller's address itself, for rate limits, sessions and
the audit log. uvicorn runs with `--no-proxy-headers`, so exactly one layer
reads `X-Forwarded-For`. `FORWARDED_ALLOW_IPS` therefore has no effect; do not
switch uvicorn's proxy headers back on.

`TRUSTED_PROXIES` (default empty) lists the proxies whose `X-Forwarded-For`
and `X-Forwarded-Proto` are believed, as IPs or CIDRs, comma-separated. The
API walks `X-Forwarded-For` from the right and takes the first address that
is not a trusted proxy. Empty means the connecting address is the client.

> **Never use `0.0.0.0/0` or `::/0`.** Any caller could then choose its own
> address and step around per-IP rate limits and lockouts. Production refuses
> to start with either.

| Topology | `TRUSTED_PROXIES` |
|---|---|
| nginx (or Caddy) on the same host, reaching the API on loopback outside Docker | `127.0.0.1/32,::1/128` |
| Reverse proxy on the Docker host reaching the published `127.0.0.1:8000` (compose) | `172.28.0.1/32` (the compose gateway) |
| Reverse proxy as a container on the compose network (`172.28.0.0/24`) | That container's pinned address, for example `172.28.0.10/32`. Do not trust the whole subnet: every container on it could then forge addresses. |
| AWS Application Load Balancer | The CIDRs of the subnets the ALB is in. |
| AWS Network Load Balancer with instance targets (client IP preserved), or no proxy | Leave empty. |
| Google Cloud external Application Load Balancer | `130.211.0.0/22,35.191.0.0/16` (Google Front Ends), plus the proxy-only subnet CIDR for regional load balancers. Check Google's current ranges. |
| Azure Application Gateway | The Application Gateway subnet CIDR. |
| Kubernetes ingress-nginx | The controller pods' CIDR. In the controller ConfigMap set `compute-full-forwarded-for: "true"`. If a load balancer sits in front, also set `use-forwarded-headers: "true"` and `proxy-real-ip-cidr` to that load balancer's CIDR, and add it here. Never leave `proxy-real-ip-cidr` at `0.0.0.0/0`. |
| Traefik | Traefik's container or pod CIDR. Set the entry point's `forwardedHeaders.trustedIPs` to the load balancer in front of Traefik, not to everything. |
| Several layers (for example ALB → nginx) | List every layer: the ALB subnets **and** nginx's address. |

Check after deploying: the client address the API records for your own
requests should be your public IP, not a proxy's.

`X-Forwarded-Proto` is honoured only from a trusted proxy, and only `http` or
`https`.

### HTTPS redirect and `EDGE_TLS_TERMINATED`

Production requires `FORCE_HTTPS=true`. The API then redirects plain http to
https, judging the scheme from `X-Forwarded-Proto`, which it believes only
from `TRUSTED_PROXIES`. With `TRUSTED_PROXIES` empty, every request behind a
proxy looks like plain http and would be redirected forever, so production
refuses `FORCE_HTTPS=true` with an empty list unless
`EDGE_TLS_TERMINATED=true`.

| Setting | Default | Meaning |
|---|---|---|
| `EDGE_TLS_TERMINATED` | `false` | TLS ends at a platform edge whose addresses you cannot list (Render). The API skips its own redirect and leaves it to the edge; HSTS is still sent. Prefer listing your proxy in `TRUSTED_PROXIES` wherever you can. |

`/health` is exempt from both the https redirect and the `TRUSTED_HOSTS`
check: container health checks call it on `http://localhost:8000`, and a
redirect or a 400 there would mark a working container unhealthy (or, with
`curl -f` and no `-L`, a broken one healthy). It returns no build details.

### `TRUSTED_HOSTS`

Production requires at least one concrete hostname (`api.example.com`, not
`*` and not empty). `TRUSTED_HOSTS=` (or a list of blanks) is treated like
missing: the host check is not installed, so any `Host` header is accepted
(Host-header reflection and open redirects). Production refuses to start
with that. `/health` stays exempt, so a probe on `Host: localhost` still
works.

### Guardrails that catch common setups

- `CORS_ORIGINS` defaults to localhost, which production refuses. A
  same-origin deployment (console and API behind one hostname) must still set
  `CORS_ORIGINS` to that https origin.
- `TRUSTED_HOSTS` must list the real hostnames. Production refuses `*`, an
  empty value, and a list that is only blanks.
- `CREATIVE_CHROMIUM_SANDBOX=false` needs `CELERY_ENABLED=true` so Chromium
  is not in this process. `CREATIVE_RENDER_ISOLATED=true` does not count
  without Celery; compose sets that flag on `render-worker` only.
- `PORTAL_ADMIN_EMAILS` on reserved domains (`.local`, `.test`,
  `example.com`, ...) is refused: list the real operators.
- `IMAGE_FETCH_ALLOWED_HOSTS`, `SELF_HOSTED_LLM_ALLOWED_HOSTS` and
  `CONNECTOR_PRIVATE_HOSTS_ALLOWED` take plain hostnames (or CIDRs where
  noted); `host:port` and URL entries are refused at startup.

---

## 5. Settings reference (new in this release)

### Network and self-hosted endpoints

| Setting | Default | Meaning |
|---|---|---|
| `SELF_HOSTED_LLM_ALLOWED_HOSTS` | empty | Private hostnames or CIDRs a self-hosted model connector (Ollama, vLLM) may reach. Public addresses need no entry. Cloud metadata addresses (`169.254.0.0/16`, `fd00:ec2::254`, `100.100.100.200`) are always blocked, and `/0` entries are refused. Addresses not on the list must use https. A listed host may use plain http, but put a TLS reverse proxy with authentication in front of the model server: Ollama has no authentication of its own. |
| `SELF_HOSTED_LLM_TIMEOUT_SECONDS` | `60` | Timeout for each call to a self-hosted model. |
| `SELF_HOSTED_LLM_ALLOW_PLAIN_HTTP` | `false` | Allow plain http to a self-hosted model server. Even when on, only for addresses inside a CIDR listed in `SELF_HOSTED_LLM_ALLOWED_HOSTS` (a hostname entry alone never allows http), and no credentials are sent over it. Production logs a warning when it is on. |
| `SELF_HOSTED_LLM_ALLOWED_PORTS` | `443,8000,11434` | Ports a self-hosted model server may listen on (1-65535, at most 20). |
| `CONNECTOR_PRIVATE_HOSTS_ALLOWED` | empty | Private hostnames or CIDRs that connectors with a customer-typed address (WordPress, Magento, a custom API) may reach, for intranet systems you have approved. **https only**, with the certificate verified, even for listed hosts. Metadata addresses are always blocked and `/0` entries are refused. The Site Crawler never uses this list. |
| `MAX_REQUEST_BODY_BYTES` | `1048576` (1 MiB) | Largest request body. Larger requests get 413 (`request_too_large`). Image uploads use `CREATIVE_UPLOAD_MAX_BYTES` instead. |
| `USER_RATE_LIMIT_PER_MINUTE` | `120` | Requests per signed-in user per minute. |
| `LLM_RATE_LIMIT_PER_TENANT_PER_MINUTE` | `20` | Model calls one workspace may start per minute. |

### Models and cost

| Setting | Default | Meaning |
|---|---|---|
| `LLM_PRICE_TABLE` | empty | Per-model prices as JSON: `{"<connector>:<model pattern>": [input, output]}`, in currency units per million tokens. Without a match, the flat `LLM_PRICE_INPUT_PER_MTOK` / `LLM_PRICE_OUTPUT_PER_MTOK` apply; with neither, usage shows "No price set". All costs are estimates. |

### Social publishing and Reddit

| Setting | Default | Meaning |
|---|---|---|
| `REDDIT_ENABLED` | `false` | Reddit stays hidden until you set this. Commercial use of Reddit's API needs Reddit's written approval: get it first. |
| `OAUTH_REDDIT_CLIENT_ID` / `OAUTH_REDDIT_CLIENT_SECRET` | empty | The platform's Reddit app. |
| `REDDIT_USER_AGENT` | empty | `<platform>:<app id>:<version> (by /u/<username>)`, as Reddit requires. |
| `REDDIT_MIN_MINUTES_BETWEEN_POSTS` | `10` | Minimum gap between posts to one subreddit per workspace. |
| `SOCIAL_DAILY_POST_CAP` | `25` | Social posts one workspace may publish in any 24 hours. A workspace setting can override it. |
| `PUBLISHED_LINK_DAYS` | `365` | How long the public link to a published post's picture stays valid (Reddit link posts, some Pinterest pins). Deleting the file revokes the link. |
| `SOCIAL_METRICS_INTERVAL_HOURS` / `SOCIAL_METRICS_LOOKBACK_DAYS` | `6` / `30` | How often post metrics are read, and for how old posts. |
| `X_METRICS_MAX_READS_PER_RUN` | `100` | X bills every read; this caps one metrics run. |
| `SOCIAL_PUBLISH_MAX_ATTEMPTS` / `SOCIAL_PUBLISH_RETRY_MINUTES` | `3` / `15` | Retries for a failed post. |
| `METRICS_BACKFILL_DAYS` | `90` | Days of history read the first time a data source appears. |
| `META_GRAPH_API_VERSION` | blank (v26.0) | Meta Graph API version. |
| `LINKEDIN_API_VERSION` | `202610` | LinkedIn REST version header. |

### Retention and history

| Setting | Default | Meaning |
|---|---|---|
| `RETENTION_LOGIN_CODES_DAYS` | `7` | Spent sign-in codes are deleted after this. |
| `RETENTION_REFRESH_TOKENS_DAYS` | `30` | Expired refresh tokens are deleted after this. |
| `RETENTION_DEMO_ANALYSES_DAYS` | `90` | Public demo analyses are deleted after this. |
| `HISTORY_KEEP_VERSIONS_DEFAULT` / `HISTORY_KEEP_DAYS_DEFAULT` / `HISTORY_MIN_VERSIONS_KEPT` | `365` / `0` (forever) / `5` | Version-history defaults. Workspace admins can change them under **Admin → History retention**. |
| `AUDIT_EXPORT_MAX_ROWS` / `AUDIT_EXPORT_MAX_DAYS` | `50000` / `366` | Limits for one audit-log export. |

Retention runs daily at 03:40 (`retention.prune`) on the worker.

### Sign-in and console

| Setting | Default | Meaning |
|---|---|---|
| `DEV_LOG_SIGN_IN_CODES` | `false` | Development only: with `SMTP_HOST` empty, write each sign-in code to the log. Any other `APP_ENV` refuses to start with it on. |
| `CSP_REPORT_ONLY` | `true` | Console (`console-prod`) only. `true` sends `Content-Security-Policy-Report-Only`; `false` enforces it. Start with `true`, watch the browser console for violations for one release, then set `false`. |
| `VITE_API_BASE_URL` | `/api/v1` | Where the console calls the API: a same-origin path behind your proxy, or the API's https URL (which is then added to the CSP). |

### Redis and Celery

| Setting | Default | Meaning |
|---|---|---|
| `CELERY_ENABLED` | `false` (compose: `true`) | `true` moves agent runs and Creative Studio jobs to the workers. `false` runs them in the API process (test only). |
| `REDIS_PASSWORD` | none (compose: required) | Password for the bundled Redis. Letters and digits only, at least 24 (`python -c "import secrets;print(secrets.token_hex(24))"`). Compose puts it into the broker, result and heartbeat URLs (`redis://:<password>@redis:6379/...`). |
| `CELERY_BROKER_URL` / `CELERY_RESULT_BACKEND` | `redis://localhost:6379/0` / `/1` | Broker and results. Use `rediss://` with a password for any Redis outside the compose network. |
| `REDIS_URL` | blank (uses the broker URL) | Redis for the beat heartbeat. |
| `CELERY_WORKER_CONCURRENCY` | `2` | Processes on the default queue. |
| `CELERY_MAX_TASKS_PER_CHILD` | `50` | Recycle a process after this many tasks. |
| `CELERY_RENDER_CONCURRENCY` | `1` | Processes on the `render` queue (one Chromium each, see §3). |
| `CELERY_MAX_MEMORY_PER_CHILD_KB` | `1500000` | Recycle a render process at this resident memory. |

Run `beat` as exactly one replica. Two would run every schedule twice.

---

## 6. Platform app review

Approval takes days to weeks, so start early. Full steps:
[oauth-setup-social-ads.md](oauth-setup-social-ads.md).

| Platform | What to apply for | Notes |
|---|---|---|
| Meta (Facebook, Instagram) | App Review for Advanced Access to `pages_manage_posts` and `instagram_content_publish`, plus Business Verification. Optional: `read_insights` (Facebook post metrics) and `instagram_manage_insights` (Instagram post metrics). | Without the optional permissions, posting works and metrics say "Reconnect … to allow insights". |
| LinkedIn | The Community Management API (Standard tier) on its own app. | Development tier: 500 calls per app per day. |
| X | Pay-per-use API access. Optional: the `media.write` scope for images. | Without `media.write`, posts go out as text with a note. Reads are billed, so metrics reads are capped. |
| Pinterest | Standard access. | Trial-access Pins are visible only to their creator. |
| Reddit | Written approval for commercial API use. | Off by default (`REDDIT_ENABLED=false`). |

**Existing X and Meta connections must reconnect** (**Integrations →
Sign in again**) to grant the new scopes. Until they do, X posts stay
text-only and Facebook/Instagram metrics stay empty.

---

## 7. Security notes

- **Append-only audit log.** Migration 0020 adds a trigger that refuses
  `UPDATE` and `DELETE` on `audit_log` for every role, and bootstrap.sql and
  0020 `REVOKE UPDATE, DELETE, TRUNCATE` from `automarket_app`. Migration
  0023 adds a `BEFORE TRUNCATE` statement trigger, so not even the owner can
  empty the table in one statement without first disabling it. Consequence:
  deleting an organisation that has audit rows is refused (workspace deletion
  in the console is a soft delete). A GDPR hard delete needs a controlled
  purge run by the owner role, which disables the trigger inside one
  transaction, deletes, and re-enables it. No such tool ships yet: plan,
  review and record any purge as a change.
- **Runtime database role.** The API and Celery workers refuse to start when
  the role is a superuser, has `BYPASSRLS`, or owns any RLS-protected table
  (`ALLOW_PRIVILEGED_DB_ROLE` overrides it with a loud warning — local/test
  only). See [NON-SUPERUSER-DB-ROLE.md](./security/NON-SUPERUSER-DB-ROLE.md)
  for the production rollout runbook.
- **Errors in logs.** Unhandled errors are logged as the exception type, a
  redacted and length-capped message, a hash and the request id, with no
  traceback. The database engine hides bound parameters from its errors.
- **Rate limiter memory.** The in-process limiter keeps at most 50,000
  counters in a least-recently-used table and drops idle ones, so a flood
  from many addresses cannot exhaust memory. It is still per process: put a
  shared limiter at your edge as well.
- **Connector redirects.** A connector that sends credentials (an API key,
  token or password) no longer follows a redirect to a different host: the
  credentials would go with it. If a customer's site redirects, for example
  `example.com` to `www.example.com`, the connection fails until they enter
  the final address (with `www.` and `https://`) in the connector settings.
- **CSP violation reports.** The console's Content-Security-Policy sends
  violations to the API's public `POST /api/v1/csp-report` (`report-uri`,
  plus `report-to` where supported). It is unauthenticated by design, so it
  has a strict budget of 30 reports per minute per client address (on top of
  the general limit) and the normal request-size cap, and it logs only the
  violated directive and the blocked host. On the Render test blueprint the
  `report-uri` names `https://automarket-api.onrender.com`; change it with
  the API host.
- **Render-worker egress.** The `render-worker` must reach the AI vendors'
  APIs for text and images, so it cannot run on an internal-only network.
  Chromium itself loads nothing (JavaScript off, every request aborted).
  Restrict the container's outbound traffic to the model vendors' API hosts,
  PostgreSQL and Redis with a host firewall, network policy or egress proxy
  allow-list. Apply the same to `api` and `worker` where your connected
  platforms allow it.
- **Pin the base image by digest.** Tags move. Build with
  `--build-arg PYTHON_IMAGE=python:3.12-slim@sha256:<digest>` (look it up
  with `docker buildx imagetools inspect python:3.12-slim`), and pin the
  `node`, `nginx`, `postgres` and `redis` images the same way in your
  registry or compose override.
- **Secrets** belong in a secret manager, not `.env` files (see §2.3).
- **Console headers.** `console-prod` sends CSP, `X-Content-Type-Options:
  nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy:
  strict-origin-when-cross-origin`, `Permissions-Policy`, and HSTS when the
  proxy forwards `X-Forwarded-Proto: https`. If a CDN, object store or
  ingress serves the console instead, set the same headers there; copy the
  policy from [`frontend/nginx.conf`](../frontend/nginx.conf) and add your API
  origin to `img-src` and `connect-src` if it differs from the console's.
  The API sends its own security headers, and HSTS when `FORCE_HTTPS=true`.

---

## 8. Upgrade and rollback

This release adds migrations **0019 to 0023**:

| Migration | Adds |
|---|---|
| `0019_creative_studio` | Stored files, brand kits, products, creative jobs (all under row-level security). |
| `0020_audit_append_only` | The audit-log trigger and REVOKE. |
| `0021_metrics_usage_versions` | Metric points, social post metrics, model used per agent run, numbered version history. |
| `0022_creative_tokens_nullable` | Creative job token counts may be "not reported". |
| `0023_audit_truncate_guard` | The audit-log `BEFORE TRUNCATE` trigger. |

**Upgrade**

1. Back up the database (and confirm the backup restores).
2. Add `REDIS_PASSWORD` to `.env` (see §5). Check `MIGRATION_DATABASE_URL`
   ends in `?sslmode=require`, and that `TRUSTED_PROXIES` is set (§4): both
   are now enforced in production.
3. Pull, build, then `docker compose --profile production up -d --build`.
   `migrate` runs first; nothing else starts if it fails.
4. Set the Creative Studio model lists (§3) and update OpenAI image models.
5. Ask customers with X or Meta connections to reconnect (§6).

**Rollback**

- Prefer turning features off over downgrading: `CREATIVE_ENABLED=false`,
  `REDDIT_ENABLED=false`, then restart.
- A downgrade **drops data**: going below 0019 deletes every brand kit,
  product, upload and design; below 0021 deletes metrics and version
  numbering. Take a backup first, and restore it rather than downgrade if you
  may need that data again.
- To downgrade, stop `api`, the workers and `beat`, then run the downgrade
  with **this** release's image (the older image does not contain these
  migrations) as the owner role:
  `docker compose run --rm migrate alembic downgrade 0018_connector_verifications`.
  Then deploy the previous images.
