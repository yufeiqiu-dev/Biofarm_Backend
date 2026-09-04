#!/bin/sh
set -e

# Apply migrations before serving.
#
# RDS is private, so a CI runner cannot reach it - migrating here is the
# pragmatic place. Alembic takes an advisory lock, so the brief two-instance
# overlap during a rolling deploy is safe: the second waits, then finds nothing
# to do.
#
# `set -e` means a failed migration aborts the container rather than starting an
# app against a schema it does not match. That is deliberate - a deploy that
# fails loudly is recoverable, one that half-works is not.
echo "Running database migrations..."
alembic upgrade head
echo "Migrations applied."

# Single worker by default: this runs behind a platform that scales by adding
# containers, so in-container workers just multiply database connections.
exec uvicorn app.main:app \
    --host 0.0.0.0 \
    --port "${PORT:-8000}" \
    --workers "${UVICORN_WORKERS:-1}" \
    --proxy-headers \
    --forwarded-allow-ips '*'
