# syntax=docker/dockerfile:1
FROM python:3.11-slim

ARG BUILD_SHA=dev

# Build metadata (GHCR uses this to link the image to the repo)
LABEL org.opencontainers.image.source="https://github.com/GZC888/prompt-manage" \
      org.opencontainers.image.title="prompt-manage" \
      org.opencontainers.image.description="Lightweight personal Prompt manager (Flask + SQLite)." \
      org.opencontainers.image.licenses="GPL-3.0-only" \
      org.opencontainers.image.revision="${BUILD_SHA}"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    APP_ENV=production \
    APP_PORT=3501 \
    DB_PATH=/app/data/data.sqlite3 \
    BUILD_SHA=${BUILD_SHA}

WORKDIR /app

# gosu lets the entrypoint fix mounted volume ownership as root, then drop
# privileges to the unprivileged app user before starting gunicorn.
RUN apt-get update \
    && apt-get install -y --no-install-recommends gosu \
    && rm -rf /var/lib/apt/lists/*

# Install dependencies first to leverage Docker layer caching.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Application code.
COPY . .

# Create a non-root user and a writable data directory. Only the data dir is
# owned by the runtime user; application code stays root-owned and read-only so
# a compromised process cannot rewrite app.py/templates for persistence. Runtime
# volume ownership is repaired by docker-entrypoint.sh before privileges drop.
RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /app/data /app/data/backups \
    && chown -R appuser:appuser /app/data

ENTRYPOINT ["/app/docker-entrypoint.sh"]

EXPOSE 3501

# Container-level health check hitting the unauthenticated /healthz endpoint.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import os,urllib.request,sys; \
url='http://127.0.0.1:%s/healthz' % os.environ.get('APP_PORT','3501'); \
sys.exit(0) if urllib.request.urlopen(url, timeout=4).getcode()==200 else sys.exit(1)"

CMD ["gunicorn", "-c", "gunicorn.conf.py", "wsgi:app"]
