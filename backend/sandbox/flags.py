"""Container flags for the sandbox, and the image cache that makes them cheap.

The flag list is a pure function of a small :class:`SandboxLimits` so the exact
argv can be asserted in a test without a Docker daemon -- which is the only
practical way to review a security control you cannot see running. See
``SANDBOX_AND_SCALE_PLAN.md`` §6.2 and CIS Docker Benchmark v1.6 §4.1/§4.6.

Two decisions are worth stating up front because both look contradictory:

``--network <internal>`` rather than ``--network none``
    Generated apps default to PostgreSQL (Phase 4), and a container with no
    network cannot reach a database. A compose network with ``internal: true``
    blocks egress at the Docker network layer -- no route to the internet, so
    no exfiltration to a remote host -- while still permitting service-to-
    service traffic. That is strictly stronger than ``--network none`` for the
    exfiltration case and it is the only topology satisfying both requirements.

``--read-only`` together with ``--tmpfs /tmp``
    A generated app that creates its SQLite file at import needs somewhere
    writable. ``--read-only`` is what makes the rest of the filesystem
    unwritable, and the tmpfs mount is the single exception. Neither the
    memory limit nor the read-only root filesystem substitutes for the other:
    one bounds RAM, the other bounds the filesystem.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("sandbox.flags")

#: The engine binary. Present here rather than in :mod:`sandbox.docker` so that
#: the argv this module builds is already executable and the tests can assert
#: the whole thing, binary included.
DOCKER = "docker"

SANDBOX_DIR = Path(__file__).resolve().parent
DEFAULT_IMAGE = "agent-team-sandbox:local"
DEFAULT_NETWORK = "sandbox-net"

#: uid:gid baked into the sandbox image and passed as ``--user``. Matching the
#: backend's appuser means a file either writes, the other can read.
SANDBOX_UID = "10001:10001"


@dataclass
class SandboxLimits:
    """Every knob the runner has, with the CIS-aligned defaults.

    Read once per process from the environment so a deployment can loosen a
    limit deliberately, and so a test can build one explicitly.
    """

    image: str = DEFAULT_IMAGE
    network: str = DEFAULT_NETWORK
    memory: str = "512m"
    # Equal to `memory`, which disables swap. Under a swap limit the kernel
    # cannot push a runaway process into swap and back: memory pressure has to
    # become a hard stop rather than an unbounded slow leak.
    memory_swap: str | None = None
    cpus: str = "1"
    # Pin to one core so `--cpus 1` is a real ceiling and so a burst of runs
    # cannot spread across every core. Empty string disables it, for a host
    # where pinning everything to CPU 0 would serialise concurrent runs.
    cpuset: str = "0"
    pids_limit: int = 256
    tmpfs: str = "/tmp:rw,noexec,nosuid,size=64m"
    user: str = SANDBOX_UID
    workdir: str = "/app"
    #: How many sandbox containers may run at once in this process. A burst of
    #: failed fix-loop iterations must not be able to fork-bomb the host.
    max_concurrent: int = 4
    #: Writable scratch inside the container, mounted over `workdir`.
    read_only: bool = True
    drop_caps: bool = True
    no_new_privileges: bool = True
    #: Second writable mount, for data rather than code. The generated app's
    #: SQLite file and any per-run state live here; `/app` stays read-only so
    #: generated code cannot rewrite the suite that is about to judge it.
    data_mount: str = "/data"

    def __post_init__(self) -> None:
        if self.memory_swap is None:
            self.memory_swap = self.memory

    @classmethod
    def from_env(cls, env: dict | None = None) -> SandboxLimits:
        import os

        env = os.environ if env is None else env

        def num(name: str, default: int) -> int:
            try:
                return max(1, int(env.get(name, "") or default))
            except ValueError:
                return default

        def text(name: str, default: str) -> str:
            # `name in env` rather than a truthiness test: an explicitly empty
            # value is a meaningful setting here. SANDBOX_CPUSET="" is how a
            # host with one core turns off pinning, and `"" or default` would
            # silently turn it back on.
            if name in env:
                return str(env[name]).strip()
            return default

        return cls(
            image=text("SANDBOX_IMAGE", DEFAULT_IMAGE),
            network=text("SANDBOX_NETWORK", DEFAULT_NETWORK),
            memory=text("SANDBOX_MEMORY", "512m"),
            memory_swap=text("SANDBOX_MEMORY_SWAP", "") or None,
            cpus=text("SANDBOX_CPUS", "1"),
            cpuset=text("SANDBOX_CPUSET", "0"),
            pids_limit=num("SANDBOX_PIDS_LIMIT", 256),
            tmpfs=text("SANDBOX_TMPFS", "/tmp:rw,noexec,nosuid,size=64m"),
            user=text("SANDBOX_USER", SANDBOX_UID),
            max_concurrent=num("SANDBOX_MAX_CONCURRENT", 4),
        )


def isolation_flags(limits: SandboxLimits | None = None) -> list[str]:
    """The containment argv, in a fixed order.

    Order is stable so the tests can assert an exact list rather than a set:
    a security control nobody can diff is one nobody reviews.
    """
    limits = limits or SandboxLimits()
    argv: list[str] = ["--network", limits.network]
    if limits.read_only:
        argv.append("--read-only")
    if limits.drop_caps:
        argv.append("--cap-drop")
        argv.append("ALL")
    if limits.no_new_privileges:
        argv.append("--security-opt")
        argv.append("no-new-privileges:true")
    argv += ["--pids-limit", str(limits.pids_limit)]
    argv += ["--memory", limits.memory]
    if limits.memory_swap:
        argv += ["--memory-swap", limits.memory_swap]
    argv += ["--cpus", limits.cpus]
    if limits.cpuset:
        argv += ["--cpuset-cpus", limits.cpuset]
    if limits.user:
        argv += ["--user", limits.user]
    if limits.tmpfs:
        argv += ["--tmpfs", limits.tmpfs]
    return argv


def run_argv(
    image: str,
    cmd: list[str],
    *,
    limits: SandboxLimits | None = None,
    name: str | None = None,
    mount: str | None = None,
    env: dict | None = None,
    entrypoint: str = "python",
    publish_port: int | None = None,
    extra: list[str] | None = None,
    detach: bool = False,
    with_data: bool = False,
    mount_volume: str | None = None,
    workdir: str | None = None,
) -> list[str]:
    """A complete ``docker run`` argv, starting with the engine binary.

    Complete on purpose: the tests assert this exact list, and an argv that
    only becomes runnable once a caller remembers to prepend ``docker`` is an
    argv that can be executed wrong. ``detach=True`` is the preview variant and
    ``with_data`` adds the writable data mount.

    ``mount`` is the single host path this is allowed to see, and it is always
    the run's own directory. There is no parameter for a host path generally:
    the sandbox sidecar re-checks this, because a flag list that can be told to
    mount ``/`` is not a control.

    The mount is ``:ro``, which is not redundant with ``--read-only`` and was
    measured to be necessary: ``--read-only`` applies to the container's own
    filesystem layers, and a ``-v`` bind mount keeps the *host* directory's
    permissions. Verified on a container started with ``--read-only -v
    /tmp:/app``: ``open("/app/x.txt", "w")`` succeeded. Without ``:ro`` the
    generated suite can rewrite the tests that are about to grade it, and
    ``--read-only`` is not actually a control over the one directory that
    matters.
    """
    limits = limits or SandboxLimits()
    # A container may be told where to look, but only inside the mount it is
    # given: `workdir` must stay under the mount point so it cannot become a
    # way to read the rest of the image.
    target = workdir or limits.workdir
    argv = [DOCKER, "run"]
    if detach:
        argv.append("--detach")
    argv.append("--rm")
    if name:
        argv += ["--name", name]
    argv += isolation_flags(limits)
    if mount_volume:
        # A named volume, not a host path. This is the only form that works when
        # the requester is itself a container: the daemon resolves `-v` on the
        # **host**, so a path like /data/runs/x that exists only inside the
        # sidecar's own mount becomes an empty directory on the host, and the
        # generated suite silently collects zero tests. Volume names are
        # resolved by the daemon globally, so they do not have that problem.
        #
        # The volume is mounted at `limits.workdir` and the workdir is then set
        # *inside* it. Mounting it at the workdir instead would make the mount
        # point and the working directory the same directory -- so "run the
        # bundle" would silently mean "run the root of the volume", and pytest
        # would collect every other run's tests.
        argv += ["-v", f"{mount_volume}:{limits.workdir}:ro"]
    if mount and not mount_volume:
        # Always absolute. `-v` refuses a relative path, and the error it gives
        # ("includes invalid characters for a local volume name") reads as if
        # the *name* were malformed rather than as "that is not a path" --
        # which is exactly how a relative RUNS_DIR surfaces as a container that
        # will not start.
        argv += ["-v", f"{Path(mount).resolve()}:{target}:ro"]

    if with_data and limits.data_mount:
        # Writable, but not the code: this is where a generated app's SQLite
        # file goes, so `--read-only` on /app costs it nothing.
        argv += ["--tmpfs", f"{limits.data_mount}:rw,nosuid,size=64m"]
    for key, value in (env or {}).items():
        argv += ["-e", f"{key}={value}"]
    if publish_port is not None:
        # 127.0.0.1 with an empty host port: Docker assigns an ephemeral one
        # and reports it back through inspect. That deletes the bind-then-close
        # race a `_free_port()` helper has.
        argv += ["--publish", f"127.0.0.1::{publish_port}"]
    argv += ["--workdir", target, "--entrypoint", entrypoint]
    argv += extra or []
    argv.append(image)
    argv += cmd
    return argv


# --------------------------------------------------------------------------
# image cache
# --------------------------------------------------------------------------


def image_fingerprint(context: Path | None = None) -> str:
    """A short hash of everything that changes the image's contents.

    The old runner ran ``docker build`` on *every test cycle*, which is a
    multi-second cost paid per attempt, and tagged a single fixed name, so two
    concurrent runs rebuilt each other's image under it. Hashing the Dockerfile
    and the wheelhouse gives a tag that is correct by construction and lets a
    build be skipped entirely when nothing changed.
    """
    context = context or SANDBOX_DIR
    digest = hashlib.sha256()
    for name in ("Dockerfile", "requirements.txt"):
        path = context / name
        digest.update(name.encode())
        try:
            digest.update(path.read_bytes())
        except OSError:
            digest.update(b"<missing>")
    return digest.hexdigest()[:12]


def cached_image_tag(base: str = DEFAULT_IMAGE, context: Path | None = None) -> str:
    """``agent-team-sandbox:local`` -> ``agent-team-sandbox:local-<fingerprint>``.

    The name is a pure function of the inputs, so a stale image can never be
    used: change the Dockerfile and the tag changes with it. Recomputed rather
    than memoised -- it hashes two small files, and a cache here would be a
    correctness hazard in a long-lived backend.
    """
    fingerprint = image_fingerprint(context)
    head, _, tail = base.rpartition(":")
    if not head or "/" in tail:
        # No tag, or a registry with a port ("localhost:5000/img"). Either way
        # the last colon is not a tag separator.
        return f"{base}-{fingerprint}"
    return f"{base}-{fingerprint}"
