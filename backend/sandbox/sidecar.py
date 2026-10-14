"""The sandbox sidecar: the only service that holds the Docker socket.

Why this exists
---------------
Mounting ``/var/run/docker.sock`` into the backend would make that container
**root-equivalent on the host**: anyone who can reach that socket can start a
container with the host filesystem mounted. The backend is the component that
runs model-generated code, so handing it the socket would hand over the entire
deployment -- which is precisely the boundary this service was built to create.

So the socket lives here, in a service that does nothing else, behind an API
with two endpoints:

    POST   /sandbox/runs    {image, cmd, env, mount, ...} -> {id, host, port}
    DELETE /sandbox/runs/{id}

What it deliberately refuses
----------------------------
* **Arbitrary images.** Only names on ``SANDBOX_IMAGE_ALLOWLIST`` (the sandbox
  image built from ``backend/sandbox``) are startable. Without this the socket
  is a pull-and-run primitive for anything on a registry.
* **Privileged mode.** Never, under any request.
* **Host paths.** ``mount`` must resolve inside ``SANDBOX_MOUNT_ROOT``, the runs
  volume. A path that escapes it is refused rather than normalised, because
  ``..`` in a mount is a root-equivalent escape and a normalisation bug is an
  escape.
* **Capability or privilege changes.** The containment flag set is fixed in
  :mod:`sandbox.flags` and is not overridable from a request.

The net effect is that this service is strictly smaller than the socket mount it
replaces: two verbs, one image, one directory tree.

It re-derives the flags rather than accepting them, so a caller cannot weaken
the container by asking nicely.
"""

from __future__ import annotations

import json
import logging
import os
import re
import socket
import time
from pathlib import Path, PurePosixPath
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field

from sandbox import docker as Docker
from sandbox.flags import DOCKER, SANDBOX_DIR, SandboxLimits, run_argv

log = logging.getLogger("sandbox.sidecar")

app = FastAPI(title="sandbox sidecar", docs_url=None, redoc_url=None, openapi_url=None)


def _env(name: str, default: str) -> str:
    return (os.getenv(name, "") or default).strip()


#: Images this service will start. Anything not listed is refused, so the
#: sidecar cannot be used as a pull-and-run primitive for arbitrary code.
DEFAULT_ALLOWLIST = ("agent-team-sandbox",)


def allowed_images() -> tuple[str, ...]:
    raw = _env("SANDBOX_IMAGE_ALLOWLIST", ",".join(DEFAULT_ALLOWLIST))
    return tuple(p.strip() for p in raw.split(",") if p.strip())


def image_allowed(image: str) -> bool:
    """Whether an image (or a repository, ignoring the tag) may be started."""
    if not image:
        return False
    name = image.split("@", 1)[0].rsplit(":", 1)[0]
    return any(name == a or name.startswith(f"{a}/") or name.startswith(f"{a}:") for a in allowed_images())


def mount_root() -> Path:
    return Path(_env("SANDBOX_MOUNT_ROOT", "/data/runs")).resolve()


#: Platform credentials. Any request naming one of these is refused: this
#: service is the boundary between the deployment and code it did not write.
CREDENTIAL_ENV = frozenset(
    {
        "GEMINI_API_KEY",
        "GROQ_API_KEY",
        "OPENAI_API_KEY",
        "OPENROUTER_API_KEY",
        "HF_TOKEN",
        "HUGGINGFACE_API_TOKEN",
        "GITHUB_TOKEN",
        "GH_TOKEN",
        "AUTH_SECRET",
        "AUTH_PASSWORD_HASH",
        "AUTH_PASSWORD",
        "AUTH_API_KEY",
    }
)

#: ``DATABASE_URL`` is handled by value rather than by name. It is the one entry
#: that is sometimes a secret and sometimes not: the platform injects
#: ``sqlite:////data/preview.db`` so a generated app has a writable database
#: inside the sandbox, and refusing that by name makes the sandbox unusable --
#: every run fails with a credential error about a URL with no password in it.
#:
#: What must never cross is a DSN that carries a password. Postgres syntax is
#: ``scheme://user:password@host/db``, so the presence of credentials in the
#: authority is what is checked. Refusing by name alone would also have hidden
#: the real bug this replaced: a DSN *with* a password still fails.
CREDENTIAL_BY_VALUE = frozenset({"DATABASE_URL"})

