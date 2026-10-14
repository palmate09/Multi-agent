"""Playground: run a generated app and keep it addressable.

One :class:`PreviewSession` per run. The app is copied to its own directory and
booted, and held in a run-id -> port map that the gateway resolves per request.
**The gateway contract does not change**: ``PreviewSession.port`` stays an
``int`` and the gateway still proxies to ``127.0.0.1:<port>``. Only the
provenance of that port changes between the two transports below.

``container`` (default)
    A container from the sandbox image, with the same containment the test loop
    uses: read-only filesystem, no capabilities, non-root uid, a pids limit,
    memory and CPU ceilings, and a network with ``internal: true`` so there is
    no route to the internet. The port is assigned by Docker and read back with
    ``docker inspect``, which also deletes the bind-then-close race a
    ``_free_port()`` helper necessarily has.

``subprocess``
    A plain child process on the host with the platform's filesystem and
    network. It exists so the project can be developed and tested without a
    container engine, and it is off in every deployed profile. It is **not**
    isolation, and the docs say so plainly.

Either way three things are done about the obvious foot-guns: every platform
credential is stripped from the child environment
(:data:`STRIPPED_ENV`); the server binds ``127.0.0.1`` so only the
authenticated gateway can reach it; and a TTL reaper plus a concurrency cap
bound how long and how many.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import httpx

from app.config import Settings, get_settings
from sandbox import docker as Docker
from sandbox import runner as Runner
from sandbox.flags import SandboxLimits, run_argv

log = logging.getLogger("app.playground")


#: Never inherited by the generated app. These are the platform's own
#: credentials: handing them to generated code would let a bad generation (or a
#: prompt injection inside the requirement) exfiltrate the whole deployment.
STRIPPED_ENV = (
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
    "OPENAI_BASE_URL",
)

#: Server must not announce its version to a page served inside our own iframe.
_UVICORN_ARGS = ("--log-level", "warning", "--no-access-log")

#: Health-probe schedule: start tight, back off to a quarter second. See
#: ``PlaygroundService._await_health``.
_PROBE_START_INTERVAL = 0.05
_PROBE_MAX_INTERVAL = 0.25

#: The port the app listens on *inside* the container. Fixed, because the
#: container's network namespace is its own; the port the host publishes is
#: assigned by Docker and read back from inspect.
_CONTAINER_PORT = 8000

IDLE_STATES = ("stopped", "crashed")

#: One requirement line: an optional name, an optional extras group, an optional
#: version specifier and an optional environment marker. Deliberately not a full
#: PEP 508 parser -- ``packaging`` is not a declared dependency, and the only
#: decision this feeds is "is it worth spawning pip", where an unparseable line
#: is simply treated as unsatisfied.
_REQUIREMENT = re.compile(
    r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:\[[^\]]*\])?\s*(?:[<>=!~].*)?(?:;.*)?$"
)


def parse_requirements(text: str) -> set[str]:
    """Distribution names declared in a requirements file, PEP 503 normalised.

    Comments, flags (``-r``, ``--index-url``), editables and bare URLs are
    dropped rather than guessed at: they cannot be checked against what is
    installed, so the caller treats the result as unsatisfied and runs pip.
    """
    names: set[str] = set()
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        m = _REQUIREMENT.match(line)
        if m:
            names.add(_normalise_dist(m.group(1)))
    return names


def _normalise_dist(name: str) -> str:
    """``Foo_Bar`` and ``foo-bar`` are the same distribution."""
    return name.strip().lower().replace("_", "-").replace(".", "-")


@lru_cache(maxsize=1)
def _installed_distributions() -> set[str]:
    """Every distribution installed in this interpreter, normalised the same way.

    Read from ``importlib.metadata`` rather than by trying to import each
    requirement, because the name a project is distributed under is not the name
    it imports (``pyyaml`` -> ``yaml``, ``python-multipart`` -> ``multipart``)
    and getting that mapping wrong would install packages that are already
    present. Cached: the first call walks every ``sys.path`` entry.
    """
    found: set[str] = set()
    try:
        from importlib.metadata import distributions

        for dist in distributions():
            name = dist.metadata["Name"] if dist.metadata else None
            if name:
                found.add(_normalise_dist(name))
    except Exception as exc:  # a broken metadata dir must not block a preview
        log.warning("could not enumerate installed distributions: %s", exc)
    return found


def _free_port() -> int:
    """Bind ``:0``, read the port back, close, return it.

    There is an unavoidable race between closing and uvicorn binding it. It is
    the best available without holding the socket open (which uvicorn would
    then refuse to bind) and the window is a few microseconds on loopback.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def child_env(workdir: Path, limits: SandboxLimits | None = None) -> dict[str, str]:
    """Environment for the generated app: platform keys removed, CWD pinned.

    ``workdir`` is only honoured in subprocess mode. In container mode the code
    is mounted read-only and the writable data directory is a tmpfs inside the
    container, so the paths that matter are the container's, not the host's --
    pointing ``SQLITE_PATH`` at a read-only bind mount would make every app that
    creates a database at import fail to boot, with an error that says nothing
    about why.
    """
    env = {k: v for k, v in os.environ.items() if k not in STRIPPED_ENV and "API_KEY" not in k}
    data_dir = (
        str(workdir) if limits is None else (limits.data_mount or str(workdir))
    )
    env.update(
        {
            # Tells generated code it is being previewed, not deployed.
            "PREVIEW": "1",
            "SANDBOX": "1",
            "PYTHONUNBUFFERED": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            # Any SQLite path the generated code builds from its own file name
            # has to land in the writable data directory, not in the run
            # directory the pipeline reads.
            "PREVIEW_DIR": data_dir,
            "SQLITE_PATH": f"{data_dir}/preview.db",
            "DATABASE_URL": f"sqlite:///{data_dir}/preview.db",
        }
    )
    return env


