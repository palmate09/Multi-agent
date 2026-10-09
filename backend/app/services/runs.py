"""Run registry, background execution and event fan-out for SSE.

Each run executes on a worker thread because `run_team` is a blocking,
LLM-and-subprocess-bound pipeline. Events are buffered per run so a client that
connects after the run started still receives the full history.
"""

from __future__ import annotations

import contextlib
import json
import logging
import re
import threading
import time
import uuid
from collections import deque
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.config import Settings, get_settings
from graph.workflow import NODES, reconstruct_state, run_team

log = logging.getLogger("app.runs")

_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_SAFE_SEGMENT = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]{0,63}$")
MAX_EVENTS = 500

#: Run statuses that still hold a worker thread or pool slot.
ACTIVE_STATUSES = ("queued", "running", "stopping")

#: Terminal statuses a resume may continue from. Accepted runs have nothing
#: to continue; active runs must be stopped (or restarted) first.
RESUMABLE_STATUSES = (
    "cancelled",
    "unresolved",
    "unresolved_review",
    "failed",
    "blocked",
    "domain_missed",
)


class RunningError(RuntimeError):
    """A lifecycle operation refused because the run is still active."""

    def __init__(self, run_id: str, status: str):
        super().__init__(f"run '{run_id}' is {status}; stop it first")
        self.run_id = run_id
        self.status = status


def _now() -> str:
    return datetime.now(UTC).isoformat()


