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

# `curl` is the healthcheck; `docker-cli` is the client `sandbox/docker.py`
# shells out to. It is installed here rather than in a sidecar-only stage
# because production pushes ONE image and runs both services from it
# (`deploy/docker-compose.prod.yml`), so a CLI that only existed in a separate
# build target would work in dev and fail in prod.
#
# `docker-cli`, not `docker.io`: on trixie (what python:3.12-slim is now) the
# `docker.io` package ships only the daemon -- dockerd and docker-proxy -- and
# no `/usr/bin/docker` at all. Installing it produced a build that failed at
# `docker --version` with exit 127, which is the kind of failure that reads as
# "the image is broken" rather than "you picked the wrong package".
#
# This does not give the backend the ability to reach a daemon: the client is a
# binary, the capability is the socket, and only the `sandbox` service is given
# `docker.sock`. Verified -- the backend image ships the CLI and has nothing to
# talk to:
#
#   $ docker compose exec backend docker info
#   Cannot connect to the Docker daemon at unix:///var/run/docker.sock.
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl docker-cli \
 && rm -rf /var/lib/apt/lists/* \
 && docker --version

COPY --from=build /wheels /wheels
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-index --find-links=/wheels -r /tmp/requirements.txt \
 && rm -rf /wheels /tmp/requirements.txt

# pip is ~7MB of the image and the runtime never installs anything: generated
# code is executed in the sandbox image, which carries its own interpreter and
# its own wheelhouse, and the backend reaches it over the sandbox API rather
# than shelling out. Removing pip also removes the only way to add a package to
# a running container, which is the point.
RUN pip uninstall -y pip setuptools wheel \
 && python -c "import pip" 2>/dev/null && exit 1 || true

# Unprivileged runtime user; /data is the volume mount for run artifacts.
# uid/gid 10001 matches the sandbox image, so a file written by one is readable
# by the other without a second shared group.
RUN groupadd --system --gid 10001 appuser \
 && useradd --system --uid 10001 --gid appuser --create-home appuser \
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
