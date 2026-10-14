# Sandbox image: the only environment in which model-generated code executes.
#
# One image serves both roles the plan needs -- `pytest` for the test loop and
# `uvicorn` for the Playground preview -- selected by command. A second image
# would mean a second thing to build, pull, keep in sync and patch.
#
#   docker build -t agent-team-sandbox:local backend/sandbox
#
# In compose this image is built once and reached through the sandbox sidecar,
# which owns the Docker socket; the backend never does.
#
# Python version MUST match backend/Dockerfile. The app and the sandbox used to
# be 3.12 and 3.11, so 3.12-only syntax passed the pipeline and failed the
# sandbox while a 3.11-only construct behaved differently in production.
ARG PYTHON_VERSION=3.12

# ---- build stage: wheels, and the brotli-free toolchain -----------------
FROM python:${PYTHON_VERSION}-slim AS build

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /wheels

COPY requirements.txt .
RUN pip wheel --wheel-dir /wheels -r requirements.txt

# ---- runtime stage ------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim AS runtime

ARG PYTHON_VERSION=3.12
# Filled in by the build; the runner also stamps these so an image pulled from
# a registry is identifiable.
ARG BUILD_DATE=unknown
ARG VCS_REF=unknown

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore \
    # Generated apps routinely build a SQLite path from the working directory.
    # Pointing them at a tmpfs mount is what lets --read-only coexist with an
    # app that wants to write something.
    TMPDIR=/tmp

COPY --from=build /wheels /wheels
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-index --find-links=/wheels -r /tmp/requirements.txt \
 && rm -rf /wheels /tmp/requirements.txt \
 && pip uninstall -y pip setuptools wheel

# Precompile now rather than letting the first run pay for it: with
# PYTHONDONTWRITEBYTECODE=1 set above, nothing in the container may write .pyc
# at runtime, and the root filesystem is mounted read-only anyway. Compiling
# here also means a syntax error in a wheel is a build failure, not a mystery
# at the first test run.
RUN python -m compileall -q /usr/local/lib/python${PYTHON_VERSION}/site-packages

# Same uid/gid as the backend image's appuser, so a file one writes is
# readable by the other without a shared supplementary group. Not root: every
# escape that matters starts from being able to write outside /app or load a
# module the userland already has.
RUN groupadd --system --gid 10001 sandbox \
 && useradd --system --uid 10001 --gid sandbox --create-home --shell /usr/sbin/nologin sandbox \
 && mkdir -p /app \
 && chown 10001:10001 /app

WORKDIR /app
USER 10001:10001

# OCI image spec v1.1 labels, so `docker inspect` answers what this is.
LABEL org.opencontainers.image.title="agent-team sandbox" \
      org.opencontainers.image.description="Isolated runtime for model-generated Python: pytest and preview" \
      org.opencontainers.image.source="https://github.com/yourorg/multi-agent-team" \
      org.opencontainers.image.version="${VCS_REF}" \
      org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.created="${BUILD_DATE}" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.base.name="docker.io/library/python:${PYTHON_VERSION}-slim"

# Overridden by the runner on every invocation. Kept so a bare `docker run` of
# this image does something safe rather than nothing.
CMD ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider"]
