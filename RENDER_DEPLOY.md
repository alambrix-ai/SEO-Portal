# Free deploy on Render + Neon

This is the free path for AutoMarket AI:

| Piece | Where | Plan |
|---|---|---|
| Backend API | Render Docker web service | Free |
| Frontend console | Render static site | Free |
| PostgreSQL | [Neon](https://neon.tech) | Free |
| Redis / Celery | Not used | — |

Agents run with the **in-process scheduler** (`SCHEDULER_ENABLED=true`,
`CELERY_ENABLED=false`). No Redis or worker service is required.

Local `docker compose` is unchanged for development. Render does **not** run
`docker-compose.yml` as one unit.

## OTP / email on Render

Render **blocks outbound SMTP** on free (and many paid) plans. Gmail SMTP will
fail with `Network is unreachable` even when frontend and backend are connected.

Use [Resend](https://resend.com) instead:

1. Create a free Resend account and API key.
2. Set on `automarket-api`:
   - `RESEND_API_KEY=<your key>`
   - `EMAIL_FROM=onboarding@resend.dev` (testing; only sends to your Resend signup email)
   - or verify your domain and use `EMAIL_FROM=no-reply@yourdomain.com`
3. Redeploy the API.

For local `.env` OTP with Neon, SMTP still works from your machine.

## Local `.env` (Neon)

In `backend/.env`, set one line — it overrides the `POSTGRES_*` fields:

```env
DATABASE_URL=postgresql://USER:PASSWORD@HOST/DATABASE?sslmode=require
POSTGRES_SSLMODE=require
```

Paste your Neon connection string as `DATABASE_URL`. Do not prefix it twice
(`DATABASE_URL=DATABASE_URL=...` is invalid). The backend rewrites
`postgresql://` to `postgresql+psycopg://` automatically.

Then from `backend/`:

```bash
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt   # Windows
alembic upgrade head
python manage.py check
uvicorn app.main:app --reload --port 8000
```

## 1. Create Neon Postgres

1. Sign up at https://neon.tech and create a free project.
2. Copy the connection string. Prefer the one that includes `sslmode=require`.
3. Keep it for `DATABASE_URL` on the API. A normal Neon URL such as
   `postgresql://...` is fine — the backend rewrites it to the `psycopg` driver.

## 2. Generate secrets

Run locally:

```bash
python -c "import secrets;print('SECRET_KEY='+secrets.token_urlsafe(48))"
python -c "import os,base64;print('MASTER_ENCRYPTION_KEY='+base64.b64encode(os.urandom(32)).decode())"
python -c "import os,base64;print('BLIND_INDEX_KEY='+base64.b64encode(os.urandom(32)).decode())"
```

`MASTER_ENCRYPTION_KEY` and `BLIND_INDEX_KEY` must be different values.

You also need:

- SMTP or Resend for sign-in codes: prefer `RESEND_API_KEY` on Render free
  (SMTP ports are blocked), plus `EMAIL_FROM`
- AI model keys are **not** set in Render env — connect OpenAI / Claude /
  Gemini / Perplexity in the console, then pick one when configuring each agent

Production refuses to start without mail delivery and encryption keys.

## 3. Deploy with the Blueprint

1. Push this repo to GitHub.
2. In Render: **New → Blueprint**.
3. Select the repo (root `render.yaml`).
4. When prompted, paste:
   - `DATABASE_URL` (Neon)
   - `MASTER_ENCRYPTION_KEY`
   - `BLIND_INDEX_KEY`
   - `RESEND_API_KEY` (or SMTP values) and `EMAIL_FROM`
5. Deploy. Render creates:
   - `automarket-api` (Docker, free)
   - `automarket-console` (static, free)

## 4. Fix URLs after first deploy

If Render assigns different subdomains than the placeholders:

On **automarket-api**:

- `PUBLIC_BASE_URL` = `https://<actual-console>.onrender.com`
- `TRUSTED_HOSTS` = `<actual-api>.onrender.com`
- `CORS_ORIGINS` = `https://<actual-console>.onrender.com`

On **automarket-console**:

- `VITE_API_BASE_URL` = `https://<actual-api>.onrender.com/api/v1`

Then **redeploy the console** (static env vars are baked at build time).

## 5. Manual create (if you skip Blueprint)

### Backend — New Web Service

- Runtime: Docker
- Branch: `main`
- Root directory: leave empty
- Dockerfile path: `./backend/Dockerfile`
- Docker context: `./backend`
- Plan: Free
- Start / docker command:

```bash
sh -c "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port $PORT --proxy-headers"
```

Set the env vars listed in `render.yaml` under `automarket-api`.

### Frontend — New Static Site

- Build command: `cd frontend && npm ci && npm run build`
- Publish directory: `frontend/dist`
- Env: `VITE_API_BASE_URL=https://<your-api>.onrender.com/api/v1`
- Rewrite: `/*` → `/index.html`

## Free-tier limits

- The free API sleeps after ~15 minutes with no traffic.
- Cold starts are slow.
- Scheduled agents only run while the API is awake.
- Neon free storage and compute are capped; fine for demos, not heavy production.

## What this path deliberately skips

- Render Postgres (paid / limited free history)
- Render Key Value / Redis
- Celery worker
- Dockerized frontend on Render (static site is free and enough)