class Run:
    def __init__(self, run_id: str, requirement: str, run_dir: Path, options: dict[str, Any]):
        self.run_id = run_id
        self.requirement = requirement
        self.run_dir = run_dir
        self.options = options
        self.resumed_from: str | None = options.get("resumed_from")
        self.status = "queued"
        self.created_at = _now()
        self.updated_at = self.created_at
        self.error: str | None = None
        self.events: deque[dict[str, Any]] = deque(maxlen=MAX_EVENTS)
        self.state: Any = None
        self._cond = threading.Condition()
        self._finished = False
        # Cooperative stop: set by stop(), honoured by run_team at node
        # boundaries. Never killed mid-call — threads cannot be safely killed.
        self.cancel_event = threading.Event()
        self.future: Any = None

    # -- event plumbing -------------------------------------------------
    def emit(self, node: str, phase: str, payload: dict[str, Any]) -> None:
        event = {"seq": len(self.events), "node": node, "phase": phase, "ts": _now(), **payload}
        with self._cond:
            self.events.append(event)
            self._cond.notify_all()

    def mark_finished(self) -> None:
        with self._cond:
            self._finished = True
            self._cond.notify_all()

    def stream(self, timeout: float = 0.5) -> Iterator[dict[str, Any]]:
        """Yield buffered events, then live ones, until the run finishes."""
        sent = 0
        while True:
            with self._cond:
                if sent < len(self.events):
                    batch = list(self.events)[sent:]
                    sent = len(self.events)
                else:
                    batch = []
                    if self._finished:
                        return
                    self._cond.wait(timeout)
                    if sent >= len(self.events):
                        continue
                    batch = list(self.events)[sent:]
                    sent = len(self.events)
            yield from batch

    # -- serialisation --------------------------------------------------
    @property
    def summary(self) -> dict[str, Any]:
        data = {
            "run_id": self.run_id,
            "requirement": self.requirement,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "error": self.error,
            "resumed_from": self.resumed_from,
        }
        summary_file = self.run_dir / "summary.json"
        if summary_file.exists():
            try:
                s = json.loads(summary_file.read_text())
                data.update(
                    {
                        "status": s.get("status", self.status),
                        "tests_passed": s.get("tests_passed", 0),
                        "tests_failed": s.get("tests_failed", 0),
                        "attempts_dev": s.get("attempts_dev", 0),
                        "attempts_test": s.get("attempts_test", 0),
                        "review_rounds": s.get("review_rounds", 0),
                        "wall_time": s.get("wall_time"),
                        "entrypoint": s.get("entrypoint"),
                        "files": s.get("files", []),
                        "error": s.get("error") or self.error,
                    }
                )
            except json.JSONDecodeError:
                pass
        return data

    def detail(self) -> dict[str, Any]:
        data = self.summary
        data["events"] = list(self.events)
        data["artifacts"] = self.list_artifacts()
        st = self.state
        if st is not None:
            data["stories"] = st.stories.model_dump() if st.stories else None
            data["spec"] = st.spec.openapi_yaml if st.spec else None
            data["code"] = dict(st.code.files) if st.code else {}
            data["tests"] = dict(st.tests.files) if st.tests else {}
            data["review"] = st.review.model_dump() if st.review else None
            data["report"] = st.report.model_dump() if st.report else None
            data["coverage"] = st.coverage.model_dump() if st.coverage else None
            data["memory"] = list(st.memory)
            data["plan"] = st.plan.model_dump() if st.plan else None
        else:
            data.update(self._artifacts_from_disk())
        return data

    def _artifacts_from_disk(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "stories": None,
            "spec": None,
            "code": {},
            "tests": {},
            "review": None,
            "report": None,
            "coverage": None,
            "memory": [],
            "plan": None,
        }
        if not self.run_dir.exists():
            return out
        stories = self.run_dir / "stories.json"
        if stories.exists():
            with contextlib.suppress(json.JSONDecodeError):
                out["stories"] = json.loads(stories.read_text())
        spec = self.run_dir / "spec.yaml"
        if spec.exists():
            out["spec"] = spec.read_text()
        for sub in ("code", "tests"):
            root = self.run_dir / sub
            if root.exists():
                out[sub] = {
                    str(p.relative_to(root)): p.read_text()
                    for p in sorted(root.rglob("*"))
                    if p.is_file()
                }
        review = self.run_dir / "review.json"
        if review.exists():
            with contextlib.suppress(json.JSONDecodeError):
                out["review"] = json.loads(review.read_text())
        reports = sorted(self.run_dir.glob("report_try*.json"))
        if reports:
            with contextlib.suppress(json.JSONDecodeError):
                out["report"] = json.loads(reports[-1].read_text())
        cov = self.run_dir / "coverage.json"
        if cov.exists():
            with contextlib.suppress(json.JSONDecodeError):
                out["coverage"] = json.loads(cov.read_text())
        refl = self.run_dir / "reflections.json"
        if refl.exists():
            with contextlib.suppress(json.JSONDecodeError):
                out["memory"] = json.loads(refl.read_text())
        plan = self.run_dir / "reasoning.json"
        if plan.exists():
            with contextlib.suppress(json.JSONDecodeError):
                out["plan"] = json.loads(plan.read_text())
        return out

    def list_artifacts(self) -> list[str]:
        if not self.run_dir.exists():
            return []
        return sorted(
            str(p.relative_to(self.run_dir)) for p in self.run_dir.rglob("*") if p.is_file()
        )

    def read_artifact(self, rel: str) -> tuple[bytes, str] | None:
        """Safely read an artifact, rejecting any path that escapes run_dir."""
        if not rel or rel.startswith("."):
            return None
        segments = rel.split("/")
        if any(not _SAFE_SEGMENT.match(s) for s in segments):
            return None
        target = (self.run_dir / rel).resolve()
        try:
            target.relative_to(self.run_dir.resolve())
        except ValueError:
            return None
        if not target.is_file():
            return None
        media = "text/plain"
        if target.suffix in {".yaml", ".yml"}:
            media = "application/yaml"
        elif target.suffix == ".json":
            media = "application/json"
        elif target.suffix == ".py":
            media = "text/x-python"
        return target.read_bytes(), media


