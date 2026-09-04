# Biofarm backend.
#
# Built for App Runner, but nothing here is App Runner specific - it is a plain
# container listening on 8000.

FROM python:3.13-slim AS runtime

# - PYTHONDONTWRITEBYTECODE: the image is read-only in practice; .pyc files are
#   just layer weight.
# - PYTHONUNBUFFERED: without it, logs sit in a buffer and arrive late or not at
#   all when a container is killed, which is exactly when you want them.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# libpq is needed by psycopg at runtime; curl is used by the container
# healthcheck below. Nothing else - build tooling stays out of the final image.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libpq5 curl \
    && rm -rf /var/lib/apt/lists/*

# Dependencies as their own layer, ahead of the source copy, so a code change
# does not reinstall 45 packages on every build.
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY alembic.ini ./
COPY alembic ./alembic
COPY app ./app
COPY docker-entrypoint.sh ./
RUN chmod +x docker-entrypoint.sh

# Run unprivileged. Nothing in this image needs to write to disk.
RUN useradd --create-home --uid 10001 appuser \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

# Readiness, not liveness: an instance that cannot reach the database should be
# taken out of rotation rather than reported healthy.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://localhost:8000/api/v1/health/ready || exit 1

ENTRYPOINT ["./docker-entrypoint.sh"]
