"""Latency harness: measure the pipeline before claiming to have sped it up.

Run the same requirement *N* times and report, per stage:

* ``p50`` / ``p90`` wall time -- the claim under test
* LLM calls and LLM seconds -- where the time actually goes
* sandbox seconds -- pytest, the boot check and static analysis
* dev retries -- the control: a "speedup" that skips work shows up here
* per-role token usage, next to the ceiling that role was allowed to spend

Those last two are why this file exists rather than a stopwatch around
``run_team``. Wall time alone cannot distinguish "the pipeline got faster"
from "the pipeline decided to do less": a run that fails its tests on the first
attempt and gives up is very fast. ``attempts_dev`` and ``spec_coverage`` are
the falsifiers, and every report carries them next to the number.

Usage
-----
    python backend/evals/perf.py                     # 3 runs of task-manager
    python backend/evals/perf.py --repeats 5 --all
    python backend/evals/perf.py --compare backend/evals/perf_baseline.json
    python backend/evals/perf.py --write backend/evals/perf_baseline.json

Requires a reachable LLM; this is not an offline test.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import statistics
import sys
import time
from collections import defaultdict
from functools import wraps
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from evals.metrics import summarize_run
from sandbox import runner as Runner

BACKEND = Path(__file__).resolve().parent.parent
SUITE = BACKEND / "evals" / "requirements_suite.json"
DEFAULT_BASELINE = BACKEND / "evals" / "perf_baseline.json"
OUT_ROOT = Path("outputs/perf")

#: Nodes that bracket a stage. ``progress`` events are informational and
#: ``start``/``done`` pairs are what a stage's duration is measured from.
_TERMINAL = ("done", "error")


# --------------------------------------------------------------------------
# sandbox timing
# --------------------------------------------------------------------------


class SandboxClock:
    """Wall time and call counts for the runner's three subprocess entry points.

    Instrumented by wrapping the module functions rather than by having the
    workflow report timings: pytest, the boot check and static analysis are
    called from three different places, and a wrapper catches all of them
    without any of those call sites knowing this exists. It measures the same
    work whether the sandbox is a local subprocess or a container, so a Phase 2
    or Phase 3 baseline is comparable with a Phase 1 one.
    """

    def __init__(self) -> None:
        self.seconds = 0.0
        self.calls: dict[str, int] = defaultdict(int)

    def install(self) -> None:
        for name in ("run_tests", "boot_check", "static_analysis"):
            original = getattr(Runner, name)
            setattr(Runner, name, self._wrap(name, original))

    def _wrap(self, name: str, fn):
        @wraps(fn)
        def timed(*args, **kwargs):
            start = time.perf_counter()
            try:
                return fn(*args, **kwargs)
            finally:
                self.seconds += time.perf_counter() - start
                self.calls[name] += 1

        return timed

    def report(self) -> dict[str, Any]:
        return {
            "seconds": round(self.seconds, 2),
            "calls": dict(sorted(self.calls.items())),
        }


# --------------------------------------------------------------------------
# per-run instrumentation
# --------------------------------------------------------------------------


class RunTrace:
    """Node durations and LLM usage for one ``run_team`` call."""

    def __init__(self) -> None:
        self.started: dict[str, float] = {}
        self.node_seconds: dict[str, float] = defaultdict(float)
        self.events = 0

    def on_event(self, node: str, phase: str, payload: dict | None = None) -> None:
        self.events += 1
        now = time.perf_counter()
        if phase == "start":
            # A node that restarts without a matching done (a boot-failure
            # repair) re-enters here; close the open interval so its time is
            # still attributed.
            if node in self.started:
                self.node_seconds[node] += now - self.started.pop(node)
            self.started[node] = now
        elif phase in _TERMINAL:
            if node in self.started:
                self.node_seconds[node] += now - self.started.pop(node)

    def finish(self) -> None:
        """Close any interval still open, so a run's total is its nodes' sum."""
        now = time.perf_counter()
        for node, started in list(self.started.items()):
            self.node_seconds[node] += now - started
        self.started.clear()

    def stages(self) -> dict[str, float]:
        return {k: round(v, 2) for k, v in sorted(self.node_seconds.items(), key=lambda kv: -kv[1])}


def _log_path() -> Path:
    """Where the backends append their call log. Mirrors ``agents.llm._log_path``.

    Read from the environment rather than imported, so a benchmark run under
    ``RUNS_DIR`` measures that run's calls instead of a different log's.
    """
    return Path(os.getenv("RUNS_DIR", "") or "outputs") / "llm_log.jsonl"


def _read_log(log_path: Path, start: int) -> list[dict]:
    """The LLM call-log entries appended since byte ``start``.

    The log is process-wide and every run appends to it, so one run's calls are
    identified by byte offset rather than by a run id the backends do not
    record. Reading from the offset also survives a log that was rotated or
    truncated mid-benchmark, which a line count would not.
    """
    entries: list[dict] = []
    try:
        with open(log_path) as f:
            f.seek(start)
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        pass
    return entries


def _llm_usage(entries: list[dict]) -> dict[str, Any]:
    """Calls, seconds, per-role spend and truncation count for a set of calls."""
    by_role: dict[str, dict] = {}
    for entry in entries:
        bucket = by_role.setdefault(
            entry.get("role", "?"),
            {
                "calls": 0,
                "seconds": 0.0,
                # The ceiling this role was actually granted, per call. A role
                # whose granted ceiling dwarfs its usage is exactly the defect
                # Phase 1 fixed; recording only one of the two hides it.
                "max_output_tokens": entry.get("max_output_tokens"),
                "tokens_in": 0,
                "tokens_out": 0,
                "reasoning_tokens": 0,
                "reported_usage": False,
            },
        )
        bucket["calls"] += 1
        bucket["seconds"] = round(bucket["seconds"] + float(entry.get("latency") or 0), 2)
        usage = entry.get("usage") or {}
        if isinstance(usage, dict) and usage:
            bucket["reported_usage"] = True
            bucket["tokens_in"] += int(usage.get("prompt") or 0)
            bucket["tokens_out"] += int(usage.get("completion") or 0)
            bucket["reasoning_tokens"] += int(usage.get("reasoning") or 0)
    return {
        "calls": len(entries),
        "seconds": round(sum(float(e.get("latency") or 0) for e in entries), 2),
        "by_role": dict(sorted(by_role.items())),
        # Truncation is a cap that was too small, and it is the risk Phase 1
        # traded latency for. Counted so a regression is visible rather than
        # inferred from a lower pass rate.
        "truncations": sum(
            1 for e in entries if any("truncated at" in f for f in (e.get("failures") or []))
        ),
    }


# --------------------------------------------------------------------------
# one benchmark run
# --------------------------------------------------------------------------


def run_once(requirement: str, out_dir: Path, want_ui: bool = False) -> dict[str, Any]:
    """One full pipeline run, measured end to end."""
    from graph.workflow import run_team

    log_path = _log_path()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_start = log_path.stat().st_size if log_path.exists() else 0

    trace = RunTrace()
    clock = SandboxClock()
    clock.install()
    try:
        started = time.perf_counter()
        state = run_team(
            requirement,
            out_dir=str(out_dir),
            want_ui=want_ui,
            on_event=trace.on_event,
        )
        wall = time.perf_counter() - started
    finally:
        trace.finish()

    llm = _llm_usage(_read_log(log_path, log_start))
    quality = summarize_run(str(out_dir))
    return {
        "out_dir": str(out_dir),
        "status": getattr(state, "status", None),
        "error": getattr(state, "error", None),
        "wall_seconds": round(wall, 2),
        "events": trace.events,
        "stages": trace.stages(),
        "sandbox": clock.report(),
        "llm": llm,
        # The controls. A faster run that also produced a worse app is not
        # faster, it is cheaper.
        "attempts_dev": getattr(state, "retry_dev", quality.get("attempts_to_green", 0)),
        "attempts_test": getattr(state, "retry_test", 0),
        "spec_coverage": quality.get("spec_coverage"),
        "test_pass_rate": quality.get("test_pass_rate"),
        "truncations": llm["truncations"],
    }


# --------------------------------------------------------------------------
# aggregation
# --------------------------------------------------------------------------


def _pct(values: list[float], q: float) -> float | None:
    """Nearest-rank percentile. ``None`` rather than 0 for an empty sample."""
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, round(q * (len(ordered) - 1))))
    return round(ordered[index], 2)


def _merge_roles(runs: list[dict]) -> dict[str, dict]:
    merged: dict[str, dict] = {}
    for run in runs:
        for role, bucket in (run.get("llm", {}).get("by_role") or {}).items():
            acc = merged.setdefault(
                role,
                {
                    "calls": 0,
                    "seconds": 0.0,
                    "max_output_tokens": bucket.get("max_output_tokens"),
                    "tokens_in": 0,
                    "tokens_out": 0,
                    "reasoning_tokens": 0,
                },
            )
            acc["calls"] += bucket.get("calls", 0)
            acc["seconds"] = round(acc["seconds"] + bucket.get("seconds", 0.0), 2)
            for key in ("tokens_in", "tokens_out", "reasoning_tokens"):
                acc[key] += bucket.get(key, 0)
    return dict(sorted(merged.items(), key=lambda kv: -kv[1]["seconds"]))


def _merge_stages(runs: list[dict]) -> dict[str, dict]:
    merged: dict[str, dict] = {}
    for run in runs:
        for node, seconds in (run.get("stages") or {}).items():
            acc = merged.setdefault(node, {"seconds": 0.0, "seen": 0})
            acc["seconds"] = round(acc["seconds"] + seconds, 2)
            acc["seen"] += 1
    return {
        node: {
            "seconds": acc["seconds"],
            "mean": round(acc["seconds"] / max(1, acc["seen"]), 2),
            "seen": acc["seen"],
        }
        for node, acc in sorted(merged.items(), key=lambda kv: -kv[1]["seconds"])
    }


def _merge_calls(runs: list[dict]) -> dict[str, dict]:
    """Sandbox call counts per runner entry point, averaged over runs."""
    merged: dict[str, dict] = {}
    for run in runs:
        for name, count in (run.get("sandbox", {}).get("calls") or {}).items():
            acc = merged.setdefault(name, {"total": 0, "runs": 0})
            acc["total"] += count
            acc["runs"] += 1
    return {
        name: {"total": acc["total"], "per_run": round(acc["total"] / max(1, acc["runs"]), 2)}
        for name, acc in sorted(merged.items())
    }


def aggregate(runs: list[dict]) -> dict[str, Any]:
    """Roll per-run measurements up to the report the plan's gate is stated in."""
    walls = [r["wall_seconds"] for r in runs]
    llm_seconds = [r["llm"]["seconds"] for r in runs]
    sandbox_seconds = [r["sandbox"]["seconds"] for r in runs]
    coverage = [r["spec_coverage"] for r in runs if r.get("spec_coverage") is not None]
    return {
        "runs": len(runs),
        "statuses": sorted({r.get("status") or "?" for r in runs}),
        "wall_seconds": {
            "p50": _pct(walls, 0.50),
            "p90": _pct(walls, 0.90),
            "min": round(min(walls), 2) if walls else None,
            "max": round(max(walls), 2) if walls else None,
            "mean": round(statistics.fmean(walls), 2) if walls else None,
            "all": walls,
        },
        "llm": {
            "calls_total": sum(r["llm"]["calls"] for r in runs),
            "calls_p50": _pct([float(r["llm"]["calls"]) for r in runs], 0.50),
            "seconds_p50": _pct(llm_seconds, 0.50),
            "seconds_total": round(sum(llm_seconds), 2),
            "truncations": sum(r.get("truncations", 0) for r in runs),
            "by_role": _merge_roles(runs),
        },
        "sandbox": {
            "seconds_p50": _pct(sandbox_seconds, 0.50),
            "seconds_total": round(sum(sandbox_seconds), 2),
            "calls": _merge_calls(runs),
        },
        "stages": _merge_stages(runs),
        # ---- controls: a speedup that changes these is a behaviour change ----
        "dev_retries": {
            "p50": _pct([float(r["attempts_dev"]) for r in runs], 0.50),
            "all": [r["attempts_dev"] for r in runs],
        },
        "spec_coverage": {
            "p50": _pct([float(c) for c in coverage], 0.50),
            "all": coverage,
        },
    }