class RunStore:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()
        self.settings.runs_dir.mkdir(parents=True, exist_ok=True)
        self._runs: dict[str, Run] = {}
        self._lock = threading.Lock()
        self._pool = ThreadPoolExecutor(
            max_workers=self.settings.max_concurrent_runs,
            thread_name_prefix="run",
        )
        self._active = 0
        # Reconstructed states waiting for their worker to pick them up
        # (resume). Popped exactly once by _execute; never persisted because
        # the artifacts they came from are the durable record.
        self._resume_states: dict[str, Any] = {}

    # -- lifecycle ------------------------------------------------------
    def new_id(self, requested: str | None) -> str:
        if requested and _SAFE_ID.match(requested):
            candidate = requested
        else:
            slug = re.sub(r"[^a-z0-9]+", "-", (requested or "").lower()).strip("-")[:32]
            stamp = time.strftime("%Y%m%d-%H%M%S")
            candidate = f"{slug or 'run'}-{stamp}"
        with self._lock:
            if candidate in self._runs:
                candidate = f"{candidate}-{uuid.uuid4().hex[:6]}"
            return candidate

    def create(self, requirement: str, run_id: str | None = None, **options: Any) -> Run:
        rid = self.new_id(run_id)
        run = Run(rid, requirement, self.settings.runs_dir / rid, options)
        with self._lock:
            self._runs[rid] = run
        return run

    def get(self, run_id: str) -> Run | None:
        with self._lock:
            return self._runs.get(run_id)

    def list(self) -> list[Run]:
        with self._lock:
            return sorted(self._runs.values(), key=lambda r: r.created_at, reverse=True)

    def active_count(self) -> int:
        with self._lock:
            return self._active

    def delete(self, run_id: str) -> bool:
        """Remove a finished run. Refuses while the worker is alive.

        Deleting under a running thread would orphan it: it keeps its pool
        slot, keeps burning LLM quota, and recreates files in the deleted
        directory. Callers must stop() first.
        """
        with self._lock:
            run = self._runs.get(run_id)
            if run is None:
                return False
            if run.status in ("queued", "running", "stopping"):
                raise RunningError(run_id, run.status)
            del self._runs[run_id]
        import shutil

        shutil.rmtree(run.run_dir, ignore_errors=True)
        return True

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)

    # -- execution ------------------------------------------------------
    def submit(self, run: Run) -> None:
        # Slot reservation and worker handoff are atomic: stop() treats
        # future-is-None as "no worker will ever run", which is only true
        # when submit() has not executed at all.
        with self._lock:
            self._active += 1
            run.future = self._pool.submit(self._execute, run)

    def stop(self, run_id: str) -> dict[str, Any] | None:
        """Request cancellation. Idempotent; safe on finished runs.

        Returns ``{"stopped": True}`` when a live run was signalled,
        ``{"stopped": False, "status": ...}`` when there was nothing to stop,
        and None when the run does not exist. A queued run that has not
        started is unqueued outright so it never occupies a worker; a run
        with no worker at all (stale entry) is settled directly.
        """
        with self._lock:
            run = self._runs.get(run_id)
            if run is None:
                return None
            if run.status not in ACTIVE_STATUSES:
                return {"run_id": run_id, "stopped": False, "status": run.status}
            run.status = "stopping"
            run.updated_at = _now()
            run.cancel_event.set()
            future, finished, never_started = run.future, run._finished, run.future is None
        if never_started or (future is not None and not finished and future.cancel()):
            # No worker will run or finish this run, so settle it here.
            # The slot is released only when submit() reserved one: a
            # never-submitted run never held a slot.
            if not never_started:
                with self._lock:
                    self._active -= 1
            run.status = "cancelled"
            run.error = "stopped by user"
            run.updated_at = _now()
            run.emit("pipeline", "cancelled", {"detail": "stopped before start"})
            run.mark_finished()
        return {"run_id": run_id, "stopped": True, "status": run.status}

    def restart(self, run_id: str, grace: float = 60.0) -> Run | None:
        """Stop if active, delete all data, and start over.

        The old run is removed entirely (directory + registry); the new run
        carries the same requirement and options under a fresh id. Returns
        the new Run, None when the id does not exist. Raises RunningError
        when the worker does not exit within ``grace`` seconds — the old
        data is left untouched in that case.
        """
        with self._lock:
            run = self._runs.get(run_id)
            if run is None:
                return None
            requirement, options = run.requirement, dict(run.options)
            active = run.status in ACTIVE_STATUSES
        if active:
            self.stop(run_id)
            deadline = time.time() + grace
            while time.time() < deadline:
                with self._lock:
                    fut, _done = run.future, run._finished
                if fut is None or fut.done():
                    break
                time.sleep(0.5)
            else:
                raise RunningError(run_id, "stopping")
        # Mint the fresh id BEFORE deleting: ids carry a second-resolution
        # timestamp, so minting after the delete could hand back the same id
        # when both runs fall in the same second. With the old entry still
        # registered, a same-second collision earns a uuid suffix instead.
        fresh_id = self.new_id(None)
        self.delete(run_id)
        new_run = self.create(requirement, run_id=fresh_id, **options)
        self.submit(new_run)
        return new_run

    def resume(self, run_id: str) -> Run | None:
        """Continue a settled run from its saved artifacts under a fresh id.

        Nothing is deleted: the old run stays as history and the new run
        records ``resumed_from``. Phases with artifacts are skipped (see
        ``reconstruct_state``); retry budgets start fresh. Returns the new
        Run, None when the id does not exist. Raises RunningError when the
        run is still active or already accepted.
        """
        with self._lock:
            run = self._runs.get(run_id)
            if run is None:
                return None
            if run.status in ACTIVE_STATUSES:
                raise RunningError(run_id, run.status)
            if run.status not in RESUMABLE_STATUSES:
                raise RunningError(run_id, f"{run.status} (nothing to resume)")
            requirement, options = run.requirement, dict(run.options)
        state = reconstruct_state(run.run_dir)
        state.requirement = requirement or state.requirement
        fresh_id = self.new_id(None)
        new_run = self.create(requirement, run_id=fresh_id, resumed_from=run_id, **options)
        with self._lock:
            self._resume_states[new_run.run_id] = state
        self.submit(new_run)
        return new_run

    def _execute(self, run: Run) -> None:
        run.status = "running"
        run.updated_at = _now()
        run.emit("pipeline", "start", {"run_id": run.run_id, "nodes": list(NODES)})
        try:
            with self._lock:
                resume_state = self._resume_states.pop(run.run_id, None)
            run.state = run_team(
                run.requirement,
                out_dir=str(run.run_dir),
                use_docker=bool(run.options.get("use_docker", self.settings.sandbox_use_docker)),
                skip_tester=bool(run.options.get("skip_tester")),
                skip_reviewer=bool(run.options.get("skip_reviewer")),
                skip_reasoner=bool(run.options.get("skip_reasoner")),
                on_event=run.emit,
                run_id=run.run_id,
                cancel=run.cancel_event,
                resume=resume_state,
            )
            run.status = run.state.status
        except Exception as exc:
            log.exception("run %s failed", run.run_id)
            run.error = f"{type(exc).__name__}: {exc}"
            run.status = "failed"
            run.emit("pipeline", "error", {"error": run.error})
        finally:
            run.updated_at = _now()
            with self._lock:
                self._active -= 1
            run.mark_finished()

    def rehydrate(self) -> int:
        """Rebuild run metadata for directories already on disk (e.g. restart)."""
        root = self.settings.runs_dir
        if not root.exists():
            return 0
        count = 0
        for d in sorted(root.iterdir()):
            if not d.is_dir() or not _SAFE_ID.match(d.name):
                continue
            with self._lock:
                if d.name in self._runs:
                    continue
                requirement = ""
                stories_file = d / "stories.json"
                if stories_file.exists():
                    try:
                        requirement = json.loads(stories_file.read_text()).get("requirement", "")
                    except json.JSONDecodeError:
                        requirement = ""
                run = Run(d.name, requirement or "(restored run)", d, {})
                summary_file = d / "summary.json"
                run.status = "restored"
                if summary_file.exists():
                    with contextlib.suppress(json.JSONDecodeError):
                        run.status = json.loads(summary_file.read_text()).get("status", "restored")
                run.mark_finished()
                self._runs[d.name] = run
            count += 1
        return count