#: Headers that describe a single hop and must not be relayed onward.
_HOP_BY_HOP = frozenset(
    {"connection", "keep-alive", "transfer-encoding", "upgrade", "te", "trailer", "proxy-authenticate"}
)


def _carries_credentials(name: str, value: str) -> bool:
    """Whether one environment entry is a platform secret in disguise."""
    if name not in CREDENTIAL_BY_VALUE:
        return False
    return bool(_DSN_CREDENTIALS.search(value))


#: ``user:password@`` in a DSN authority. Deliberately narrow: it matches the
#: shape of a credential, not the presence of an ``@`` (which a Unix-socket or
#: query-parameter DSN may contain legitimately).
_DSN_CREDENTIALS = re.compile(r"://[^/\s:@]+:[^/\s@]+@")


class RunRequest(BaseModel):
    """A request to start one sandbox container.

    Every containment flag is absent by design. They are not "defaults that can
    be overridden" -- they are not part of the request at all, so no caller can
    ask for a weaker container.

    ``extra="forbid"`` is what makes that real rather than merely intended. With
    extra fields ignored, a caller sending ``{"privileged": true}`` would get a
    200 and could reasonably believe it had been granted. It was not, and it
    never will be, but a refusal at the edge says so unambiguously.
    """

    model_config = ConfigDict(extra="forbid")

    image: str
    cmd: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    mount: str | None = None
    name: str | None = None
    detach: bool = False
    with_data: bool = False
    timeout: int = Field(default=120, ge=1, le=3600)
    #: Set by the backend's transport when it knows the runs data is a named
    #: volume rather than a host directory. The sidecar still confines `mount`
    #: to its mount root -- this only changes how the *already-validated* path
    #: is handed to the daemon.
    mount_volume: str | None = None
    #: The bundle's directory *inside* the mount, when mounting by volume. Not
    #: a privilege and not trusted: `create_run` recomputes it from the
    #: confined `mount` and ignores this field, because a caller-supplied
    #: workdir would be a way to read outside the directory it was granted.
    workdir: str | None = None


class DeleteResponse(BaseModel):
    stopped: bool
    id: str


def _mount_volume() -> str | None:
    """The Docker volume backing the mount root, discovered rather than assumed.

    A named volume is what makes the sidecar usable from inside a container at
    all: the daemon resolves a ``-v`` source on the **host**, so a path like
    ``/data/runs/<bundle>`` -- which exists only inside this container's own
    mount -- becomes an empty directory on the host, and the sandbox starts,
    runs pytest, and collects nothing.

    The name cannot simply be the one in the compose file. Compose prefixes
    named volumes with the project name, so ``runs-data:`` is really
    ``multi-agent-team-dev_runs-data``; asking the daemon for ``runs-data``
    does not fail, it silently creates a *new empty* volume, which is how the
    first version of this mounted an empty directory and reported "no tests ran"
    with no error anywhere.

    So read the real name off this container's own mounts. ``SANDBOX_MOUNT_VOLUME``
    remains an override for the cases where the sidecar runs outside Docker.
    """
    override = (os.getenv("SANDBOX_MOUNT_VOLUME", "") or "").strip()
    if override:
        return override
    try:
        import subprocess

        # In a container the hostname is the container id, which is how a
        # process can ask the daemon about itself.
        cid = socket.gethostname().strip()
        if not cid:
            return None
        out = subprocess.run(
            [DOCKER, "inspect", cid, "--format", "{{json .Mounts}}"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if out.returncode != 0:
            return None
        root = str(mount_root())
        for m in json.loads(out.stdout or "[]"):
            if m.get("Destination") == root and m.get("Name"):
                return str(m["Name"])
    except Exception as exc:  # discovery is best-effort
        log.warning("could not determine the volume backing %s: %s", mount_root(), exc)
    return None


def _resolve_mount(mount: str | None) -> str | None:
    """Resolve a requested mount, or refuse it.

    Refusal rather than normalisation: ``..`` segments are checked *before* the
    resolved path is compared, so no traversal reaches the filesystem, and a
    symlink inside the runs volume cannot be used to point outside it either.
    """
    if not mount:
        return None
    raw = Path(mount)
    parts = raw.resolve().parts
    if ".." in parts:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="mount path may not contain '..'",
        )
    resolved = raw.resolve() if raw.is_absolute() else (mount_root() / raw).resolve()
    root = mount_root()
    try:
        resolved.relative_to(root)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"mount must be inside {root}",
        ) from None
    if not resolved.exists() or not resolved.is_dir():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="mount path is not a directory"
        )
    return str(resolved)


