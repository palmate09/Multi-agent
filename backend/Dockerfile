# ---- build stage -------------------------------------------------------
FROM python:3.12-slim AS build

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /wheels

# Build wheels once so the runtime stage installs without a compiler.
COPY requirements.txt .
RUN pip wheel --wheel-dir /wheels -r requirements.txt

# ---- runtime stage -----------------------------------------------------
FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    RUNS_DIR=/data/runs \
    BACKEND=/app

RUN apt-get update \
 && apt-get install -y --no-install-recommends curl \
 && rm -rf /var/lib/apt/lists/*

COPY --from=build /wheels /wheels
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-index --find-links=/wheels -r /tmp/requirements.txt \
 && rm -rf /wheels /tmp/requirements.txt

# Unprivileged runtime user; /data is the volume mount for run artifacts.
RUN groupadd --system --gid 1001 appuser \
 && useradd --system --uid 1001 --gid appuser --create-home appuser \
 && mkdir -p /data/runs \
 && chown -R appuser:appuser /data

WORKDIR /app

# Agent/graph/schemas/sandbox/evals sit next to app/ so their existing
# top-level absolute imports keep resolving with PYTHONPATH=/app.
COPY app ./app
COPY agents ./agents
COPY graph ./graph
COPY schemas ./schemas
COPY evals ./evals
COPY sandbox ./sandbox

USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8000/health || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", \
     "--proxy-headers", "--forwarded-allow-ips", "*"]