@dataclass
class PreviewSession:
    """One running (or stopped) preview.

    Holds both transports behind one shape: ``proc`` for the subprocess path,
    ``container_id`` for the container path. Exactly one is set. ``port`` is an
    ``int`` in both cases and the gateway proxies to ``127.0.0.1:<port>``
    regardless, which is what keeps the ``/preview/<rid>/`` contract identical
    to the one the frontend already talks to.
    """

    run_id: str
    port: int
    #: Where ``port`` is. "127.0.0.1" in subprocess mode; in container mode it
    #: is the container's address on the internal sandbox network, because an
    #: internal network cannot publish host ports at all. The gateway only ever
    #: reads the pair, which is why the contract the frontend depends on is the
    #: same in both modes.
    host: str
    workdir: Path
    log_path: Path
    run_dir: Path
    started_at: float = 0.0
    last_hit: float = field(default_factory=time.time)
    state: str = "starting"
    error: str = ""
    entry: str = ""
    proc: subprocess.Popen | None = None
    #: Set instead of ``proc`` in container mode.
    container_id: str | None = None
    #: Set when the container is only reachable through the sandbox sidecar.
    proxy_url: str | None = None
    #: Sticky: True once the app has answered a health probe at least once.
    #: ``state`` alone cannot answer "did this app ever boot?", because
    #: stopping a preview resets it to "stopped" — and an eval that reads the
    #: file afterwards must not score a successful boot as a failure.
    ever_booted: bool = False
    #: Set the instant teardown begins, before any signal is sent.
    #:
    #: A session leaves the service's map before _terminate() finishes, because
    #: waiting for a process to exit takes seconds and holding the service lock
    #: across that would stall the API thread. Without this flag, "removed from
    #: the map" and "the port is released" are two different instants, and
    #: anything that observes the first and checks the second sees a process
    #: that is on its way out still answering. Terminating is the first thing
    #: teardown does, so it is the flag's first write.
    terminating: bool = False

    @property
    def mode(self) -> str:
        return "container" if self.container_id else "subprocess"

    @property
    def base_url(self) -> str:
        """Where the gateway sends proxied requests for this preview.

        Normally the container's own address. When the backend cannot reach
        that address -- it is not on ``sandbox-net``, where the containers
        live -- the sidecar is, so requests go through it instead. Set once at
        spawn rather than recomputed per request: it is a property of where the
        caller sits, not of the container's current state.
        """
        if self.proxy_url:
            return self.proxy_url
        return f"http://{self.host}:{self.port}"

    @property
    def entry_path(self) -> str:
        """The best path to open the preview at: the spec's first real route.

        Same list the boot probe uses, so the frame and the health check agree
        on what "the app is up" means. Falls back to ``/`` for an app that
        declares nothing, which is then also what the probe tried.
        """
        try:
            return _probe_paths(self.run_dir)[0]
        except Exception:
            return "/"

    def alive(self) -> bool:
        if self.terminating:
            return False
        if self.container_id:
            # `docker inspect` rather than a cached flag: the whole point of
            # the crash-restart path is to notice a container that died on its
            # own (OOM, a review patch that broke the app).
            return Docker.is_running(self.container_id)
        return self.proc is not None and self.proc.poll() is None


    def log_tail(self, lines: int = 40) -> str:
        """The last ``lines`` of the app's output, from wherever it is running."""
        if self.container_id:
            try:
                return "\n".join(Docker.logs(self.container_id, lines).splitlines()[-lines:])
            except Docker.DockerUnavailable:
                return ""
        try:
            text = self.log_path.read_text(errors="replace")
        except OSError:
            return ""
        return "\n".join(text.splitlines()[-lines:])

    def status(self) -> dict[str, Any]:
        """The wire shape the gateway returns. Never raises."""
        idle = int(time.time() - self.last_hit)
        return {
            "run_id": self.run_id,
            "state": self.state,
            "url": f"/preview/{self.run_id}/" if self.state == "running" else None,
            "port": self.port if self.state == "running" else None,
            # Diagnostic only. The client always talks to /preview/<rid>/; it
            # never learns an internal address.
            "host": self.host if self.state == "running" else None,
            "started_at": self.started_at or None,
            "idle_seconds": idle,
            "error": self.error or None,
            "ever_booted": self.ever_booted,
            # Which transport actually ran it. The Playground tab must behave
            # identically either way, so this is diagnostics, not behaviour --
            # but "subprocess" is the single most important thing to know when
            # reading a screenshot of a machine you do not trust.
            "mode": self.mode,
            # Where the iframe should actually point. `/` is frequently not a
            # route the generated app declares, so pointing the frame at the
            # gateway root gives a 404 for a perfectly healthy app -- which
            # reads as "the playground is broken" when the app is serving.
            "entry_path": self.entry_path,
            "log_tail": self.log_tail(),
        }

    def persist(self) -> None:
        """Mirror the status into the run directory.

        The in-memory session is gone the moment the process stops, so this is
        the durable record the eval metrics read for ``preview_booted`` — by
        the time an eval runs, every preview has long been reaped.
        """
        if not self.run_dir.is_dir():
            return
        with contextlib.suppress(OSError):
            (self.run_dir / "playground.json").write_text(
                json.dumps(self.status(), indent=2, default=str)
            )


