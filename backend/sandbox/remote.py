"""HTTP transport for the sandbox sidecar.

The sidecar exists so the backend never holds ``docker.sock``. That is only
worth anything if the backend can actually *reach* the sidecar: :mod:`sandbox.docker`
speaks the Docker CLI, which needs a local daemon, so in the deployed topology --
where the backend has no socket -- every call fails, ``uses_docker()`` returns
False, and generated code silently runs as a plain subprocess in the backend
container with the platform's filesystem and network. The isolation is gone and
nothing says so.

This module is the other half of that trade. It exposes the same operations,
spoken over HTTP to ``SANDBOX_API_URL``, and :mod:`sandbox.docker` dispatches
here when that variable is set, so the runner and the playground keep calling
``Docker.run_container(argv)`` and neither had to change.

**Why argv is decomposed here.** The sidecar rebuilds every containment flag
from the request fields rather than accepting an argv: a backend that could
send ``--privileged`` would defeat the only reason the sidecar exists. So only
the four caller-controlled fields cross the wire -- ``image``, ``cmd``,
``mount``, ``env`` -- and they are recovered from the argv that
:func:`sandbox.flags.run_argv` produced. That is sound because we generate that
argv ourselves in a fixed shape, and it is pinned by
``test_remote_decomposes_only_the_allowlisted_fields`` so a change to
``run_argv`` cannot quietly start forwarding a flag.
"""

from __future__ import annotations

import logging
import os
import shlex
from typing import Any

import httpx

log = logging.getLogger(__name__)

#: Bounds the HTTP wait so a wedged container surfaces as a backend timeout
#: rather than a hang. The sidecar applies its own, smaller timeout per run.
DEFAULT_TIMEOUT = 900.0


def base_url() -> str:
    return (os.getenv("SANDBOX_API_URL", "") or "").strip().rstrip("/")


class RemoteUnavailable(RuntimeError):
    """The sidecar could not be reached or refused the request.

    Named separately from :class:`sandbox.docker.DockerUnavailable` so a caller
    can tell "no engine on this host" from "the service that owns the engine is
    down" -- the first is a configuration mistake, the second an outage.
    """


def _raise_for_status(resp: httpx.Response) -> dict[str, Any]:
    if resp.status_code >= 400:
        # The sidecar's `detail` is written to be read: it names the refused
        # mount, the rejected image, or the dead socket. Pass it through rather
        # than flattening it to "HTTP 422".
        try:
            detail = resp.json().get("detail") or resp.text
        except Exception:
            detail = resp.text
        raise RemoteUnavailable(f"sandbox sidecar refused the request: {detail}")
    return resp.json()


# ------------------------------------------------------------------------ argv ----
#: Flags run_argv emits that carry no caller intent and are re-derived by the
#: sidecar. Listed so that the parser can *assert* it never forwards them,
#: rather than trusting that it does not.
_DERIVED_PREFIXES = (
    "--network",
    "--read-only",
    "--cap-drop",
    "--security-opt",
    "--pids-limit",
    "--memory",
    "--memory-swap",
    "--cpus",
    "--cpuset-cpus",
    "--user",
    "--tmpfs",
    "--workdir",
    "--entrypoint",
    "--publish",
    "--detach",
    "--rm",
)


