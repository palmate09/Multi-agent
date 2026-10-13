"""The playground: subprocess lifecycle, proxying, auth and env hardening.

Everything here runs against a tiny generated FastAPI app written to a
temporary run directory — a real uvicorn subprocess on a real loopback port, so
the boot probe and the proxy are exercised rather than mocked. No LLM.
"""

import json
import socket
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app, get_settings
from app.services.playground import (
    STRIPPED_ENV,
    PlaygroundService,
    child_env,
)

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


@pytest.fixture()
def runs_root(tmp_path):
    """Where the service looks for run directories."""
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
def service(runs_root):
    svc = PlaygroundService(
        Settings(runs_dir=runs_root, preview_boot_timeout_seconds=25),
        runs_dir=runs_root,
    )
    yield svc
    svc.shutdown()


def _wait_running(svc, run_id, timeout=40):
    deadline = time.time() + timeout
    session = None
    while time.time() < deadline:
        session = svc.start(run_id)
        if session.state in {"running", "crashed"}:
            return session
        time.sleep(0.3)
    return session


# ------------------------------------------------------------- env hardening ----
def test_child_env_strips_every_platform_credential(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "super-secret")
    monkeypatch.setenv("GEMINI_API_KEY", "super-secret")
    monkeypatch.setenv("AUTH_SECRET", "super-secret")
    monkeypatch.setenv("AUTH_PASSWORD_HASH", "super-secret")
    monkeypatch.setenv("TOTALLY_UNRELATED_API_KEY", "super-secret")
    env = child_env(Path("/tmp"))
    assert not any("API_KEY" in k for k in env)
    assert not any(k in env for k in STRIPPED_ENV)
    assert "super-secret" not in "".join(f"{k}={v}" for k, v in env.items())
    # Non-credential config must survive: the child needs PATH and PYTHONPATH.
    assert "PATH" in env


def test_child_env_points_sqlite_inside_the_preview_dir(tmp_path):
    env = child_env(tmp_path)
    assert env["PREVIEW"] == "1"
    assert env["SQLITE_PATH"] == str(tmp_path / "preview.db")
    assert env["DATABASE_URL"].startswith("sqlite:///")


# ------------------------------------------------------------------ lifecycle ----
def test_start_boots_the_app_and_reports_running(service, run_dir):
    session = _wait_running(service, "run-1")
    assert session.state == "running", session.error
    assert session.entry == "main:app"
    assert session.port > 0
    assert Path(session.log_path).exists()


def test_start_is_idempotent(service, run_dir):
    first = _wait_running(service, "run-1")
    assert first.state == "running"
    again = service.start("run-1")
    assert again.port == first.port, "a second start took a second port"


def test_stop_kills_the_process_and_clears_the_directory(service, run_dir):
    session = _wait_running(service, "run-1")
    port = session.port
    assert service.stop("run-1") is True
    assert not session.alive()
    # The port is free again: a fresh bind must succeed.
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", port))
    assert not Path(session.workdir).exists()


def test_stop_is_safe_for_an_unknown_run(service):
    assert service.stop("never-existed") is False


def test_a_crashing_app_is_reported_as_crashed_not_raised(service, runs_root):
    d = runs_root / "run-2"
    (d / "code").mkdir(parents=True)
    (d / "code" / "main.py").write_text("import nonexistent_module_xyz\n\napp = object()\n")
    session = service.start("run-2")
    assert session.state == "crashed"
    assert session.error
    assert "nonexistent_module_xyz" in session.log_tail()


def test_a_run_with_no_app_object_is_refused(service, runs_root):
    d = runs_root / "run-3"
    (d / "code").mkdir(parents=True)
    (d / "code" / "main.py").write_text("def helper():\n    return 1\n")
    with pytest.raises(RuntimeError, match="ASGI app"):
        service.start("run-3")


def test_a_missing_run_directory_is_refused(service):
    with pytest.raises(FileNotFoundError):
        service.start("no-such-run")


def test_reaper_thread_actually_ticks(service, run_dir, monkeypatch):
    """The reaper must run as a thread, not only when reap() is called by hand.

    Every other TTL test calls ``service.reap()`` directly, so none of them
    exercise ``_reap_loop`` — which shipped calling a method that no longer
    existed, so the thread raised AttributeError on its first tick and died.
    Nothing reaped idle previews and nothing logged an error. This drives a
    real tick.
    """
    session = _wait_running(service, "run-1")
    session.last_hit = time.time() - 10_000  # expired, but nobody is polling
    svc = PlaygroundService(
        Settings(
            runs_dir=service.runs_dir,
            preview_boot_timeout_seconds=25,
            preview_reap_interval=1,
        ),
        runs_dir=service.runs_dir,
    )
    svc._sessions = service._sessions  # share state with the session above
    svc.start_reaper()
    try:
        deadline = time.time() + 15
        while time.time() < deadline and svc._sessions:
            time.sleep(0.25)
        assert not svc._sessions, "the reaper thread never ran"
        assert not session.alive(), "the expired preview's process is still up"
    finally:
        svc.shutdown()


