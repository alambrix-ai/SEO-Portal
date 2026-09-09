# Deploying on Render

This repository can be deployed on Render, but not as a single `docker-compose`
application. Render deploys each part separately, so the local compose stack is
mapped like this:

- `frontend/` -> Docker web service
- `backend/` -> Docker web service
- PostgreSQL -> Render managed Postgres
- Redis/Celery -> Render Key Value + Render worker

The included `render.yaml` now describes that setup.

## What changed

- `frontend/Dockerfile` now builds a production image instead of running the
  Vite dev server.
- `frontend/nginx.conf` serves the built SPA and rewrites all routes to
  `index.html`.
- `frontend/docker-entrypoint.sh` generates `/config.js` at container startup
  so `VITE_API_BASE_URL` can come from Render environment variables.
- `frontend/src/api/client.ts` reads that runtime config first and falls back
  to the Vite build-time env or `/api/v1`.
- `backend/Dockerfile` now listens on Render's `PORT` environment variable.
- `render.yaml` now provisions:
  - a managed Postgres database
  - a managed Redis-compatible Key Value instance
  - the API web service
  - a Celery worker service
  - the frontend web service

## Render deployment steps

1. Push the repo to GitHub.
2. In Render, click **New -> Blueprint**.
3. Select the GitHub repo and deploy from the root `render.yaml`.
4. Wait for Render to create:
   - `automarket-db`
   - `automarket-cache`
   - `automarket-api`
   - `automarket-worker`
   - `automarket-console`

## Required environment values

Render can generate `SECRET_KEY`, but you must provide the following yourself.

Set the same values on both `automarket-api` and `automarket-worker`:

- `MASTER_ENCRYPTION_KEY`
- `BLIND_INDEX_KEY`
- `LLM_MODEL`
- `ANTHROPIC_API_KEY`
- `SMTP_HOST`
- `SMTP_USER`
- `SMTP_PASSWORD`
- `EMAIL_FROM`

Use these commands locally to generate the two encryption keys:

```bash
python -c "import os,base64;print(base64.b64encode(os.urandom(32)).decode())"
python -c "import os,base64;print(base64.b64encode(os.urandom(32)).decode())"
```

Use the first output for `MASTER_ENCRYPTION_KEY` and the second for
`BLIND_INDEX_KEY`. They must be different.

## Important post-deploy fix

The blueprint uses placeholder public URLs:

- `https://automarket-api.onrender.com`
- `https://automarket-console.onrender.com`

If Render assigns different subdomains, update these values:

- On `automarket-api` and `automarket-worker`:
  - `PUBLIC_BASE_URL`
  - `TRUSTED_HOSTS`
  - `CORS_ORIGINS`
- On `automarket-console`:
  - `VITE_API_BASE_URL`

Then redeploy the changed services.

## Why Postgres and Redis are not deployed as your own Docker containers

Render is a good fit for stateless containers, but database and queue
infrastructure should usually use Render-managed services there:

- managed Postgres gives you persistent storage, backups, and internal network
  wiring
- managed Key Value is the correct Redis-compatible service for Celery
- trying to run a database container the same way as app containers is more
  fragile on Render than using its native data services

If you strictly need self-hosted Docker containers for Postgres or Redis,
Render is not the ideal target for that architecture. A VM-based host such as
Railway, Hetzner, DigitalOcean, or AWS ECS/EC2 is usually a better fit.
