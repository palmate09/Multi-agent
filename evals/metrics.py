"""Metrics for Phase 5 evaluation."""
from __future__ import annotations
import json
from pathlib import Path

def summarize_run(run_dir: str) -> dict:
    d = Path(run_dir)
    s = json.loads((d / "summary.json").read_text()) if (d / "summary.json").exists() else {}
    spec = (d / "spec.yaml").read_text() if (d / "spec.yaml").exists() else ""
    has_health = "/health" in spec
    has_list = "/tasks" in spec
    has_detail = "/tasks/{id}" in spec or "/tasks/{task_id}" in spec
    endpoints = [x for x in [has_health, has_list, has_detail] if x]
    return {
        "run": d.name,
        "status": s.get("status"),
        "test_pass_rate": (s.get("tests_passed", 0) / max(1, s.get("tests_passed", 0) + s.get("tests_failed", 0))),
        "spec_coverage": len(endpoints) / 3,
        "attempts_to_green": s.get("attempts_dev", 0),
        "wall_time": s.get("wall_time"),
    }

def tokens_from_log(log: str = "outputs/llm_log.jsonl") -> dict:
    p = Path(log)
    if not p.exists():
        return {"calls": 0}
    calls = p.read_text().strip().splitlines()
    return {"calls": len(calls)}
