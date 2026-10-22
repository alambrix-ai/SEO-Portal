# syntax=docker/dockerfile:1

# The base image is a build argument so a release can pin it by digest, which
# a tag cannot guarantee (tags move). Look the digest up with
#   docker buildx imagetools inspect python:3.12-slim
# and build with
#   --build-arg PYTHON_IMAGE=python:3.12-slim@sha256:<digest>
# The default stays a tag because this file cannot know the digest your
# registry or mirror serves.
ARG PYTHON_IMAGE=python:3.12-slim
FROM ${PYTHON_IMAGE} AS base

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

# Headless Chromium (the "shell" build: screenshots and PDFs only) for
# Creative Studio rendering, plus Noto fonts so text never falls back to boxes.
# Installed as root, before USER, into a fixed path the app user can read.
# Build with --build-arg INSTALL_CHROMIUM=false for an image without it; the
# renderer then falls back to Pillow.
ARG INSTALL_CHROMIUM=true
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers
RUN case "$INSTALL_CHROMIUM" in \
      true) \
        playwright install --with-deps --only-shell chromium \
        && apt-get update \
        && apt-get install -y --no-install-recommends fonts-noto-core \
        && rm -rf /var/lib/apt/lists/* \
        && chmod -R a+rX "$PLAYWRIGHT_BROWSERS_PATH" ;; \
      false) echo "Skipping Chromium install" ;; \
      *) echo "INSTALL_CHROMIUM must be true or false" >&2; exit 1 ;; \
    esac

COPY . .

# Refuse to build with a font that differs from the one recorded in fonts.lock
# (format: <sha256> <path> <url>), so a swapped or corrupted file never ships.
RUN cd /app/app/services/creative/assets/fonts \
    && grep -v '^#' fonts.lock | awk '{print $1"  "$2}' | sha256sum -c --strict --quiet

# Run as a non-root user that cannot change the code it runs: /app stays
# owned by root and read-only to everyone else. The app writes nothing under
# /app (no bytecode, see PYTHONDONTWRITEBYTECODE); scratch files go to /tmp,
# and the user's own home is the only other writable place. Compose also
# mounts the root filesystem read-only.
RUN useradd --create-home --uid 10001 automarket \
    && chown -R root:root /app \
    && chmod -R u=rwX,go=rX /app \
    && chmod 0755 /app/docker-entrypoint.sh
USER automarket

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD sh -c 'curl -fsS "http://localhost:${PORT:-8000}/health" || exit 1'

CMD ["./docker-entrypoint.sh"]