@app.get("/health")
def health() -> dict[str, Any]:
    """Liveness for compose, plus whether the engine itself answers.

    ``engine`` is separate from ``ok`` on purpose: the sidecar can be alive with
    a dead socket, and a healthcheck that only reported process liveness would
    let compose wire the backend to a service that cannot start anything.
    """
    engine = Docker.available()
    return {"ok": True, "engine": engine, "mount_root": str(mount_root())}


def _sanitise_env(env: dict[str, str]) -> dict[str, str]:
    """Drop platform credentials from anything a caller asks to forward.

    The sidecar is the last hop before a container, so it is the last place
    this can be enforced. :func:`app.services.playground.child_env` already
    strips them for the subprocess path, but a bug in a caller, a future
    endpoint that forgets, or a request crafted by hand would otherwise put
    ``GEMINI_API_KEY`` into a container running model-generated code -- which
    is precisely the exfiltration the sandbox network exists to prevent.

    Refused loudly rather than dropped quietly: a caller that believes it set a
    variable and did not is a bug worth surfacing.
    """
    leaked = sorted(
        k
        for k, v in env.items()
        if k in CREDENTIAL_ENV or "API_KEY" in k or _carries_credentials(k, v or "")
    )
    if leaked:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"refusing to pass platform credential(s) into a sandbox container: "
                f"{', '.join(leaked)}. Generated code must never see these."
            ),
        )
    return dict(env)


@app.post("/sandbox/runs")
def create_run(payload: RunRequest) -> dict[str, Any]:
    """Start one sandbox container under the fixed containment policy."""
    if not image_allowed(payload.image):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                f"image {payload.image!r} is not on the allowlist "
                f"({', '.join(allowed_images())})"
            ),
        )

    limits = SandboxLimits.from_env()
    mount_volume = _mount_volume()
    requested = payload.mount
    if requested is None and payload.workdir and mount_volume:
        # The caller named the bundle by its path *inside* the mount (the only
        # form that survives a `-v` source, which the daemon resolves on the
        # host). Turn it back into the host-side path so it goes through
        # `_resolve_mount` like any other request -- confinement is not
        # weakened by the caller having phrased it differently, and a workdir
        # pointing outside the mount is refused exactly as a mount would be.
        rel = PurePosixPath(payload.workdir)
        try:
            rel = rel.relative_to(limits.workdir)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"workdir must be inside {limits.workdir}",
            ) from None
        requested = str(mount_root() / rel)
    resolved_mount = _resolve_mount(requested)
    env = _sanitise_env(payload.env)
    workdir = None
    if mount_volume and resolved_mount:
        # The requester is a container, so `resolved_mount` names a path that
        # exists only inside this container's own mount of the runs volume. The
        # daemon resolves `-v` against the **host**, so passing it through would
        # create an empty directory on the host and mount that: the container
        # starts, pytest runs, and it collects zero tests because /app is empty.
        #
        # Mount the volume by name instead -- the daemon resolves volume names
        # globally, so where the requester runs stops mattering -- and point the
        # workdir at the bundle's directory inside it. Confinement is unaffected:
        # `resolved_mount` has already been checked against the mount root, and
        # the workdir is derived from that same checked path.
        root = mount_root()
        rel = Path(resolved_mount).resolve().relative_to(root)
        workdir = f"{limits.workdir}/{rel.as_posix()}"
    # The sidecar owns the socket, so it is also the only place that can build
    # the image. Doing it here rather than leaving it to a deploy step means a
    # fresh host works without a manual `docker build` someone has to remember.
    try:
        image = Docker.ensure_image(payload.image, SANDBOX_DIR)
    except Docker.DockerUnavailable as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"sandbox image unavailable: {exc}",
        ) from exc
    argv = run_argv(
        image,
        payload.cmd,
        limits=limits,
        name=payload.name,
        mount=None if mount_volume else resolved_mount,
        mount_volume=mount_volume if resolved_mount else None,
        workdir=workdir,
        env=env,
        detach=payload.detach,
        with_data=payload.with_data,
    )

    if not payload.detach:
        # A bounded, one-shot run: pytest, the boot check, static analysis.
        # Output comes back in the response body, which is what keeps the
        # backend from needing the socket just to read a result.
        result = Docker.run_container(argv, timeout=payload.timeout)
        return {
            "id": "",
            "host": None,
            "port": None,
            "returncode": result.returncode,
            "stdout": result.stdout[-20000:],
            "stderr": result.stderr[-20000:],
        }

    try:
        Docker.ensure_network(limits.network)
        container_id = Docker.start_detached(argv)
        host, port = Docker.reachable_address(container_id, limits.network)
    except Docker.DockerUnavailable as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
    return {"id": container_id, "host": host, "port": port, "started_at": time.time()}


