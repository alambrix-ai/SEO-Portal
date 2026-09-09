# syntax=docker/dockerfile:1
FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# libpq for psycopg, curl for the health check. Build tools are not needed:
# every wheel in requirements.txt ships binaries for this platform.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libpq5 curl \
    && rm -rf /var/lib/apt/lists/*

# Dependencies first, so a code change does not reinstall them.
COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY . .

# Run as a non-root user.
RUN useradd --create-home --uid 10001 automarket \
    && chown -R automarket:automarket /app
USER automarket

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://localhost:8000/health || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
