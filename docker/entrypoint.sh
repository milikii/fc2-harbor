#!/bin/sh
# Container entrypoint.
#
# Applies migrations, then:
#   * no arguments -> start the application as PID 1 (SIGTERM reaches the graceful
#     shutdown path in app/main.py);
#   * arguments   -> run that command instead, which is how the one-off tools are
#     invoked, e.g. `docker compose run --rm app python scripts/import_legacy.py`.
set -eu

if [ -z "${FC2_CONFIG:-}" ]; then
    export FC2_CONFIG=/app/config/config.yaml
fi

if [ ! -f "$FC2_CONFIG" ]; then
    echo "configuration file not found: $FC2_CONFIG" >&2
    echo "copy config/example.yaml to config/config.yaml on the host" >&2
    exit 1
fi

echo "applying database migrations..."
alembic upgrade head

if [ "$#" -gt 0 ]; then
    exec "$@"
fi

exec python -m app.main
