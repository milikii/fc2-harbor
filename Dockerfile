# syntax=docker/dockerfile:1
#
# Single-container deployment (spec 19). The application is a modular monolith
# that owns its SQLite database, so exactly one container may write /app/data.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=Asia/Shanghai

# Build-time proxy support. The values are applied inline to the apt and pip RUN
# steps only and are never exported into the runtime image: a lingering HTTP_PROXY
# would make curl honour a proxy while aiohttp (trust_env=False) ignores it, which
# is exactly the kind of inconsistency that hides real connectivity problems.
#   docker compose build --build-arg HTTPS_PROXY=http://192.168.1.220:7890
ARG HTTP_PROXY=""
ARG HTTPS_PROXY=""
ARG NO_PROXY=""

# curl is needed for the healthcheck; tzdata for the configured timezone.
RUN http_proxy="$HTTP_PROXY" https_proxy="$HTTPS_PROXY" no_proxy="$NO_PROXY" \
    HTTP_PROXY="$HTTP_PROXY" HTTPS_PROXY="$HTTPS_PROXY" NO_PROXY="$NO_PROXY" \
    apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl tzdata \
    && rm -rf /var/lib/apt/lists/*

# Non-root runtime user (spec 19). uid/gid 1000 must match the NAS download
# owner so qBittorrent and the organizer can share /downloads.
RUN groupadd --gid 1000 app \
    && useradd --uid 1000 --gid app --create-home --shell /usr/sbin/nologin app

WORKDIR /app

# Dependencies first so the layer cache survives application edits.
COPY requirements.txt ./
RUN HTTP_PROXY="$HTTP_PROXY" HTTPS_PROXY="$HTTPS_PROXY" NO_PROXY="$NO_PROXY" \
    http_proxy="$HTTP_PROXY" https_proxy="$HTTPS_PROXY" no_proxy="$NO_PROXY" \
    pip install --no-cache-dir -r requirements.txt

COPY --chown=app:app alembic.ini ./
COPY --chown=app:app app ./app
# One-off importer / migration scripts: runnable inside the image.
COPY --chown=app:app scripts ./scripts
COPY --chown=app:app docker ./docker
COPY --chown=app:app docs ./docs
COPY --chown=app:app README.md LICENSE ./

RUN chmod +x docker/entrypoint.sh \
    && mkdir -p /app/data \
    && chown -R app:app /app/data

USER app

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8080/healthz || exit 1

STOPSIGNAL SIGTERM

ENTRYPOINT ["./docker/entrypoint.sh"]