def decompose(argv: list[str]) -> dict[str, Any]:
    """Recover the four allowlisted fields from a ``run_argv`` list.

    Returns ``{image, cmd, mount, env, name, with_data}``. The security flags
    are deliberately dropped: the sidecar recomputes them from its own limits.
    """
    argv = list(argv)
    if not argv or argv[0].endswith("docker"):
        argv = argv[1:]
    # A leading `run` (and `--rm`) belong to the engine, not the request.
    while argv and argv[0] in {"run", "--rm"}:
        argv.pop(0)

    mount: str | None = None
    mount_volume: str | None = None
    workdir: str | None = None
    env: dict[str, str] = {}
    name: str | None = None
    with_data = False
    rest: list[str] = []

    i = 0
    while i < len(argv):
        token = argv[i]
        if token == "--name" and i + 1 < len(argv):
            name = argv[i + 1]
            i += 2
        elif token == "-v" and i + 1 < len(argv):
            # `source:target[:ro]`. A source that is an absolute path is a host
            # bind; anything else is a named volume. That distinction is not
            # cosmetic -- it decides whether the sidecar can mount the data at
            # all, since the daemon resolves host paths on the host and the
            # requester is itself a container. The target doubles as the
            # workdir, because mounting a volume at a point and then asking for
            # a subdirectory is how a bundle inside a volume gets selected.
            parts = argv[i + 1].rsplit(":", 2)
            source = parts[0] if parts else ""
            if source.startswith("/"):
                mount = source
            else:
                # A volume is mounted at its own target and the workdir is set
                # *inside* it by --workdir, so the target is not the workdir
                # here. Taking it as one would make "run this bundle" mean "run
                # the root of the volume", collecting every other run's tests.
                mount_volume = source
            i += 2
        elif token == "-e" and i + 1 < len(argv):
            key, _, value = argv[i + 1].partition("=")
            env[key] = value
            i += 2
        elif token == "--tmpfs" and i + 1 < len(argv):
            with_data = True  # the writable /data mount, not the read-only one
            i += 2
        elif token == "--workdir" and i + 1 < len(argv):
            # An explicit workdir (the volume case) is the bundle's directory
            # inside the mount and must survive; the default one is re-derived.
            workdir = argv[i + 1]
            i += 2
        elif token.startswith(_DERIVED_PREFIXES):
            # Skip the flag and its value if the next token is not a flag.
            i += 2 if i + 1 < len(argv) and not argv[i + 1].startswith("-") else 1
        else:
            rest.append(token)
            i += 1

    if not rest:
        raise RemoteUnavailable("could not find the image and command in the sandbox argv")
    return {
        "image": rest[0],
        "cmd": rest[1:],
        "mount": mount,
        "mount_volume": mount_volume,
        "workdir": workdir,
        "env": env,
        "name": name,
        "with_data": with_data,
    }


# ------------------------------------------------------------------------ calls ----
def _post(payload: dict[str, Any], timeout: float | None = None) -> dict[str, Any]:
    url = base_url()
    if not url:
        raise RemoteUnavailable("SANDBOX_API_URL is not set")
    try:
        with httpx.Client(base_url=url, timeout=timeout or DEFAULT_TIMEOUT) as client:
            return _raise_for_status(client.post("/sandbox/runs", json=payload))
    except httpx.HTTPError as exc:
        raise RemoteUnavailable(f"cannot reach the sandbox sidecar at {url}: {exc}") from exc


def available() -> bool:
    """Whether the sidecar answers *and* reports a working engine.

    Both halves matter. A sidecar that is up but cannot reach the daemon is the
    exact state that previously reported ``"engine": true`` on a dead socket, and
    the backend would discover it on the first real run rather than at startup.
    """
    url = base_url()
    if not url:
        return False
    try:
        with httpx.Client(base_url=url, timeout=15) as client:
            body = client.get("/health").json()
    except Exception:
        return False
    return bool(body.get("ok")) and bool(body.get("engine"))


def _noop(*_args: Any, **_kwargs: Any) -> Any:
    """For operations the sidecar performs itself.

    Image builds and network creation happen inside ``create_run`` because the
    sidecar owns the socket. Forwarding a build request would be asking the
    component that is trusted to refuse things to do extra work on request.
    """
    return None


def run_container(
    argv: list[str], timeout: int = 120, mount: str | None = None
) -> Any:
    from .docker import ContainerResult

    payload = decompose(argv)
    # The caller states the mount rather than it being recovered from argv.
    # pytest now runs with its cwd on the writable tmpfs, so the workdir no
    # longer names the bundle -- and a mount the sidecar cannot check is a mount
    # it cannot confine. Naming it here keeps `_resolve_mount` in the path.
    if mount:
        payload["mount"] = mount
    body = _post({**payload, "detach": False, "timeout": timeout}, timeout=timeout + 60)
    return ContainerResult(
        returncode=int(body.get("returncode", -1)),
        stdout=body.get("stdout", ""),
        stderr=body.get("stderr", ""),
    )


