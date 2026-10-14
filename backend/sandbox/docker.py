"""The Docker CLI transport the sandbox runner drives.

Deliberately the *only* place in the platform that knows how to talk to a
container engine. Everything above it (:mod:`sandbox.runner`,
:mod:`app.services.playground`) works in terms of "run this argv, give me the
output and the id", which is what lets Phase 5 put a two-endpoint HTTP sidecar
underneath it without a single caller changing.

The socket is not mounted into the backend in any deployed profile -- that is
root-equivalent on the host, and handing it to the component that executes
model-generated code would hand over the whole deployment. See
``SANDBOX_AND_SCALE_PLAN.md`` §3.3.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path

from sandbox.flags import DOCKER, SANDBOX_DIR, cached_image_tag

log = logging.getLogger("sandbox.docker")

BUILD_TIMEOUT = 900
RUN_TIMEOUT = 120

#: One build at a time per tag. Two fix loops starting together used to race on
#: a single shared tag and overwrite each other's image mid-run.
_build_locks: dict[str, threading.Lock] = {}
_build_locks_guard = threading.Lock()


class DockerUnavailable(RuntimeError):
    """No usable container engine. Callers fall back rather than failing a run."""


def _remote():
    """The sidecar transport, when this process must not touch a daemon itself.

    ``SANDBOX_API_URL`` is set for the backend in the deployed topology, where
    the sandbox sidecar -- and only the sidecar -- holds ``docker.sock``. Without
    this dispatch every function below would shell out to a CLI with nothing to
    talk to, ``uses_docker()`` would return False, and generated code would run
    as a plain subprocess in the backend container with the platform's
    filesystem and network: the isolation silently gone, with no error anywhere.

    Empty inside the sidecar itself, which does hold the socket and must use the
    local path -- otherwise it would call itself.
    """
    import os

    if (os.getenv("SANDBOX_API_URL", "") or "").strip():
        from sandbox import remote

        return remote
    return None


@dataclass
class ContainerResult:
    """What one ``docker run`` produced."""

    stdout: str
    stderr: str
    returncode: int
    container_id: str = ""

    @property
    def output(self) -> str:
        return self.stdout + "\n" + self.stderr


def available() -> bool:
    """Whether a container engine is reachable right now.

    Asks the sidecar when one is configured, because that is where the engine
    actually is. Probing this process instead is the bug that made the deployed
    topology quietly unisolated: the backend holds no socket, so a local probe
    answers "no engine", ``uses_docker()`` returns False, and model-generated
    code runs as a plain subprocess in the backend container with the
    platform's filesystem and network.

    The exit code alone is not the answer either. Verified on Docker CLI 26.1.5:
    ``docker info --format '{{.ServerVersion}}'`` prints ``permission denied
    while trying to connect to the Docker daemon socket`` on **stderr** and
    exits **0**, with empty stdout, when it cannot reach the daemon. Testing
    ``returncode == 0`` therefore reports a dead socket as a working engine --
    which is exactly how a sidecar with no permission to ``docker.sock`` came up
    reporting ``"engine": true``, satisfied its healthcheck, and would have
    raised ``DockerUnavailable`` on the first real request.

    So require the version string as well. An unreachable daemon is the case
    this function exists to detect, and "it printed nothing" is that case.
    """
    _r = _remote()
    if _r is not None:
        return _r.available()
    if not shutil.which(DOCKER):
        return False
    try:
        proc = subprocess.run(
            [DOCKER, "info", "--format", "{{.ServerVersion}}"],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except Exception:
        return False
    return proc.returncode == 0 and bool((proc.stdout or "").strip())


def _run(argv: list[str], timeout: int, check: bool = False) -> subprocess.CompletedProcess:
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError as exc:
        raise DockerUnavailable(str(exc)) from exc
    except subprocess.TimeoutExpired as exc:
        raise DockerUnavailable(f"{argv[0]} timed out after {timeout}s") from exc
    if check and proc.returncode != 0:
        raise DockerUnavailable((proc.stderr or proc.stdout or "").strip()[:400])
    return proc


def _lock_for(tag: str) -> threading.Lock:
    with _build_locks_guard:
        return _build_locks.setdefault(tag, threading.Lock())


def image_exists(tag: str) -> bool:
    _r = _remote()
    if _r is not None:
        return _r.image_exists(tag)
    return _run([DOCKER, "image", "inspect", tag], timeout=30).returncode == 0


def network_exists(name: str) -> bool:
    proc = _run(
        [DOCKER, "network", "inspect", "--format", "{{.Internal}}", name], timeout=15
    )
    return proc.returncode == 0


def network_is_internal(name: str) -> bool:
    _r = _remote()
    if _r is not None:
        return _r.network_is_internal(name)
    """Whether ``name`` is an ``internal: true`` network. False if it is missing.

    This one property decides how a sandbox container is reached, so it is read
    rather than assumed: an ``internal`` network cannot publish host ports, and
    pretending otherwise produces a container nothing can connect to.
    """
    proc = _run([DOCKER, "network", "inspect", "--format", "{{.Internal}}", name], timeout=15)
    return proc.returncode == 0 and proc.stdout.strip() == "true"


def ensure_network(name: str) -> None:
    _r = _remote()
    if _r is not None:
        return _r.ensure_network(name)
    """Create the sandbox network with ``internal: true`` if it is missing.

    ``internal`` is the control that gives the sandbox no route to the internet
    while leaving service-to-service traffic -- notably to the Postgres the
    generated app talks to -- working. It is stronger than ``--network none``
    for the exfiltration case, which is why the runner never passes ``none``.

    Created idempotently, and *created here* rather than assumed: in compose it
    is declared in the topology file, but locally nothing else would make it,
    and a missing network makes every sandbox run fail with a message about
    networking rather than about the code under test.

    An existing network that is not internal is an error rather than a
    redefinition: silently recreating it would break whichever containers are
    already attached.
    """
    proc = _run(
        [DOCKER, "network", "inspect", "--format", "{{.Internal}}", name], timeout=15
    )
    if proc.returncode == 0:
        if proc.stdout.strip() != "true":
            raise DockerUnavailable(
                f"docker network '{name}' exists but is not internal; it would give "
                f"sandboxed code a route to the internet. Remove it or point "
                f"SANDBOX_NETWORK at an internal network."
            )
        return
    created = _run(
        [DOCKER, "network", "create", "--internal", "--driver", "bridge", name], timeout=30
    )
    if created.returncode != 0:
        raise DockerUnavailable(
            f"could not create the internal sandbox network '{name}': "
            f"{(created.stderr or '').strip()[:200]}"
        )
    log.info("created internal sandbox network %s", name)



def ensure_image(base: str = "agent-team-sandbox:local", context: Path | None = None) -> str:
    _r = _remote()
    if _r is not None:
        return _r.ensure_image(base, context)
    """Build the sandbox image once per Dockerfile revision. Returns the tag.

    Two fixes over the previous behaviour, which was ``docker build`` on every
    test cycle against the fixed tag ``agent-team-sandbox``: the build is now
    skipped entirely when the Dockerfile and wheelhouse are unchanged, and the
    tag is derived from their hash, so concurrent runs cannot serve each other
    a half-rebuilt image.
    """
    context = context or SANDBOX_DIR
    tag = cached_image_tag(base, context)
    if image_exists(tag):
        log.debug("sandbox image %s is current", tag)
        return tag
    with _lock_for(tag):
        # Re-check under the lock: another thread may have built it while we
        # waited, and a redundant build is exactly what this is avoiding.
        if image_exists(tag):
            return tag
        log.info("building sandbox image %s", tag)
        proc = _run(
            [
                DOCKER,
                "build",
                "-t",
                tag,
                "--label",
                "org.opencontainers.image.version=" + tag.rpartition("-")[2],
                str(context),
            ],
            timeout=BUILD_TIMEOUT,
        )
        if proc.returncode != 0:
            raise DockerUnavailable(f"sandbox image build failed: {(proc.stderr or '')[-400:]}")
        return tag


def run_container(
    argv: list[str],
    timeout: int = RUN_TIMEOUT,
    mount: str | None = None,
) -> ContainerResult:
    _r = _remote()
    if _r is not None:
        return _r.run_container(argv, timeout, mount=mount)
    """``docker run`` with the full argv, returning output rather than raising.

    A container that exits non-zero is a *result* -- a failing test suite is
    exactly that -- so only an engine-level failure raises.
    """
    proc = _run(argv, timeout=timeout)
    return ContainerResult(
        stdout=proc.stdout or "",
        stderr=proc.stderr or "",
        returncode=proc.returncode,
    )


def start_detached(argv: list[str], timeout: int = 60, mount: str | None = None) -> str:
    _r = _remote()
    if _r is not None:
        return _r.start_detached(argv, timeout, mount=mount)
    """Start a long-lived container. Returns its id.

    ``docker run -d`` rather than ``docker run`` followed by a list, so the id
    comes back in the command's own output and the caller does not have to go
    looking for it. What address the container is then reachable at is a
    separate question with a separate answer -- see :func:`reachable_address` --
    because it depends on whether the network is internal.
    """
    proc = _run(argv, timeout=timeout, check=True)
    lines = (proc.stdout or "").strip().splitlines()
    container_id = lines[-1].strip() if lines else ""
    if not container_id:
        raise DockerUnavailable(f"docker run -d returned no container id: {proc.stderr[:200]}")
    return container_id



def published_port(container_id: str, container_port: int = 8000) -> int:
    _r = _remote()
    if _r is not None:
        return _r.published_port(container_id, container_port)
    """Read the host port Docker assigned to ``container_port`` inside it.

    ``NetworkSettings.Ports`` maps ``"8000/tcp"`` to a list of host bindings,
    or to ``None``/empty when nothing is published -- which is how a typo in the
    publish flag surfaces as "no port" rather than as a silent wrong answer.
    """
    proc = _run(
        [DOCKER, "inspect", "--format", "{{json .NetworkSettings.Ports}}", container_id],
        timeout=30,
        check=True,
    )
    try:
        ports = json.loads(proc.stdout.strip() or "{}")
    except json.JSONDecodeError as exc:
        raise DockerUnavailable(
            f"unreadable port map for {container_id}: {proc.stdout[:120]}"
        ) from exc
    for binding in ports.get(f"{container_port}/tcp") or []:
        port = binding.get("HostPort")
        if port:
            return int(port)
    raise DockerUnavailable(
        f"container {container_id} publishes no port for {container_port}/tcp"
    )


def container_ip(container_id: str, network: str) -> str:
    _r = _remote()
    if _r is not None:
        return _r.container_ip(container_id, network)
    """The container's IPv4 address on ``network``."""
    proc = _run(
        [DOCKER, "inspect", "--format", "{{json .NetworkSettings.Networks}}", container_id],
        timeout=30,
        check=True,
    )
    try:
        networks = json.loads(proc.stdout.strip() or "{}")
    except json.JSONDecodeError as exc:
        raise DockerUnavailable(
            f"unreadable network map for {container_id}: {proc.stdout[:120]}"
        ) from exc
    entry = networks.get(network)
    ip = entry.get("IPAddress") if isinstance(entry, dict) else None
    if not ip:
        raise DockerUnavailable(f"container {container_id} has no address on '{network}'")
    return str(ip)