def test_reaper_thread_survives_a_failing_tick(service, run_dir, monkeypatch):
    """One bad tick must not silently end reaping for the life of the process."""
    svc = PlaygroundService(
        Settings(
            runs_dir=service.runs_dir,
            preview_boot_timeout_seconds=25,
            preview_reap_interval=1,
        ),
        runs_dir=service.runs_dir,
    )
    calls = {"n": 0}
    real = svc.reap

    def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")
        return real()

    monkeypatch.setattr(svc, "reap", flaky)
    svc.start_reaper()
    try:
        deadline = time.time() + 15
        while time.time() < deadline and calls["n"] < 3:
            time.sleep(0.25)
        assert calls["n"] >= 3, f"reaper stopped after {calls['n']} tick(s)"
    finally:
        svc.shutdown()


def test_status_of_an_unknown_run_is_stopped(service):
    status = service.status("nothing")
    assert status["state"] == "stopped"
    assert status["url"] is None
    assert status["port"] is None


def test_ttl_reaper_expires_an_idle_preview(service, run_dir, monkeypatch):
    session = _wait_running(service, "run-1")
    assert session.state == "running"
    # Backdate last_hit rather than sleeping the whole TTL.
    session.last_hit = time.time() - 10_000
    assert service.reap() == 1
    assert service.status("run-1")["state"] == "stopped"
    assert not session.alive()


def test_ttl_reaper_leaves_an_active_preview_alone(service, run_dir):
    session = _wait_running(service, "run-1")
    session.last_hit = time.time()
    assert service.reap() == 0
    assert session.alive()
    service.stop("run-1")


def test_concurrency_cap_is_enforced(service, runs_root):
    capped = PlaygroundService(
        Settings(runs_dir=runs_root, preview_max_concurrent=1, preview_boot_timeout_seconds=25),
        runs_dir=runs_root,
    )
    try:
        for i in (1, 2):
            d = runs_root / f"cap-{i}"
            (d / "code").mkdir(parents=True)
            (d / "code" / "main.py").write_text(APP_PY)
            (d / "spec.yaml").write_text(SPEC_YAML)
        assert capped.start("cap-1").state in {"running", "starting", "crashed"}
        with pytest.raises(RuntimeError, match="limit reached"):
            capped.start("cap-2")
    finally:
        capped.shutdown()


def test_dead_process_is_reported_as_crashed(service, run_dir):
    session = _wait_running(service, "run-1")
    session.proc.kill()
    session.proc.wait(timeout=5)
    assert service.status("run-1")["state"] == "crashed"


def test_crash_restart_recovers_on_the_next_proxy_request(service, run_dir):
    """A preview killed underneath us comes back once, not 502 forever."""
    session = _wait_running(service, "run-1")
    old_port = session.port
    session.proc.kill()
    session.proc.wait(timeout=5)
    recovered = service.resolve("run-1")
    assert recovered is not None
    assert recovered.state == "running", recovered.error
    assert recovered.port != old_port, "the restart reused a dead port"


def test_shutdown_stops_every_child(service, run_dir):
    session = _wait_running(service, "run-1")
    service.shutdown()
    assert not session.alive()


def test_status_is_persisted_for_the_eval_metrics(service, run_dir):
    _wait_running(service, "run-1")
    saved = json.loads((run_dir / "playground.json").read_text())
    assert saved["state"] == "running"
    assert saved["run_id"] == "run-1"
    service.stop("run-1")


def test_ever_booted_survives_a_stop(service, run_dir):
    """ "Did this app ever boot" is a fact about the run, not about one process.

    Without the sticky flag, stopping a preview rewrites playground.json as
    state=stopped and an eval reading it afterwards scores a successful boot
    as a failure.
    """
    _wait_running(service, "run-1")
    service.stop("run-1")
    saved = json.loads((run_dir / "playground.json").read_text())
    assert saved["state"] == "stopped"
    assert saved["ever_booted"] is True


def test_ever_booted_survives_a_restart(service, run_dir):
    session = _wait_running(service, "run-1")
    session.state = "crashed"
    recovered = service.start("run-1", restart=True)
    assert recovered.ever_booted is True
    service.stop("run-1")


