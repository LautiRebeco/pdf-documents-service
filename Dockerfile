# syntax=docker/dockerfile:1.7
# Imagen base e instalador fijados por digest (supply chain).
ARG PYTHON_IMAGE=python:3.14-slim@sha256:9aaa474097d094040649f2162a627c0049b408f52138d980916788d80f04802f
ARG UV_VERSION=0.13.9
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.13.9@sha256:d3b601f0c7b118bc1753febf7c41cc80981df678445e0254284eecf84db79a7d

# ---------------------------------------------------------------------------
# Build: las dependencias se resuelven desde pyproject.toml + uv.lock (frozen).
# ---------------------------------------------------------------------------
FROM ${PYTHON_IMAGE} AS builder

COPY --from=${UV_IMAGE} /uv /uvx /usr/local/bin/

WORKDIR /app
ENV UV_PROJECT_ENVIRONMENT=/opt/venv

COPY pyproject.toml uv.lock .python-version ./
COPY app/ ./app/
RUN uv sync --frozen --no-default-groups --no-cache

# ---------------------------------------------------------------------------
# Runtime: usuario no privilegiado, healthcheck y arranque configurable.
# ---------------------------------------------------------------------------
FROM ${PYTHON_IMAGE} AS runtime

ARG APP_VERSION=dev
LABEL org.opencontainers.image.title="pdf-documents-service" \
      org.opencontainers.image.description="Servicio de documentos: MongoDB, saga, caché y resiliencia" \
      org.opencontainers.image.version="${APP_VERSION}" \
      org.opencontainers.image.source="https://example.local/pdf-extract/pdf-documents-service"

RUN addgroup --system appgroup && \
    adduser --system --ingroup appgroup --home /home/appuser appuser && \
    mkdir -p /home/appuser && chown -R appuser:appgroup /home/appuser

WORKDIR /app

COPY --from=builder --chown=appuser:appgroup /opt/venv /opt/venv
COPY --chown=appuser:appgroup app/ ./app/

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8000 \
    APP_LOG_FORMAT=json \
    APP_DISABLE_DOTENV=1 \
    APP_DOCS_ENABLED=false

USER appuser
EXPOSE 8000
STOPSIGNAL SIGTERM

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PORT','8000')+'/health/live', timeout=3).read()" || exit 1

# WEB_CONCURRENCY permite escalar workers dentro del contenedor. Con >1 worker,
# configurar REDIS_URL para que la caché sea compartida entre procesos.
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port \"${PORT:-8000}\" --workers \"${WEB_CONCURRENCY:-1}\" --no-access-log"]
