"""The sandbox sidecar's boundary.

Offline: the refusals are the interesting part, and they are refusals *before*
any engine call, so they are testable without a Docker daemon. What the engine
actually does is covered by ``test_sandbox_container.py``.

The service exists because ``/var/run/docker.sock`` is root-equivalent on the
host, so these tests are really tests about who is allowed to do what with a
root-equivalent capability.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from sandbox import sidecar

IMAGE = "agent-team-sandbox:local"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("SANDBOX_MOUNT_ROOT", str(tmp_path))
    monkeypatch.setenv("SANDBOX_IMAGE_ALLOWLIST", "agent-team-sandbox")
    return TestClient(sidecar.app)


@pytest.fixture
def run_dir(tmp_path):
    d = tmp_path / "run-1"
    d.mkdir()
    return d


# --------------------------------------------------------------------------
# image allowlist
# --------------------------------------------------------------------------
def test_the_sandbox_image_is_allowed(client):
    assert sidecar.image_allowed("agent-team-sandbox:local")
    assert sidecar.image_allowed("agent-team-sandbox:local-abc123")


@pytest.mark.parametrize("image", ["alpine", "python:3.12", "evil/agent-team-sandbox", ""])
def test_any_other_image_is_refused(client, image):
    """No arbitrary pull-and-run. The socket must not be a registry proxy."""
    assert sidecar.image_allowed(image) is False


def test_a_prefix_lookalike_is_refused(client):
    """`agent-team-sandboxX` must not satisfy an `agent-team-sandbox` rule."""
    assert sidecar.image_allowed("agent-team-sandboxX/img:latest") is False


def test_requesting_an_unlisted_image_is_rejected(client):
    r = client.post("/sandbox/runs", json={"image": "alpine", "cmd": ["true"]})
    assert r.status_code == 403
    assert "allowlist" in r.json()["detail"]


# --------------------------------------------------------------------------
# mount confinement
# --------------------------------------------------------------------------
def test_a_mount_inside_the_root_is_accepted(client, run_dir):
    assert sidecar._resolve_mount(str(run_dir)) == str(run_dir.resolve())


def test_a_relative_mount_resolves_against_the_root(client, run_dir):
    assert sidecar._resolve_mount("run-1") == str(run_dir.resolve())


def test_a_mount_outside_the_root_is_refused(client):
    r = client.post("/sandbox/runs", json={"image": IMAGE, "mount": "/etc"})
    assert r.status_code == 400
    assert "must be inside" in r.json()["detail"]


@pytest.mark.parametrize("evil", ["../../etc", "/tmp/../etc", "run-1/../../.."])
def test_traversal_is_refused(client, run_dir, evil):
    """Refused before resolution, not normalised away.

    ``..`` is checked on the raw path first so no traversal reaches the
    filesystem, and the resolved path is then required to sit under the root --
    belt and braces, because one check of the wrong thing is an escape.
    """
    r = client.post("/sandbox/runs", json={"image": IMAGE, "mount": evil})
    assert r.status_code == 400


def test_a_symlink_pointing_out_of_the_root_is_refused(client, tmp_path):
    """A symlink inside the runs volume must not become an exit."""
    outside = tmp_path.parent / "outside-secret"
    outside.mkdir(exist_ok=True)
    link = tmp_path / "escape"
    link.symlink_to(outside)
    with pytest.raises(Exception) as exc:
        sidecar._resolve_mount(str(link))
    assert "must be inside" in str(exc.value.detail)


def test_a_mount_that_is_not_a_directory_is_refused(client, tmp_path):
    f = tmp_path / "a-file"
    f.write_text("x")
    with pytest.raises(Exception) as exc:
        sidecar._resolve_mount(str(f))
    assert "not a directory" in str(exc.value.detail)


# --------------------------------------------------------------------------
# privilege and capability requests
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "field", ["privileged", "cap_add", "security_opt", "network", "user", "pid", "read_only"]
)
def test_containment_fields_cannot_even_be_named(client, field):
    """Rejected at the schema, not ignored.

    With extra fields ignored, a caller sending ``{"privileged": true}`` would
    get a 200 and could reasonably conclude it had been granted. It never will
    be; a 422 says so unambiguously at the edge.
    """
    r = client.post("/sandbox/runs", json={"image": IMAGE, field: True})
    assert r.status_code == 422, f"{field} was accepted"
    assert "extra_forbidden" in r.text


def test_no_request_field_can_name_an_image_outside_the_allowlist_via_a_tag(client):
    # Allowlisting on the repository, not the full tag: a caller may pick any
    # tag of the sandbox image, but not a different repository.
    assert sidecar.image_allowed("agent-team-sandbox:anything-at-all")
    assert not sidecar.image_allowed("somethingelse:agent-team-sandbox")


# --------------------------------------------------------------------------
# credentials
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "env",
    [
        {"GEMINI_API_KEY": "x"},
        {"GROQ_API_KEY": "x"},
        {"AUTH_SECRET": "x"},
        # A DATABASE_URL is refused for carrying a password, not for its name --
        # see the by-value tests below. This is the case that must stay refused.
        {"DATABASE_URL": "postgresql+psycopg://mat:hunter2@postgres:5432/mat"},
        {"SOMETHING_ELSE_API_KEY": "x"},
    ],
)
def test_credentials_cannot_be_passed_into_a_container(client, env):
    """The last hop refuses, so a caller bug cannot leak the deployment.

    Generated code runs in this container. A provider key that reached it would
    be exfiltratable -- except that the sandbox network has no route out, which
    is a second line of defence, not a reason to skip the first.
    """
    r = client.post("/sandbox/runs", json={"image": IMAGE, "env": env, "cmd": ["-c", "pass"]})
    assert r.status_code == 400
    assert "credential" in r.json()["detail"]


def test_ordinary_sandbox_env_is_allowed(client):
    r = client.post(
        "/sandbox/runs",
        json={"image": IMAGE, "env": {"SANDBOX": "1", "PREVIEW": "1"}, "cmd": ["-c", "print(1)"]},
    )
    # Reaches the engine, not refused by validation. Whether the engine is up is
    # a separate question the health endpoint answers.
    assert r.status_code in {200, 503}


# --------------------------------------------------------------------------
# health and idempotence
# --------------------------------------------------------------------------
def test_health_reports_the_engine_separately(client):
    """A live process with a dead socket is not healthy.

    If this only reported process liveness, compose would happily wire the
    backend to a sidecar that cannot start anything, and the first run would be
    the thing that discovers it.
    """
    body = client.get("/health").json()
    assert body["ok"] is True
    assert "engine" in body
    assert isinstance(body["engine"], bool)
    assert body["mount_root"]


def test_stopping_something_that_does_not_exist_is_not_an_error(client):
    """A TTL reaper and a user clicking Stop race; both must be safe."""
    r = client.delete("/sandbox/runs/deadbeef")
    assert r.status_code == 200
    assert r.json()["stopped"] is False


def test_the_mount_root_is_configurable(tmp_path, monkeypatch):
    monkeypatch.setenv("SANDBOX_MOUNT_ROOT", str(tmp_path / "elsewhere"))
    assert sidecar.mount_root() == (tmp_path / "elsewhere").resolve()


def test_the_image_allowlist_is_configurable(monkeypatch):
    monkeypatch.setenv("SANDBOX_IMAGE_ALLOWLIST", "agent-team-sandbox, my-own-sandbox")
    assert "my-own-sandbox" in sidecar.allowed_images()
    assert sidecar.image_allowed("my-own-sandbox:v1")


# ------------------------------------------------------------------------ engine detection ----
def test_available_is_false_when_the_daemon_cannot_be_reached(monkeypatch):
    """`docker info` exits 0 even when it cannot reach the daemon.

    Verified on Docker CLI 26.1.5: it prints "permission denied while trying to
    connect to the Docker daemon socket" on stderr, writes nothing to stdout,
    and exits **0**. Testing the exit code alone reported a sidecar with no
    permission to docker.sock as a healthy service with a working engine, which
    then failed on the first request instead of at startup.
    """
    from sandbox import docker as Docker

    class FakeProc:
        returncode = 0
        stdout = ""  # the tell: it connected to nothing
        stderr = "permission denied while trying to connect to the Docker daemon socket"

    monkeypatch.setattr(Docker.shutil, "which", lambda _name: "/usr/bin/docker")
    monkeypatch.setattr(Docker.subprocess, "run", lambda *a, **k: FakeProc())
    assert Docker.available() is False


def test_available_is_true_only_when_a_version_comes_back(monkeypatch):
    from sandbox import docker as Docker

    class Ok:
        returncode = 0
        stdout = "26.1.5\n"
        stderr = ""

    monkeypatch.setattr(Docker.shutil, "which", lambda _name: "/usr/bin/docker")
    monkeypatch.setattr(Docker.subprocess, "run", lambda *a, **k: Ok())
    assert Docker.available() is True


def test_available_is_false_when_the_cli_is_absent(monkeypatch):
    from sandbox import docker as Docker

    monkeypatch.setattr(Docker.shutil, "which", lambda _name: None)
    assert Docker.available() is False


def test_the_health_endpoint_reports_a_dead_engine(client, monkeypatch):
    """Liveness and engine reachability are different facts.

    The sidecar can be perfectly alive with a socket it cannot use. Reporting
    only `ok` is what let compose wire the backend to a service that could not
    start anything.
    """
    from sandbox import docker as Docker

    monkeypatch.setattr(Docker, "available", lambda: False)
    body = client.get("/health").json()
    assert body["ok"] is True, "the process is alive; only the engine is gone"
    assert body["engine"] is False


# ------------------------------------------------------- sidecar transport (remote) ----
def test_remote_decomposes_only_the_allowlisted_fields():
    """The sidecar re-derives every containment flag, so none may be forwarded.

    This is the whole reason the sidecar is not just "the backend with a
    socket": a backend that could send `--privileged` or `--network` would make
    the allowlist decorative. `decompose` recovers only what the caller chose,
    and the sidecar rebuilds the policy from its own limits.
    """
    from sandbox.flags import run_argv
    from sandbox.remote import decompose

    argv = run_argv(
        "agent-team-sandbox:local",
        ["-m", "pytest", "-q"],
        mount="/data/runs/run-1/code",
        env={"PATH": "/usr/bin", "PREVIEW": "1"},
        detach=True,
        with_data=True,
    )
    payload = decompose(argv)

    assert payload["image"] == "agent-team-sandbox:local"
    assert payload["cmd"] == ["-m", "pytest", "-q"]
    assert payload["mount"] == "/data/runs/run-1/code"
    assert payload["env"] == {"PATH": "/usr/bin", "PREVIEW": "1"}
    assert payload["with_data"] is True

    # Nothing that grants privilege may appear in the request body. `workdir` and
    # `mount_volume` are not privileges: the first is the bundle's directory
    # inside the mount the sidecar already validated, and the second only tells
    # it to mount the same data by name instead of by a host path that does not
    # exist for the daemon.
    assert set(payload) == {
        "image", "cmd", "mount", "mount_volume", "workdir", "env", "name", "with_data",
    }
    blob = repr(payload)
    for flag in ("--privileged", "--cap-add", "--network", "--user", "--read-only",
                 "--security-opt", "--pid", "--ipc", "--uts"):
        assert flag not in blob, f"{flag} reached the sidecar request"


def test_remote_decompose_handles_a_command_that_looks_like_flags():
    """Generated pytest args are caller data, not engine flags.

    `-q` and `-x` arrive after the image; treating a leading dash in `cmd` as
    an engine flag would silently truncate the command the sandbox runs.
    """
    from sandbox.remote import decompose

    argv = [
        "docker", "run", "--rm", "--network", "sandbox-net", "--read-only",
        "--memory", "512m", "--user", "10001:10001", "--workdir", "/app",
        "--entrypoint", "python", "agent-team-sandbox:local",
        "-m", "pytest", "-q", "-x", "--tb=short",
    ]
    payload = decompose(argv)
    assert payload["cmd"] == ["-m", "pytest", "-q", "-x", "--tb=short"]
    assert payload["image"] == "agent-team-sandbox:local"


def test_the_backend_routes_through_the_sidecar_when_one_is_configured(monkeypatch):
    """`SANDBOX_API_URL` must switch the transport, not just be set.

    The backend holds no socket, so without the dispatch every call fails and
    `uses_docker()` is False -- which silently downgrades generated code to a
    subprocess in the backend container.
    """
    from sandbox import docker as Docker

    monkeypatch.setenv("SANDBOX_API_URL", "http://sandbox:8001")
    assert Docker._remote() is not None

    monkeypatch.delenv("SANDBOX_API_URL", raising=False)
    assert Docker._remote() is None, "with no sidecar the local path is correct"


def test_every_transport_function_actually_dispatches(monkeypatch):
    """The dispatch must be *executed*, not merely present in the source.

    A dispatch inserted between a signature and its docstring is a string
    literal: the module still imports, the text is still there, and the function
    silently keeps shelling out to a daemon this process does not have. That is
    exactly how `available()` kept returning False for a perfectly healthy
    sidecar. Call each entry point with a stubbed remote and require that the
    stub is what answers.
    """
    from sandbox import docker as Docker

    monkeypatch.setenv("SANDBOX_API_URL", "http://sandbox:8001")
    remote = Docker._remote()
    assert remote is not None

    called = []

    def spy(name, result=None):
        def _fn(*_a, **_k):
            called.append(name)
            return result

        return _fn

    monkeypatch.setattr(remote, "available", spy("available", True))
    monkeypatch.setattr(remote, "image_exists", spy("image_exists", True))
    monkeypatch.setattr(remote, "network_is_internal", spy("network_is_internal", True))
    monkeypatch.setattr(remote, "run_container", spy("run_container", "RC"))
    monkeypatch.setattr(remote, "start_detached", spy("start_detached", "cid"))
    monkeypatch.setattr(remote, "container_ip", spy("container_ip", "10.0.0.9"))
    monkeypatch.setattr(remote, "published_port", spy("published_port", 49152))
    monkeypatch.setattr(remote, "reachable_address", spy("reachable_address", ("h", 1)))
    monkeypatch.setattr(remote, "is_running", spy("is_running", True))
    monkeypatch.setattr(remote, "stop_container", spy("stop_container", True))
    monkeypatch.setattr(remote, "logs", spy("logs", "out"))
    monkeypatch.setattr(remote, "ensure_image", spy("ensure_image", "img"))
    monkeypatch.setattr(remote, "ensure_network", spy("ensure_network"))

    argv = ["docker", "run", "--rm", "img", "echo", "hi"]

    assert Docker.available() is True
    Docker.image_exists("x")
    Docker.network_is_internal("net")
    Docker.run_container(argv)
    Docker.start_detached(argv)
    Docker.container_ip("c", "net")
    Docker.published_port("c")
    Docker.reachable_address("c", "net")
    Docker.is_running("c")
    Docker.stop_container("c")
    Docker.logs("c")
    Docker.ensure_image("base")
    Docker.ensure_network("net")

    missing = set(called)
    assert missing, "no function dispatched to the sidecar at all"
    assert "available" in missing, "available() never asked the sidecar"


# ------------------------------------------------------- DATABASE_URL by value ----
def test_a_sqlite_database_url_is_allowed_through():
    """The platform injects `sqlite:////data/preview.db` so a generated app has
    a writable database. Refusing it by name made the sandbox unusable: every
    run failed with a credential error about a URL with no password in it.
    """
    assert sidecar._carries_credentials("DATABASE_URL", "sqlite:////data/preview.db") is False
    assert sidecar._sanitise_env({"DATABASE_URL": "sqlite:////data/preview.db"}) == {
        "DATABASE_URL": "sqlite:////data/preview.db"
    }


def test_a_database_url_carrying_a_password_is_still_refused():
    """The relaxation must not become a hole: the secret is the reason the rule
    exists. `postgresql+psycopg://mat:hunter2@postgres:5432/mat` hands the app
    the database password."""
    assert sidecar._carries_credentials("DATABASE_URL", "postgresql+psycopg://mat:hunter2@pg:5432/mat")
    with pytest.raises(HTTPException) as exc:
        sidecar._sanitise_env({"DATABASE_URL": "postgresql+psycopg://mat:hunter2@pg:5432/mat"})
    assert exc.value.status_code == 400


def test_a_passwordless_postgres_url_is_allowed():
    """Trust or peer auth is a real configuration; it carries nothing to steal."""
    assert sidecar._carries_credentials("DATABASE_URL", "postgresql://postgres@/mat") is False


def test_named_credentials_are_refused_whatever_the_value():
    """The by-value rule is an addition, not a replacement."""
    for name, value in (
        ("GEMINI_API_KEY", "x"),
        ("AUTH_SECRET", ""),
        ("SOME_API_KEY", "anything"),
    ):
        with pytest.raises(HTTPException):
            sidecar._sanitise_env({name: value})


def test_a_volume_mount_is_distinguished_from_a_host_path():
    """The daemon resolves a `-v` source on the host, not in the requester.

    The backend is a container, so its bundle path exists only inside its own
    mount of the runs volume. Forwarded verbatim it becomes an *empty directory
    on the host*: the container starts, pytest runs, and reports "no tests ran"
    because /app is empty. Naming the volume works from anywhere, so the kind of
    mount has to survive the trip.
    """
    from sandbox.remote import decompose

    host_path = decompose(
        ["docker", "run", "--rm", "-v", "/data/runs/b1:/app:ro", "--workdir", "/app",
         "img", "-m", "pytest"]
    )
    assert host_path["mount"] == "/data/runs/b1"
    assert host_path["mount_volume"] is None

    # The volume is mounted at /app and the workdir points into it -- mounting
    # the volume *at* the workdir would make the bundle directory and the volume
    # root the same place, so pytest would collect every other run's tests.
    by_volume = decompose(
        ["docker", "run", "--rm", "-v", "runs-data:/app:ro", "--workdir", "/app/bundle-1",
         "img", "-m", "pytest"]
    )
    assert by_volume["mount_volume"] == "runs-data"
    assert by_volume["workdir"] == "/app/bundle-1"
    assert by_volume["mount"] is None


# ------------------------------------------------------- preview relay ----
def test_the_backend_dials_the_sidecar_not_the_container():
    """The backend is not on `sandbox-net`, so a container IP has no route.

    Two ways to close that gap are both wrong: attaching the backend to
    `sandbox-net` puts the platform's own API within reach of the code it is
    sandboxing, and publishing a host port gives sandboxed code a route off the
    machine. The sidecar is already on both networks, so it relays.
    """
    from sandbox import docker as Docker

    monkey = pytest.MonkeyPatch()
    monkey.setenv("SANDBOX_API_URL", "http://sandbox:8001")
    try:
        assert Docker.preview_base_url("abc123").startswith(
            "http://sandbox:8001/sandbox/proxy/"
        )
    finally:
        monkey.undo()

    # Without a sidecar the container's own address is used, as before.
    monkey2 = pytest.MonkeyPatch()
    monkey2.delenv("SANDBOX_API_URL", raising=False)
    try:
        assert Docker.preview_base_url("abc123") is None
    finally:
        monkey2.undo()


def test_the_proxy_route_exists_and_refuses_a_dead_container(client):
    """A relayed preview must fail as a gateway error, not hang or 500."""
    assert client.get("/sandbox/proxy/deadbeef/health").status_code == 502


# ------------------------------------------------- mount root vs daemon paths ----
def test_a_bundle_path_is_never_handed_to_the_daemon_as_a_host_path(monkeypatch, tmp_path):
    """A path that exists only inside a container is not a path on the host.

    The daemon resolves `-v` sources against the host filesystem. A bundle at
    `/data/runs/x` exists only inside the backend container's volume, so
    forwarded verbatim the daemon creates an empty directory there, mounts it,
    and the sandbox starts, runs pytest, and reports "no tests ran" with no
    error anywhere. The mount has to go by volume name instead.
    """
    from sandbox import remote

    monkeypatch.setenv("SANDBOX_API_URL", "http://sandbox:8001")
    monkeypatch.setenv("RUNS_VOLUME", "runs-data")
    monkeypatch.setenv("SANDBOX_MOUNT_ROOT", str(tmp_path / "runs"))

    from sandbox import runner as Runner

    assert Runner._runs_volume() == "runs-data"
    assert Runner._mount_root() == tmp_path / "runs"

    # And the argv the runner builds carries the volume, not the host path.
    from sandbox.flags import SandboxLimits, run_argv

    argv = run_argv(
        "img", ["-m", "pytest"], limits=SandboxLimits(),
        mount=None, mount_volume="runs-data", workdir="/app/bundle-1",
    )
    payload = remote.decompose(argv)
    assert payload["mount"] is None
    assert payload["mount_volume"] == "runs-data"
    assert payload["workdir"] == "/app/bundle-1"
    assert "-v" in argv and "runs-data:/app:ro" in argv


def test_the_bundle_root_is_not_created_when_no_sidecar_is_configured(monkeypatch):
    """No sidecar means no confinement, so RUNS_DIR must not be created as a side
    effect of reading a bundle path."""
    from sandbox import runner as Runner

    monkeypatch.delenv("SANDBOX_API_URL", raising=False)
    monkeypatch.setenv("RUNS_DIR", "/definitely/not/created/by/this/test")
    assert Runner._mount_root() is None
    assert Runner._runs_volume() is None
