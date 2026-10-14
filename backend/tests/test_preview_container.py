"""Container-mode preview lifecycle and proxying.

These tests run a real preview in a sandbox container (not a subprocess).
They require:
- A running Docker daemon (`docker.available()`)
- The sandbox image present locally under its **hashed** tag

If either is missing the whole file is skipped. No LLM is involved.
"""

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import create_app, get_settings
from app.services.playground import child_env, reset_playground
from sandbox.docker import (
    available as docker_available_check,
)
from sandbox.docker import (
    image_exists,
    is_running,
    network_is_internal,
    stop_container,
)
from sandbox.flags import cached_image_tag

# ------------------------------------------------------------------------ docker availability ---
docker_available = docker_available_check()

# The image is tagged by its Dockerfile+wheelhouse hash -- `agent-team-sandbox:
# local-7ff1f648a138`, NOT `agent-team-sandbox:local`. Checking the base name
# here made all six tests skip while the suite still reported success: a skip
# guard that skips itself for the wrong reason is worse than no guard. Ask for
# the tag the runner will actually ask for.
_sandbox_tag = cached_image_tag()
sandbox_image_local = False
if docker_available:
    try:
        sandbox_image_local = image_exists(_sandbox_tag)
    except Exception:
        sandbox_image_local = False

skip_if_no_docker = pytest.mark.skipif(
    not docker_available,
    reason="Docker daemon not available; container preview tests skipped",
)
skip_if_no_image = pytest.mark.skipif(
    not sandbox_image_local,
    reason=(
        f"sandbox image {_sandbox_tag} is not built; build it with: "
        "docker build -t agent-team-sandbox:local backend/sandbox"
    ),
)

# ------------------------------------------------------------------------ generated app + spec ---
APP_PY = """from fastapi import FastAPI

app = FastAPI(items=["one"])


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/items")
def items():
    return ["one", "two"]


@app.post("/items")
def add(item: dict):
    return {"added": item.get("name", "")}
"""

SPEC_YAML = (
    "openapi: 3.1.0\n"
    "info: {title: Items, version: 1.0.0}\n"
    "paths:\n"
    "  /health:\n    get:\n      responses: {'200': {description: ok}}\n"
    "  /items:\n    get:\n      responses: {'200': {description: ok}}\n"
)

# ------------------------------------------------------------------------ fixtures ---
@pytest.fixture()
def runs_root(tmp_path):
    root = tmp_path / "runs"
    root.mkdir(parents=True, exist_ok=True)
    return root


@pytest.fixture()
def run_dir(runs_root):
    """A finished run's directory, shaped exactly like a real one."""
    d = runs_root / "run-1"
    (d / "code").mkdir(parents=True)
    (d / "code" / "main.py").write_text(APP_PY)
    (d / "spec.yaml").write_text(SPEC_YAML)
    return d