class PlaygroundService:
    """Owns every preview on this host.

    Thread-safe: the API thread starts and stops sessions while a reaper thread
    expires them.
    """

    def __init__(self, settings: Settings | None = None, runs_dir: Path | None = None):
        self.settings = settings or get_settings()
        root = Path(runs_dir or self.settings.runs_dir)
        self.runs_dir = root
        # Dot-prefixed so rehydrate() (which scans runs_dir) and the artifact
        # listing never mistake a working directory for a run.
        self.root = root / ".preview"
        # Short, and overridable so a test can drive a real tick without
        # sleeping 15 seconds.
        self._reap_interval = float(getattr(settings, "preview_reap_interval", 15) or 15)
        self._sessions: dict[str, PreviewSession] = {}
        self._lock = threading.RLock()
        self._reaper: threading.Thread | None = None
        self._stop = threading.Event()

    # -- lifecycle ------------------------------------------------------
    def start_reaper(self) -> None:
        if self._reaper is not None:
            return
        self._stop.clear()
        self._reaper = threading.Thread(
            target=self._reap_loop, name="playground-reaper", daemon=True
        )
        self._reaper.start()
        log.info("playground reaper started (every %ss)", self._reap_interval)

    def shutdown(self) -> None:
        """Stop the reaper and kill every child. Called from the app lifespan."""
        self._stop.set()
        reaper, self._reaper = self._reaper, None
        if reaper is not None:
            reaper.join(timeout=2.0)
        with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for session in sessions:
            self._terminate(session)

    # -- queries --------------------------------------------------------
    def status(self, run_id: str) -> dict[str, Any]:
        with self._lock:
            session = self._sessions.get(run_id)
        if session is None:
            return {
                "run_id": run_id,
                "state": "stopped",
                "url": None,
                "port": None,
                "host": None,
                "started_at": None,
                "idle_seconds": 0,
                "error": None,
                "ever_booted": False,
                "mode": None,
                "log_tail": "",
            }
        # A process that died under us (a review patch rewrote its files, an
        # OOM kill) must not keep reporting "running".
        if session.state == "running" and not session.alive():
            session.state = "crashed"
            session.error = (
                "the preview container exited"
                if session.container_id
                else "the preview process exited"
            )
            session.persist()
        return session.status()

    def resolve(self, run_id: str) -> PreviewSession | None:
        """The live session for a run, or None. One auto-restart attempt.

        A restart is not a convenience here: a review patch rewrites the
        generated files while the preview holds them open, and a re-run leaves
        an old process pointing at deleted code. One attempt is enough; after
        that the caller gets 502 and the status endpoint explains.
        """
        with self._lock:
            session = self._sessions.get(run_id)
            if session is None:
                return None
            if session.alive():
                session.last_hit = time.time()
                return session
            if session.state != "running":
                return None
            log.warning("preview %s died unexpectedly; restarting once", run_id)
            run_dir = session.run_dir
        try:
            return self.start(run_id, run_dir=run_dir, restart=True)
        except Exception as exc:
            log.warning("preview %s could not be restarted: %s", run_id, exc)
            return None

    def count(self) -> int:
        with self._lock:
            return sum(1 for s in self._sessions.values() if s.state in ("starting", "running"))

    # -- start / stop ---------------------------------------------------
    def start(
        self,
        run_id: str,
        run_dir: str | Path | None = None,
        restart: bool = False,
    ) -> PreviewSession:
        """Boot the run's generated app. Idempotent when already running.

        ``restart=True`` replaces a live session (used after a review patch);
        otherwise a running session is returned untouched.
        """
        source = Path(run_dir) if run_dir else self.runs_dir / run_id
        if not source.is_dir():
            raise FileNotFoundError(f"run '{run_id}' has no directory on disk")
        code_dir = source / "code"
        if not (code_dir / "main.py").exists() and not any(code_dir.glob("*.py")):
            raise FileNotFoundError(f"run '{run_id}' has no generated code to preview")

        with self._lock:
            existing = self._sessions.get(run_id)
            ever_booted = existing.ever_booted if existing is not None else False
            if existing is not None:
                if existing.alive() and existing.state == "running" and not restart:
                    return existing
                self._terminate(existing)
                del self._sessions[run_id]
            if self.count() >= max(1, self.settings.preview_max_concurrent):
                raise RuntimeError(
                    f"preview limit reached ({self.settings.preview_max_concurrent}); "
                    "stop another preview first"
                )

        workdir = self.root / run_id
        self._sync_code(code_dir, source, workdir)
        entry = self._discover_entry(workdir)
        if entry is None:
            raise RuntimeError(
                "the generated code exposes no ASGI app object, so there is nothing to serve"
            )
        entry_module, entry_attr = entry

        session = PreviewSession(
            run_id=run_id,
            # Provisional. In container mode the real port comes back from
            # `docker inspect` once the container is up, because Docker assigns
            # it; in subprocess mode it is chosen here. Either way the gateway
            # only ever reads the int.
            port=0,
            host="127.0.0.1",
            workdir=workdir,
            log_path=workdir / "preview.log",
            run_dir=source,
            started_at=time.time(),
            # Carried across restarts: "did this app ever boot" is a fact
            # about the run, not about one process lifetime.
            ever_booted=ever_booted,
        )
        with self._lock:
            self._sessions[run_id] = session

        try:
            if not self._install_requirements(workdir):
                # Skipped on purpose: in container mode nothing is installed
                # into the image at run time. A dependency outside the
                # wheelhouse means the app cannot boot, and the boot failure
                # says so by name rather than leaving an ImportError.
                log.debug("preview %s: no dependency install needed", run_id)
            self._spawn(session, entry_module, entry_attr)
        except Exception as exc:
            with self._lock:
                self._sessions.pop(run_id, None)
            self._terminate(session)
            if session.container_id or session.proc:
                raise
            # A failure before anything was spawned is a plain error: no
            # engine, no image, no entry point to serve.
            raise RuntimeError(
                f"preview for run '{run_id}' could not start: {exc}"
            ) from exc

        session.error = ""
        if self._await_health(session, source):
            session.state = "running"
            session.ever_booted = True
            log.info(
                "preview %s running at %s (%s, %s)",
                run_id,
                session.base_url,
                session.entry,
                session.mode,
            )
        else:
            session.state = "crashed"
            detail = (
                f"the app did not answer 200 on {', '.join(_probe_paths(source))} within "
                f"{self.settings.preview_boot_timeout_seconds}s"
            )
            session.error = (detail + "\n" + session.log_tail(20))[-1200:]
            log.warning("preview %s failed to boot: %s", run_id, detail)
        session.persist()
        return session

    def stop(self, run_id: str) -> bool:
        """Stop a preview. False when there was nothing running."""
        with self._lock:
            session = self._sessions.pop(run_id, None)
            if session is not None:
                self._claim(session)
        if session is None:
            return False
        self._terminate(session)
        session.persist()
        shutil.rmtree(session.workdir, ignore_errors=True)
        return True

    def stop_all(self) -> int:
        with self._lock:
            run_ids = list(self._sessions)
        return sum(1 for rid in run_ids if self.stop(rid))

    # -- internals ------------------------------------------------------
    def _sync_code(self, code_dir: Path, source: Path, workdir: Path) -> None:
        """Copy the run's code (and its UI) into a private working directory.

        A copy rather than running in place: the app writes a SQLite file and
        bytecode next to itself, and the run directory is the pipeline's
        durable record, which a user may delete or resume from at any time.

        The UI is also dropped into ``static/`` so an app that mounts
        ``StaticFiles(directory="static")`` finds it. That is a bonus, not the
        mechanism: the Developer that wrote the API never saw the UI, so most
        generated apps mount nothing, and the gateway serves ``ui/`` itself.
        """
        shutil.rmtree(workdir, ignore_errors=True)
        workdir.mkdir(parents=True, exist_ok=True)
        shutil.copytree(code_dir, workdir, dirs_exist_ok=True)
        ui_dir = source / "ui"
        if ui_dir.is_dir():
            for target in (workdir / "ui", workdir / "static"):
                shutil.copytree(ui_dir, target, dirs_exist_ok=True)

    def _discover_entry(self, workdir: Path) -> tuple[str, str] | None:
        """Resolve the ASGI app the way the test runner does: from the code.

        Statically, via AST. Importing the generated module *here* would execute
        untrusted code inside the platform process, with every credential this
        service goes to such lengths to strip. The import happens in the child,
        under the hardened environment, where it belongs.
        """
        try:
            from agents.introspect import find_app_object

            files: dict[str, str] = {}
            for path in sorted(workdir.rglob("*.py")):
                if "__pycache__" in path.parts:
                    continue
                try:
                    files[str(path.relative_to(workdir))] = path.read_text()
                except OSError:
                    continue
            return find_app_object(files)
        except Exception as exc:
            log.warning("preview entry discovery failed: %s", exc)
            return None

    def _install_requirements(self, workdir: Path) -> bool:
        """Best-effort ``pip install`` of the generated app's dependencies.

        Subprocess mode only. A container cannot install anything -- it has no
        route to PyPI and its image is fixed -- so in container mode a missing
        dependency is a wheelhouse problem, reported by name when the app fails
        to boot, not something to paper over with a failing subprocess here.

        Capped and never fatal in either mode: the interpreter already has
        FastAPI, SQLAlchemy and uvicorn, so the common case needs no install at
        all. The measured cost of *not* checking was 0.52 s plus a subprocess on
        every preview start, for requirements that were already satisfied.
        """
        req = workdir / "requirements.txt"
        if not req.is_file():
            return False
        wanted = parse_requirements(req.read_text(errors="replace"))
        if not wanted:
            return False
        if wanted <= _installed_distributions():
            log.debug("preview %s: all %d requirements already satisfied", workdir.name, len(wanted))
            return False
        if Runner.uses_docker():
            # Named, not installed. A user watching a preview crash on an
            # ImportError deserves to be told which line to add.
            missing = ", ".join(f"`{d}`" for d in sorted(wanted - _installed_distributions())[:6])
            log.info(
                "preview %s needs %s, which is not in the sandbox wheelhouse",
                workdir.name,
                missing,
            )
            return False
        try:
            subprocess.run(
                [sys.executable, "-m", "pip", "install", "--quiet", "-r", str(req)],
                cwd=workdir,
                env=child_env(workdir),
                capture_output=True,
                timeout=60,
                check=False,
            )
            return True
        except Exception as exc:
            log.info("preview dependency install skipped: %s", exc)
            return False

    def _spawn(self, session: PreviewSession, module: str, attr: str) -> None:
        session.entry = f"{module}:{attr}"
        if Runner.uses_docker():
            self._spawn_container(session, module, attr)
        else:
            self._spawn_subprocess(session, module, attr)

    def _spawn_container(self, session: PreviewSession, module: str, attr: str) -> None:
        """Start the preview in a sandbox container and adopt Docker's address.

        Nothing is chosen beforehand. On the deployed internal network the app
        is reached at the container's own address, and on a development bridge
        network at a host port Docker assigns and reports through ``inspect``.
        Either way there is no window between picking a number and binding it --
        which is the entire reason ``_free_port()`` existed, with its
        bind/close/rebind race.
        """
        limits = SandboxLimits.from_env()
        Docker.ensure_network(limits.network)
        image = Docker.ensure_image(limits.image)
        # The workdir is mounted read-only like every other sandbox mount, so
        # the writable state -- a SQLite file, uploaded artefacts -- goes to the
        # /data tmpfs instead. That is why the child env below points there.
        #
        # Behind the sidecar the runs volume is mounted *by name* and the workdir
        # set inside it. A host path here is a path that exists only inside this
        # container, and the daemon resolves `-v` sources on the host -- so it
        # would create an empty directory there and the preview would start
        # uvicorn against a directory with no `main.py` in it. That presents as
        # "the app did not answer on /health", which is exactly what it looks
        # like when a generated app is simply broken.
        volume = Runner._runs_volume() if Runner.uses_docker() else None
        inside = None
        if volume:
            root = Runner._mount_root()
            if root and root in session.workdir.parents:
                rel = session.workdir.relative_to(root)
                inside = f"{limits.workdir}/{rel.as_posix()}"
        argv = run_argv(
            image,
            [
                "-m",
                "uvicorn",
                f"{module}:{attr}",
                "--host",
                "0.0.0.0",
                "--port",
                str(_CONTAINER_PORT),
                *_UVICORN_ARGS,
            ],
            limits=limits,
            mount=None if volume else str(session.workdir),
            mount_volume=volume if inside else None,
            workdir=inside,
            env=child_env(session.workdir, limits=limits),
            entrypoint="python",
            detach=True,
            with_data=True,
        )
        session.container_id = Docker.start_detached(argv, mount=str(session.workdir))
        session.host, session.port = Docker.reachable_address(
            session.container_id, limits.network, _CONTAINER_PORT
        )
        # The backend is not on sandbox-net, so that address has no route from
        # here; the sidecar is on both networks and relays.
        session.proxy_url = Docker.preview_base_url(session.container_id)
        log.debug("preview container %s reachable at %s", session.container_id, session.base_url)

    def _spawn_subprocess(self, session: PreviewSession, module: str, attr: str) -> None:
        session.port = _free_port()
        logfile = session.log_path.open("w")
        session.proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                f"{module}:{attr}",
                "--host",
                "127.0.0.1",
                "--port",
                str(session.port),
                *_UVICORN_ARGS,
            ],
            cwd=session.workdir,
            env=child_env(session.workdir),
            stdout=logfile,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            # Own process group, so stop() takes the reloader's children too.
            start_new_session=True,
        )

    def _await_health(self, session: PreviewSession, source: Path) -> bool:
        """Poll until the app answers 200, or give up.

        Every candidate is tried on each pass, not one candidate for the whole
        budget: a generated app routinely answers on ``/`` while the spec's
        first declared path 404s, and spending the entire timeout on that one
        path would report a healthy app as crashed.

        The interval grows from 50 ms. A flat 250 ms made every boot pay a
        quarter-second floor on its first probe even when uvicorn was answering
        within a few milliseconds; the ceiling is unchanged, so a genuinely
        slow app still gets the full timeout.
        """
        candidates = _probe_paths(source)
        deadline = time.time() + max(5, self.settings.preview_boot_timeout_seconds)
        delay = _PROBE_START_INTERVAL
        while time.time() < deadline:
            if not session.alive():
                return False
            for candidate in candidates:
                url = f"{session.base_url}{candidate}"
                try:
                    if httpx.get(url, timeout=2.0, follow_redirects=False).status_code == 200:
                        return True
                except Exception:
                    continue
            time.sleep(delay)
            delay = min(delay * 2, _PROBE_MAX_INTERVAL)
        return False

    def _claim(self, session: PreviewSession) -> None:
        """Mark a session as being torn down. Caller holds ``self._lock``."""
        session.terminating = True

    def _terminate(self, session: PreviewSession) -> None:
        # First statement, before the state changes: see PreviewSession.terminating.
        session.terminating = True
        session.state = "stopped"
        if session.container_id:
            container_id, session.container_id = session.container_id, None
            session.proc = None
            Docker.stop_container(container_id)
            return
        proc, session.proc = session.proc, None
        if proc is None or proc.poll() is not None:
            return
        with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
            os.killpg(proc.pid, 15)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
                os.killpg(proc.pid, 9)
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(timeout=2)

    def _reap_loop(self) -> None:
        while not self._stop.wait(self._reap_interval):
            try:
                self.reap()
            except Exception:  # the reaper must outlive one bad tick
                # A raising reaper silently stops reaping, and nothing else
                # would notice: previews would hold their ports until the
                # process restarts. Log and keep the loop alive instead.
                log.exception("preview reaper tick failed; continuing")

    def reap(self) -> int:
        """Expire idle and dead previews now. Returns how many were reaped."""
        ttl = max(30, self.settings.preview_ttl_seconds)
        now = time.time()
        with self._lock:
            expired = [
                s
                for s in self._sessions.values()
                if s.state in IDLE_STATES or now - s.last_hit > ttl
            ]
            for session in expired:
                self._sessions.pop(session.run_id, None)
                # Claimed under the same lock that removed it, so "no longer in
                # the map" and "no longer alive" are one instant. Claiming
                # afterwards leaves a window in which a caller that just saw the
                # map empty still sees a running process -- which is the race a
                # TTL test, or a gateway request, can lose.
                self._claim(session)
        for session in expired:
            log.info("preview %s expired (idle or stopped)", session.run_id)
            self._terminate(session)
        return len(expired)


def _probe_paths(source: Path) -> list[str]:
    """Which paths to try, in order, when checking whether the app is up."""
    spec_file = source / "spec.yaml"
    candidates: list[str] = []
    if spec_file.exists():
        try:
            from agents.introspect import spec_paths

            _pairs, paths = spec_paths(spec_file.read_text())
            candidates = sorted(p for p in paths if p != "/health")
        except Exception:
            candidates = []
    for fallback in ("/health", "/"):
        if fallback not in candidates:
            candidates.append(fallback)
    return candidates[:3]


_service: PlaygroundService | None = None


def get_playground() -> PlaygroundService:
    """Process-wide service, created on first use and shut down by the lifespan."""
    global _service
    if _service is None:
        _service = PlaygroundService()
        _service.start_reaper()
    return _service


def reset_playground() -> None:
    """Test hook: drop the process-wide instance."""
    global _service
    if _service is not None:
        _service.shutdown()
    _service = None
