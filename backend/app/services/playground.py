"""Playground: run a generated app as a live subprocess and keep it addressable.

One :class:`PreviewSession` per run. The app is copied to its own directory,
booted with uvicorn on an ephemeral loopback port, and held in a run-id -> port
map that the gateway resolves per request.

What this is **not**: isolation. The child is a normal process on this host
with this host's filesystem, network and CPU. Three things are done about the
obvious foot-guns, and the docs say plainly that they are mitigations rather
than a sandbox:

* every platform credential is stripped from the child environment
  (:data:`STRIPPED_ENV`); generated code and platform keys never share a process
* the server binds ``127.0.0.1``, so nothing outside the host can reach it and
  only the authenticated gateway can proxy to it
* a TTL reaper and a concurrency cap bound how long and how many

Anyone running this on a machine they care about should put a real sandbox
(Docker, a VM, gVisor) in front of it.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from app.config import Settings, get_settings

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

IDLE_STATES = ("stopped", "crashed")


def _free_port() -> int:
    """Bind ``:0``, read the port back, close, return it.

    There is an unavoidable race between closing and uvicorn binding it. It is
    the best available without holding the socket open (which uvicorn would
    then refuse to bind) and the window is a few microseconds on loopback.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def child_env(workdir: Path) -> dict[str, str]:
    """Environment for the generated app: platform keys removed, CWD pinned."""
    env = {k: v for k, v in os.environ.items() if k not in STRIPPED_ENV and "API_KEY" not in k}
    env.update(
        {
            # Tells generated code it is being previewed, not deployed.
            "PREVIEW": "1",
            "PYTHONUNBUFFERED": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            # Any SQLite path the generated code builds from its own file name
            # has to land here, not in the run directory the pipeline reads.
            "PREVIEW_DIR": str(workdir),
            "SQLITE_PATH": str(workdir / "preview.db"),
            "DATABASE_URL": f"sqlite:///{workdir / 'preview.db'}",
        }
    )
    return env


@dataclass
class PreviewSession:
    """One running (or stopped) preview."""

    run_id: str
    port: int
    workdir: Path
    log_path: Path
    run_dir: Path
    started_at: float = 0.0
    last_hit: float = field(default_factory=time.time)
    state: str = "starting"
    error: str = ""
    entry: str = ""
    proc: subprocess.Popen | None = None
    #: Sticky: True once the app has answered a health probe at least once.
    #: ``state`` alone cannot answer "did this app ever boot?", because
    #: stopping a preview resets it to "stopped" — and an eval that reads the
    #: file afterwards must not score a successful boot as a failure.
    ever_booted: bool = False

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def log_tail(self, lines: int = 40) -> str:
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
            "started_at": self.started_at or None,
            "idle_seconds": idle,
            "error": self.error or None,
            "ever_booted": self.ever_booted,
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
                "started_at": None,
                "idle_seconds": 0,
                "error": None,
                "ever_booted": False,
                "log_tail": "",
            }
        # A process that died under us (a review patch rewrote its files, an
        # OOM kill) must not keep reporting "running".
        if session.state == "running" and not session.alive():
            session.state = "crashed"
            session.error = "the preview process exited"
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
            port=_free_port(),
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

        self._install_requirements(workdir)
        try:
            self._spawn(session, entry_module, entry_attr)
        except Exception:
            with self._lock:
                self._sessions.pop(run_id, None)
            raise

        session.error = ""
        if self._await_health(session, source):
            session.state = "running"
            session.ever_booted = True
            log.info("preview %s running on 127.0.0.1:%s (%s)", run_id, session.port, session.entry)
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

    def _install_requirements(self, workdir: Path) -> None:
        """Best-effort ``pip install`` of the generated app's dependencies.

        Capped and never fatal: the platform venv already has FastAPI,
        SQLAlchemy and uvicorn, so the common case needs no install at all. A
        dependency the sandbox cannot fetch (no network) leaves the app
        crashing on import, which the health probe then reports honestly.
        """
        req = workdir / "requirements.txt"
        if not req.is_file() or not req.read_text(errors="replace").strip():
            return
        try:
            subprocess.run(
                [sys.executable, "-m", "pip", "install", "--quiet", "-r", str(req)],
                cwd=workdir,
                env=child_env(workdir),
                capture_output=True,
                timeout=60,
                check=False,
            )
        except Exception as exc:
            log.info("preview dependency install skipped: %s", exc)

    def _spawn(self, session: PreviewSession, module: str, attr: str) -> None:
        session.entry = f"{module}:{attr}"
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
        """
        candidates = _probe_paths(source)
        deadline = time.time() + max(5, self.settings.preview_boot_timeout_seconds)
        while time.time() < deadline:
            if not session.alive():
                return False
            for candidate in candidates:
                url = f"http://127.0.0.1:{session.port}{candidate}"
                try:
                    if httpx.get(url, timeout=2.0, follow_redirects=False).status_code == 200:
                        return True
                except Exception:
                    continue
            time.sleep(0.25)
        return False

    def _terminate(self, session: PreviewSession) -> None:
        proc = session.proc
        session.state = "stopped"
        session.proc = None
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
        while not self._stop.wait(15.0):
            self._reap_once()

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