# --------------------------------------------------------------------------
# comparison against a baseline
# --------------------------------------------------------------------------


def compare(baseline: dict, current: dict) -> dict[str, Any]:
    """Diff two aggregates, flagging any control that moved.

    Only ``wall_seconds`` is allowed to move in the Phase 1 direction. A change
    in ``dev_retries`` or ``spec_coverage`` means the pipeline is reaching a
    different answer, and the latency number means nothing.
    """
    b = baseline.get("aggregate", {})
    c = current.get("aggregate", {})

    def get(section: str, field: str, src: dict | None = None):
        return ((src if src is not None else b).get(section) or {}).get(field)

    def delta(before, after):
        if before is None or after is None:
            return None
        return round(after - before, 2)

    def moved(before, after, tol: float = 1e-9):
        if before is None or after is None:
            return False
        return abs(float(after) - float(before)) > tol

    b_wall, c_wall = get("wall_seconds", "p50"), get("wall_seconds", "p50", c)
    b_dev, c_dev = get("dev_retries", "p50"), get("dev_retries", "p50", c)
    b_cov, c_cov = get("spec_coverage", "p50"), get("spec_coverage", "p50", c)

    speedup = None
    if b_wall:
        speedup = round((b_wall - c_wall) / b_wall * 100, 1)
    if b_wall is None or c_wall is None:
        verdict = "no comparable wall-time baseline"
    elif moved(b_dev, c_dev) or moved(b_cov, c_cov):
        # Faster *and* a different answer: the run got cheaper, not quicker.
        verdict = (
            "REGRESSION: control moved -- "
            f"dev_retries {b_dev} -> {c_dev}, spec_coverage {b_cov} -> {c_cov}"
        )
    elif speedup is not None and speedup > 0:
        verdict = f"faster: p50 wall -{speedup}% with dev_retries and spec_coverage unchanged"
    else:
        verdict = f"not faster: p50 wall change {speedup}% with controls unchanged"

    return {
        "wall_p50": {"baseline": b_wall, "current": c_wall, "delta": delta(b_wall, c_wall)},
        "llm_calls_p50": {
            "baseline": get("llm", "calls_p50"),
            "current": get("llm", "calls_p50", c),
        },
        "llm_seconds_p50": {
            "baseline": get("llm", "seconds_p50"),
            "current": get("llm", "seconds_p50", c),
        },
        "sandbox_seconds_p50": {
            "baseline": get("sandbox", "seconds_p50"),
            "current": get("sandbox", "seconds_p50", c),
        },
        "controls": {
            "dev_retries": {"baseline": b_dev, "current": c_dev},
            "spec_coverage": {"baseline": b_cov, "current": c_cov},
        },
        "truncations": {
            "baseline": get("llm", "truncations"),
            "current": get("llm", "truncations", c),
        },
        "speedup_pct": speedup,
        "verdict": verdict,
    }


