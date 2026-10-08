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
from graph.workflow import NODES, run_team

log = logging.getLogger("app.runs")

_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_SAFE_SEGMENT = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]{0,63}$")
MAX_EVENTS = 500


def _now() -> str:
    return datetime.now(UTC).isoformat()


class Run:
    def __init__(self, run_id: str, requirement: str, run_dir: Path, options: dict[str, Any]):
        self.run_id = run_id
        self.requirement = requirement
        self.run_dir = run_dir
        self.options = options
        self.status = "queued"
        self.created_at = _now()
        self.updated_at = self.created_at
        self.error: str | None = None
        self.events: deque[dict[str, Any]] = deque(maxlen=MAX_EVENTS)
        self.state: Any = None
        self._cond = threading.Condition()
        self._finished = False

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
            data["memory"] = list(st.memory)
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
            "memory": [],
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
        refl = self.run_dir / "reflections.json"
        if refl.exists():
            with contextlib.suppress(json.JSONDecodeError):
                out["memory"] = json.loads(refl.read_text())
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
        with self._lock:
            run = self._runs.pop(run_id, None)
        if not run:
            return False
        import shutil

        shutil.rmtree(run.run_dir, ignore_errors=True)
        return True

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)

    # -- execution ------------------------------------------------------
    def submit(self, run: Run) -> None:
        with self._lock:
            self._active += 1
        self._pool.submit(self._execute, run)

    def _execute(self, run: Run) -> None:
        run.status = "running"
        run.updated_at = _now()
        run.emit("pipeline", "start", {"run_id": run.run_id, "nodes": list(NODES)})
        try:
            run.state = run_team(
                run.requirement,
                out_dir=str(run.run_dir),
                use_docker=bool(run.options.get("use_docker", self.settings.sandbox_use_docker)),
                skip_tester=bool(run.options.get("skip_tester")),
                skip_reviewer=bool(run.options.get("skip_reviewer")),
                on_event=run.emit,
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