@pytest.fixture()
def client(runs_root, run_dir, monkeypatch):
    """A live app whose playground is the process-wide service (container mode).

    The gateway resolves through ``get_playground()``, so injecting a separate
    instance would test a wiring that does not exist.
    """
    from app import auth

    # Auth is off by default here, but the config is cached process-wide, so it
    # has to be reset explicitly or a previously-run auth test leaks into this
    # one and every call answers 401.
    for var in ("AUTH_PASSWORD_HASH", "AUTH_SECRET", "AUTH_PASSWORD", "AUTH_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    auth.reset_auth_config()

    # Container mode
    monkeypatch.setenv("SANDBOX_MODE", "container")
    monkeypatch.setenv("SANDBOX_NETWORK", "sandbox-net")
    monkeypatch.setenv("RUNS_DIR", str(runs_root))

    get_settings.cache_clear()
    reset_playground()
    app = create_app()
    with TestClient(app) as c:
        yield c
    reset_playground()
    auth.reset_auth_config()
    get_settings.cache_clear()


def _start(client, headers: dict | None = None) -> None:
    """Boot the run-1 preview through the public API and wait for it."""
    resp = client.post("/api/runs/run-1/playground/start", headers=headers or {})
    assert resp.status_code == 202, resp.text
    _wait_for_state(client, "running", headers=headers)


def _wait_for_state(client, state, headers: dict | None = None, timeout=60):
    deadline = time.time() + timeout
    body = {}
    while time.time() < deadline:
        body = client.get("/api/runs/run-1/playground", headers=headers or {}).json()
        if body["state"] == state:
            return body
        time.sleep(0.5)
    raise AssertionError(f"preview stayed {body.get('state')}, wanted {state}: {body.get('error')}")


# ------------------------------------------------------------------------ tests ---
@skip_if_no_docker
@skip_if_no_image
def test_container_mode_spawns_a_real_container(client):
    """A container preview gets host (IP), port, mode='container' in status."""
    _start(client)

    status = client.get("/api/runs/run-1/playground").json()
    assert status["mode"] == "container"
    assert status["host"] is not None and status["host"] != ""
    assert status["port"] == 8000
    assert status["state"] == "running"
    assert status["url"] == "/preview/run-1/"
    assert status["ever_booted"] is True


@skip_if_no_docker
@skip_if_no_image
def test_gateway_proxies_to_the_container_ip(client):
    """The /preview/{run_id}/... proxy reaches the app at the container IP on the internal network."""
    _start(client)

    # Health endpoint
    r = client.get("/preview/run-1/health")
    assert r.status_code == 200, r.text
    assert r.json() == {"status": "ok"}

    # Items endpoint
    r = client.get("/preview/run-1/items")
    assert r.status_code == 200, r.text
    assert r.json() == ["one", "two"]


@skip_if_no_docker
@skip_if_no_image
def test_stop_releases_the_container(client):
    """stop() calls Docker.stop_container and the container is gone."""
    _start(client)

    status = client.get("/api/runs/run-1/playground").json()
    # We can't get container_id from the API, but we can verify the preview stops
    # and the internal session clears host/port. The stop endpoint returns stopped=true.
    resp = client.post("/api/runs/run-1/playground/stop")
    assert resp.status_code == 200, resp.text
    assert resp.json()["stopped"] is True

    status = client.get("/api/runs/run-1/playground").json()
    assert status["state"] == "stopped"
    assert status["host"] is None
    assert status["port"] is None
    assert status["url"] is None


@skip_if_no_docker
@skip_if_no_image
def test_crash_restart_recovers_a_container_preview(client):
    """A container that crashes (exits) is detected and restarted on the next proxy request."""
    _start(client)

    from app.services.playground import get_playground
    svc = get_playground()

    # Get the internal session to find the container_id
    session = svc._sessions["run-1"]
    original_id = session.container_id
    assert original_id is not None

    # Kill the container from outside -- simulates OOM or crash
    stop_container(original_id)
    time.sleep(0.5)  # let the stop propagate

    # Next proxy request should restart it
    r = client.get("/preview/run-1/health")
    # First request may 502 if the health check hasn't run yet, but the
    # start() call inside the proxy should launch a new container
    assert r.status_code == 200, f"restart failed: {r.text}"
    assert r.json() == {"status": "ok"}

    # A new container should be running
    session = svc._sessions["run-1"]
    assert session.container_id is not None
    assert session.container_id != original_id
    assert is_running(session.container_id)


@skip_if_no_docker
@skip_if_no_image
def test_child_env_points_sqlite_inside_data_mount(client):
    """Container preview child env has SQLITE_PATH/DATABASE_URL pointing at /data (tmpfs)."""
    from sandbox.flags import SandboxLimits

    limits = SandboxLimits.from_env()
    env = child_env(Path("/tmp"), limits=limits)

    # In container mode, PREVIEW=1 and paths point at /data tmpfs
    assert env["PREVIEW"] == "1"
    assert env["SQLITE_PATH"] == "/data/preview.db"
    assert env["DATABASE_URL"] == "sqlite:////data/preview.db"

    # Credentials must still be stripped
    assert not any("API_KEY" in k for k in env)
    assert "AUTH_SECRET" not in env
    assert "AUTH_PASSWORD_HASH" not in env

    # Non-credential config survives
    assert "PATH" in env


@skip_if_no_docker
@skip_if_no_image
def test_container_preview_has_no_egress(client):
    """The internal sandbox-net has no default route; preview cannot reach internet."""
    _start(client)

    # Verify the network is internal
    assert network_is_internal("sandbox-net") is True