# --------------------------------------------------------------------------
# reporting
# --------------------------------------------------------------------------


def _print_report(report: dict) -> None:
    agg = report["aggregate"]
    print(f"\n=== {report['requirement_id']} x{agg['runs']} ===")
    print(f"statuses: {', '.join(agg['statuses'])}")
    wall = agg["wall_seconds"]
    print(
        f"wall   p50={wall['p50']}s  p90={wall['p90']}s  "
        f"min={wall['min']}s  max={wall['max']}s  all={wall['all']}"
    )
    print(
        f"llm    calls p50={agg['llm']['calls_p50']}  seconds p50={agg['llm']['seconds_p50']}s  "
        f"truncations={agg['llm']['truncations']}"
    )
    print(
        f"sandbox seconds p50={agg['sandbox']['seconds_p50']}s  "
        f"calls={agg['sandbox']['calls']}"
    )
    print("per-stage wall seconds (mean):")
    for node, s in list(agg["stages"].items())[:10]:
        print(f"  {node:<14} {s['mean']:>7}s  (seen {s['seen']}x)")
    print("per-role LLM:")
    for role, s in agg["llm"]["by_role"].items():
        print(
            f"  {role:<12} calls={s['calls']:<3} seconds={s['seconds']:<7} "
            f"ceiling={s['max_output_tokens']} in={s['tokens_in']} out={s['tokens_out']}"
        )
    print(
        f"controls: dev_retries={agg['dev_retries']['all']}  "
        f"spec_coverage={agg['spec_coverage']['all']}"
    )
    if "comparison" in report:
        cmp = report["comparison"]
        print(f"\nvs baseline: {cmp['verdict']}")
        print(
            f"  wall p50 {cmp['wall_p50']['baseline']} -> {cmp['wall_p50']['current']}  |  "
            f"llm calls {cmp['llm_calls_p50']['baseline']} -> {cmp['llm_calls_p50']['current']}  |  "
            f"llm seconds {cmp['llm_seconds_p50']['baseline']} -> {cmp['llm_seconds_p50']['current']}"
        )
        print(
            f"  controls: dev_retries {cmp['controls']['dev_retries']['baseline']} -> "
            f"{cmp['controls']['dev_retries']['current']}, spec_coverage "
            f"{cmp['controls']['spec_coverage']['baseline']} -> "
            f"{cmp['controls']['spec_coverage']['current']}"
        )