def start_detached(argv: list[str], timeout: int = 60, mount: str | None = None) -> str:
    payload = decompose(argv)
    if mount:
        payload["mount"] = mount
    body = _post({**payload, "detach": True}, timeout=timeout + 60)
    cid = body.get("id") or ""
    if not cid:
        raise RemoteUnavailable("sidecar started no container and returned no id")
    return cid


def reachable_address(
    container_id: str, network: str, container_port: int = 8000
) -> tuple[str, int]:
    body = get_run(container_id)
    host = body.get("host")
    if not host:
        raise RemoteUnavailable(f"sidecar has no address for container {container_id[:12]}")
    return str(host), int(body.get("port") or container_port)


def container_ip(container_id: str, network: str) -> str:
    return reachable_address(container_id, network)[0]


def published_port(container_id: str, container_port: int = 8000) -> int:
    return reachable_address(container_id, "", container_port)[1]


def get_run(container_id: str) -> dict[str, Any]:
    url = base_url()
    try:
        with httpx.Client(base_url=url, timeout=30) as client:
            return _raise_for_status(client.get(f"/sandbox/runs/{container_id}"))
    except httpx.HTTPError as exc:
        raise RemoteUnavailable(f"cannot reach the sandbox sidecar: {exc}") from exc


def is_running(container_id: str) -> bool:
    try:
        return bool(get_run(container_id).get("running"))
    except Exception:
        # A container the sidecar cannot describe is not one to report as alive;
        # callers treat False as stopped and clean up.
        return False


def stop_container(container_id: str, timeout: int = 15) -> bool:
    url = base_url()
    try:
        with httpx.Client(base_url=url, timeout=timeout + 30) as client:
            body = _raise_for_status(client.delete(f"/sandbox/runs/{container_id}"))
    except Exception as exc:
        log.warning("could not stop container %s via the sidecar: %s", container_id[:12], exc)
        return False
    return bool(body.get("stopped"))


def logs(container_id: str, tail: int = 40) -> str:
    url = base_url()
    try:
        with httpx.Client(base_url=url, timeout=30) as client:
            body = _raise_for_status(
                client.get(f"/sandbox/logs/{container_id}", params={"lines": tail})
            )
    except Exception as exc:
        return f"(no logs: {exc})"
    return body.get("logs", "")


def network_is_internal(name: str) -> bool:
    """The internal/bridge decision decides how the backend dials a container.

    The sidecar is the only process that can inspect the network, and it is the
    one that created it, so ask rather than assume.
    """
    url = base_url()
    try:
        with httpx.Client(base_url=url, timeout=30) as client:
            body = _raise_for_status(client.get("/sandbox/network", params={"name": name}))
    except Exception as exc:
        # Unknown is not the same as "not internal": reporting False would send
        # the backend down the published-port path, which does not exist on an
        # internal network.
        raise RemoteUnavailable(f"sidecar could not describe network {name!r}: {exc}") from exc
    return bool(body.get("internal"))


def image_exists(tag: str) -> bool:
    return bool(available())


def ensure_image(base: str, context: Any = None) -> str:
    return base


def ensure_network(name: str) -> None:
    return None


__all__ = [
    "RemoteUnavailable",
    "available",
    "container_ip",
    "decompose",
    "ensure_image",
    "ensure_network",
    "get_run",
    "image_exists",
    "is_running",
    "logs",
    "network_is_internal",
    "published_port",
    "reachable_address",
    "run_container",
    "shlex",
    "start_detached",
    "stop_container",
]


def preview_base_url(container_id: str) -> str | None:
    """The URL the backend should dial to reach a sandbox container.

    Not the container's own address: the backend is not on ``sandbox-net``, so
    that address has no route from here. The sidecar is on both networks and is
    the only component that can reach both ends, so the gateway dials it and it
    relays. See :func:`sandbox.sidecar.proxy`.
    """
    url = base_url()
    if not url:
        return None
    return f"{url}/sandbox/proxy/{container_id}"
