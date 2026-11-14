#!/bin/sh
# Container entrypoint: apply migrations, then hand PID 1 to the application so
# SIGTERM reaches the graceful shutdown path in app/main.py.
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

exec python -m app.main