# --------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--repeats", type=int, default=3, help="identical runs per requirement")
    ap.add_argument("--requirement-id", default="task-manager")
    ap.add_argument("--all", action="store_true", help="every requirement in the suite")
    ap.add_argument("--out", default=str(OUT_ROOT))
    ap.add_argument("--write", nargs="?", const=str(DEFAULT_BASELINE), help="write the report here")
    ap.add_argument("--compare", nargs="?", const=str(DEFAULT_BASELINE))
    ap.add_argument(
        "--purge",
        action="store_true",
        help="delete each run's output directory after it is summarised",
    )
    args = ap.parse_args()

    suite = json.loads(SUITE.read_text())
    selected = suite if args.all else [s for s in suite if s["id"] == args.requirement_id]
    if not selected:
        print(f"no requirement '{args.requirement_id}' in the suite", file=sys.stderr)
        return 2

    reports: list[dict] = []
    for item in selected:
        for i in range(max(1, args.repeats)):
            out_dir = Path(args.out) / f"{item['id']}_{i}"
            print(f"[perf] {item['id']} run {i + 1}/{args.repeats} -> {out_dir}", flush=True)
            run = run_once(item["requirement"], out_dir, want_ui=bool(item.get("ui")))
            reports.append(run)
            print(
                f"[perf]   status={run['status']} wall={run['wall_seconds']}s "
                f"llm={run['llm']['calls']} calls sandbox={run['sandbox']['seconds']}s",
                flush=True,
            )
            if args.purge:
                shutil.rmtree(out_dir, ignore_errors=True)

    agg = aggregate(reports)
    report = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "requirement_id": selected[0]["id"] if len(selected) == 1 else "multiple",
        "repeats": args.repeats,
        "sandbox_mode": os.getenv("SANDBOX_MODE", "unset"),
        "llm_provider": os.getenv("LLM_PROVIDER", "auto"),
        "runs": reports,
        "aggregate": agg,
    }

    baseline_path = Path(args.compare) if args.compare else None
    if baseline_path and baseline_path.exists():
        report["comparison"] = compare(json.loads(baseline_path.read_text()), report)
    elif baseline_path:
        print(f"baseline {baseline_path} does not exist; skipping comparison", file=sys.stderr)

    _print_report(report)

    if args.write:
        path = Path(args.write)
        path.write_text(json.dumps(report, indent=2) + "\n")
        print(f"\nwrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