@app.delete("/sandbox/runs/{container_id}")
def delete_run(container_id: str) -> DeleteResponse:
    """Stop and remove one container.

    Idempotent: stopping something already stopped is not an error, which is
    what a caller needs when a preview is reaped by the TTL while a user is
    clicking Stop.
    """
    return DeleteResponse(stopped=Docker.stop_container(container_id), id=container_id)


@app.get("/sandbox/runs/{container_id}")
def get_run(container_id: str) -> dict[str, Any]:
    """Whether a container is still up, and where it is reachable."""
    running = Docker.is_running(container_id)
    out: dict[str, Any] = {"id": container_id, "running": running}
    if running:
        limits = SandboxLimits.from_env()
        try:
            host, port = Docker.reachable_address(container_id, limits.network)
            out["host"], out["port"] = host, port
        except Docker.DockerUnavailable as exc:
            out["error"] = str(exc)
    return out


@app.api_route(
    "/sandbox/proxy/{container_id}/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
    include_in_schema=False,
)
async def proxy(container_id: str, request: Request, path: str = "") -> Response:
    """Forward one request to a sandbox container.

    Why the backend cannot simply dial the container: the containers are on
    ``sandbox-net`` and the backend is not, so a container IP is unreachable
    from it. The two ways to close that gap are both wrong in different ways.
    Attaching the backend to ``sandbox-net`` would put the platform's own API
    within reach of the code it is sandboxing -- any generated app could then
    call ``/api/*`` -- and publishing a host port would give sandboxed code a
    route off the machine.

    The sidecar is already on ``sandbox-net``, already holds the socket, and is
    on the backend's ``inner`` network. So it is the one component that can
    reach both ends, and relaying here keeps the backend off the untrusted
    network entirely.

    Async throughout: ``request.stream()`` is an async generator, and handing it
    to a synchronous client from a threadpool endpoint hangs until the client
    times out -- which is exactly what a request body of any size does here.
    The response is streamed back for the same reason a generated app's SSE
    would otherwise appear as a hung preview.
    """
    if not Docker.is_running(container_id):
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="sandbox container is not running",
        )
    limits = SandboxLimits.from_env()
    host, port = Docker.reachable_address(container_id, limits.network)
    url = f"http://{host}:{port}/{path}"

    headers = {
        k: v
        for k, v in request.headers.items()
        if k.lower() not in _HOP_BY_HOP and k.lower() not in {"host", "content-length"}
    }
    body = await request.body()
    try:
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=False) as client:
            upstream = await client.request(
                request.method,
                url,
                params=request.query_params,
                content=body if request.method not in {"GET", "HEAD"} else None,
                headers=headers,
            )
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"cannot reach the sandbox container: {exc}",
        ) from exc

    resp_headers = {
        k: v
        for k, v in upstream.headers.items()
        if k.lower() not in _HOP_BY_HOP
        and k.lower() not in {"content-encoding", "content-length"}
    }
    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        headers=resp_headers,
        media_type=upstream.headers.get("content-type"),
    )


@app.get("/sandbox/network")
def describe_network(name: str) -> dict[str, Any]:
    """Whether a network is `internal`, so the backend knows how to dial it.

    The backend cannot inspect the network itself -- it holds no socket -- and it
    cannot infer the answer either: on an internal network Docker will not
    publish a port at all, so asking for one yields a container that is healthy
    and unreachable. Guessing "not internal" is what would send it down the
    published-port path and produce exactly that.
    """
    Docker.ensure_network(name)
    return {"name": name, "internal": Docker.network_is_internal(name)}


@app.get("/sandbox/logs/{container_id}")
def container_logs(container_id: str, lines: int = 40) -> dict[str, str]:
    lines = max(1, min(int(lines), 500))
    return {"id": container_id, "logs": Docker.logs(container_id, lines)}


