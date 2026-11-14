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

# curl is needed for the healthcheck; tzdata for the configured timezone.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl tzdata \
    && rm -rf /var/lib/apt/lists/*

# Non-root runtime user (spec 19). uid/gid 1000 must match the NAS download
# owner so qBittorrent and the organizer can share /downloads.
RUN groupadd --gid 1000 app \
    && useradd --uid 1000 --gid app --create-home --shell /usr/sbin/nologin app

WORKDIR /app

# Dependencies first so the layer cache survives application edits.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY --chown=app:app alembic.ini ./
COPY --chown=app:app app ./app
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