def reachable_address(
    container_id: str, network: str, container_port: int = 8000
) -> tuple[str, int]:
    _r = _remote()
    if _r is not None:
        return _r.reachable_address(container_id, network, container_port)
    """``(host, port)`` from this process to a container, however it is reachable.

    Which of the two applies is not a preference -- Docker will not do both, and
    the choice is a security property, so it is derived from the network rather
    than configured:

    *On an ``internal: true`` network* (the deployed configuration) the container
    is reached **by its address on that network**, and no port is published.
    Verified on Docker 29.9: ``--publish 127.0.0.1::8000`` on an internal
    network yields ``"8000/tcp": null``, because an internal network has no
    gateway and therefore no NAT to publish through. The only thing attached to
    that network is the backend, which is exactly the intended audience -- and
    unlike a loopback publish it exposes nothing to the host at all.

    *On an ordinary bridge network* (local development, where the backend runs on
    the host rather than inside the stack) the port **is** published on loopback
    and read back from inspect. That keeps the bind-then-close race a
    ``_free_port()`` helper necessarily has out of the picture, but such a
    network has a default route and therefore *does* give the sandboxed code a
    way out. That is a real weakening and the reason the deployed profile uses
    the internal network instead.
    """
    if network_is_internal(network):
        return container_ip(container_id, network), container_port
    return "127.0.0.1", published_port(container_id, container_port)



