#!/bin/sh
set -eu

# Migrations run on start unless RUN_MIGRATIONS says otherwise. The compose
# stack sets it to false and migrates once, as the schema owner, in its own
# `migrate` service; Render and local runs keep the default. The false
# spellings match the ones the application's settings accept.
case "$(printf '%s' "${RUN_MIGRATIONS:-true}" | tr '[:upper:]' '[:lower:]')" in
  false|f|0|no|n|off) ;;
  *) alembic upgrade head ;;
esac

# --no-proxy-headers: the application reads X-Forwarded-For itself, trusting
# only TRUSTED_PROXIES, so exactly one layer decides the client address.
exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}" --no-proxy-headers