def test_a_crashed_preview_is_not_marked_as_booted(service, runs_root):
    d = runs_root / "run-boom"
    (d / "code").mkdir(parents=True)
    (d / "code" / "main.py").write_text("import nonexistent_module_xyz\n\napp = object()\n")
    session = service.start("run-boom")
    assert session.state == "crashed"
    assert session.ever_booted is False
    assert json.loads((d / "playground.json").read_text())["ever_booted"] is False


# ---------------------------------------------------------------------- proxy ----
@pytest.fixture()
def client(runs_root, run_dir, monkeypatch):
    """A live app whose playground is the real process-wide service.

    The gateway resolves through ``get_playground()``, so injecting a separate
    instance here would test a wiring that does not exist.
    """
    from app import auth
    from app.services.playground import reset_playground

    # Auth is off by default here, but the config is cached process-wide, so it
    # has to be reset explicitly or a previously-run auth test leaks into this
    # one and every call answers 401.
    for var in ("AUTH_PASSWORD_HASH", "AUTH_SECRET", "AUTH_PASSWORD", "AUTH_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    auth.reset_auth_config()
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


def _wait_for_state(client, state, headers: dict | None = None, timeout=40):
    deadline = time.time() + timeout
    body = {}
    while time.time() < deadline:
        body = client.get("/api/runs/run-1/playground", headers=headers or {}).json()
        if body["state"] == state:
            return body
        time.sleep(0.3)
    raise AssertionError(f"preview stayed {body.get('state')}, wanted {state}: {body.get('error')}")


def test_proxy_forwards_a_get(client):
    _start(client)
    resp = client.get("/preview/run-1/items")
    assert resp.status_code == 200
    assert resp.json() == ["one", "two"]


def test_proxy_forwards_a_post_body(client):
    _start(client)
    resp = client.post("/preview/run-1/items", json={"name": "three"})
    assert resp.status_code == 200
    assert resp.json() == {"added": "three"}


def test_proxy_preserves_the_query_string(client):
    _start(client)
    assert client.get("/preview/run-1/items?limit=1").status_code == 200


def test_proxy_502s_for_an_unknown_run(client):
    resp = client.get("/preview/does-not-exist/items")
    assert resp.status_code == 502


def test_websocket_upgrade_is_refused_rather_than_hung(client):
    resp = client.get(
        "/preview/run-1/ws", headers={"upgrade": "websocket", "connection": "Upgrade"}
    )
    assert resp.status_code == 501


def test_start_endpoint_is_202_and_idempotent(client):
    first = client.post("/api/runs/run-1/playground/start")
    assert first.status_code == 202
    _wait_for_state(client, "running")
    second = client.post("/api/runs/run-1/playground/start")
    assert second.status_code == 202
    assert second.json()["port"] == client.get("/api/runs/run-1/playground").json()["port"]


def test_status_and_stop_endpoints(client):
    assert client.get("/api/runs/run-1/playground").json()["state"] == "stopped"
    client.post("/api/runs/run-1/playground/start")
    _wait_for_state(client, "running")
    assert client.post("/api/runs/run-1/playground/stop").json()["stopped"] is True
    assert client.get("/api/runs/run-1/playground").json()["state"] == "stopped"


def test_endpoints_404_for_an_unknown_run(client):
    assert client.get("/api/runs/nope/playground").status_code == 404
    assert client.post("/api/runs/nope/playground/start").status_code == 404
    assert client.post("/api/runs/nope/playground/stop").status_code == 404


# ---------------------------------------------------- gateway-served UI files ----
@pytest.fixture()
def ui_run(runs_root, run_dir):
    """The same run, plus the ui/ artifacts a full-stack run leaves behind."""
    ui = run_dir / "ui"
    ui.mkdir()
    (ui / "index.html").write_text("<html><body><div id=root></div></body></html>")
    (ui / "app.js").write_text("fetch('items');")
    (ui / "styles.css").write_text("body{}")
    return run_dir


def test_ui_page_is_served_by_the_gateway_not_the_app(client, ui_run):
    """The generated app mounts no static files, so the gateway must.

    The Developer node runs before the UI exists and never sees it, so a real
    generation cannot mount its own page. Proxying everything would 404.
    """
    resp = client.get("/preview/run-1/")
    assert resp.status_code == 200
    assert "id=root" in resp.text
    assert resp.headers["content-type"].startswith("text/html")


def test_ui_assets_resolve_from_the_same_mount(client, ui_run):
    assert client.get("/preview/run-1/app.js").text == "fetch('items');"
    assert client.get("/preview/run-1/styles.css").status_code == 200


def test_ui_is_still_proxied_when_there_is_no_match(client, ui_run):
    """The page comes from ui/, but its API calls still reach the app."""
    _start(client)
    assert client.get("/preview/run-1/items").json() == ["one", "two"]


def test_ui_serving_works_without_a_running_preview(client, ui_run):
    """Static assets are files on disk; they need no subprocess."""
    assert client.get("/preview/run-1/app.js").status_code == 200
    # ...but the API still needs one.
    assert client.get("/preview/run-1/items").status_code == 502


def test_unknown_ui_path_falls_through_to_the_proxy(client, ui_run):
    _start(client)
    assert client.get("/preview/run-1/nope.js").status_code == 404  # from the app


def test_ui_path_traversal_is_refused(client, ui_run):
    """The gateway reads files by path; it must not escape the run's ui/."""
    secret = ui_run.parent / "secret.txt"
    secret.write_text("do not serve me")
    for attempt in ("../../secret.txt", "..%2F..%2Fsecret.txt", "....//secret.txt"):
        resp = client.get(f"/preview/run-1/{attempt}")
        assert "do not serve me" not in resp.text, attempt
    assert secret.exists()


def test_ui_serving_refuses_non_text_assets(client, ui_run):
    (ui_run / "ui" / "evil.py").write_text("print('nope')")
    _start(client)
    resp = client.get("/preview/run-1/evil.py")
    assert "print('nope')" not in resp.text


# ------------------------------------------------------------------------ auth ----
@pytest.fixture()
def auth_client(runs_root, run_dir, monkeypatch):
    """A client with auth switched on, which is the deployment configuration."""
    from app import auth
    from app.services.playground import reset_playground

    monkeypatch.setenv("AUTH_PASSWORD_HASH", auth.hash_password("correct-horse"))
    monkeypatch.setenv("AUTH_SECRET", "test-secret")
    monkeypatch.setenv("AUTH_CSRF", "1")
    monkeypatch.setenv("RUNS_DIR", str(runs_root))
    auth.reset_auth_config()
    get_settings.cache_clear()
    reset_playground()
    app = create_app()
    with TestClient(app) as c:
        yield c
    reset_playground()
    auth.reset_auth_config()
    get_settings.cache_clear()


def _login(client) -> dict[str, str]:
    """Sign in and return the headers every platform call needs."""
    resp = client.post("/api/auth/login", json={"username": "admin", "password": "correct-horse"})
    assert resp.status_code == 200, resp.text
    return {"X-CSRF-Token": resp.json()["csrf_token"]}


def test_unauthenticated_preview_is_401(auth_client):
    """The proxy must never become an open relay onto whatever this host can run."""
    assert auth_client.get("/preview/run-1/items").status_code == 401


def test_unauthenticated_preview_post_is_401(auth_client):
    assert auth_client.post("/preview/run-1/items", json={"name": "x"}).status_code == 401


def test_authenticated_preview_post_needs_no_csrf_header(auth_client):
    """CSRF is exempt for /preview: the caller is sandboxed generated code.

    Without the exemption a generated app could never POST to its own API,
    because it has no way to read the platform's CSRF cookie — and the app is
    loopback-only behind a session-gated gateway anyway.
    """
    headers = _login(auth_client)
    _start(auth_client, headers)
    # Deliberately no X-CSRF-Token on this one.
    resp = auth_client.post("/preview/run-1/items", json={"name": "three"})
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"added": "three"}


def test_platform_api_still_requires_csrf(auth_client):
    """The exemption is scoped to /preview and nothing else."""
    _login(auth_client)
    assert auth_client.post("/api/runs", json={"requirement": "x" * 40}).status_code == 403


def test_authenticated_preview_get_works(auth_client):
    headers = _login(auth_client)
    _start(auth_client, headers)
    assert auth_client.get("/preview/run-1/items").json() == ["one", "two"]


# ---------------------------------------------------------------- production ----
def test_previews_are_blocked_in_production(runs_root, run_dir, monkeypatch):
    from app.services.playground import reset_playground

    monkeypatch.setenv("RUNS_DIR", str(runs_root))
    monkeypatch.setenv("ENVIRONMENT", "production")
    get_settings.cache_clear()
    reset_playground()
    app = create_app()
    with TestClient(app) as c:
        resp = c.post("/api/runs/run-1/playground/start")
        assert resp.status_code == 403
        assert "PREVIEW_ALLOW_START" in resp.json()["detail"]
    get_settings.cache_clear()
    reset_playground()