def preview_base_url(container_id: str) -> str | None:
    """Where this process should send preview traffic for a container.

    ``None`` when the container is directly reachable (subprocess mode, or a
    backend sharing the sandbox network), which is the signal to dial the
    container's own address as before.
    """
    _r = _remote()
    if _r is not None:
        return _r.preview_base_url(container_id)
    return None


def is_running(container_id: str) -> bool:
    _r = _remote()
    if _r is not None:
        return _r.is_running(container_id)
    """Whether the container is still up. Cheap: inspect, not wait."""
    proc = _run(
        [DOCKER, "inspect", "--format", "{{.State.Running}}", container_id],
        timeout=15,
    )
    return proc.returncode == 0 and proc.stdout.strip() == "true"


def stop_container(container_id: str, timeout: int = 15) -> bool:
    _r = _remote()
    if _r is not None:
        return _r.stop_container(container_id, timeout)
    """Stop and remove. False when it was already gone.

    ``docker rm --force`` exits 0 for a container that has already exited, so
    its exit status alone cannot distinguish "stopped it" from "it was not
    there". Callers -- a TTL reaper racing a user's Stop button -- need to know
    which happened, so existence is checked first.
    """
    if not container_id:
        return False
    inspect = _run([DOCKER, "inspect", "--format", "{{.Id}}", container_id], timeout=15)
    if inspect.returncode != 0:
        return False
    proc = _run([DOCKER, "rm", "--force", "--volumes", container_id], timeout=timeout)
    if proc.returncode != 0:
        log.debug("removing %s: %s", container_id, (proc.stderr or "").strip()[:200])
        return False
    return True


def logs(container_id: str, tail: int = 40) -> str:
    _r = _remote()
    if _r is not None:
        return _r.logs(container_id, tail)
    proc = _run([DOCKER, "logs", "--tail", str(tail), container_id], timeout=15)
    return (proc.stdout or "") + (proc.stderr or "")
