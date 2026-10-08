"""Evaluation results and agent prompts, for the UI's benchmark panel."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter

router = APIRouter(prefix="/api/evals", tags=["evals"])

SUITE_PATH = Path(__file__).resolve().parents[2] / "evals" / "requirements_suite.json"
RESULTS_PATH = Path(__file__).resolve().parents[2] / "evals" / "results.json"
TASKS_PATH = Path(__file__).resolve().parents[2] / "evals" / "task-manager.md"


def _load_json(path: Path, fallback):
    if not path.exists():
        return fallback
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        return fallback


@router.get("/results")
def results() -> dict:
    rows = _load_json(RESULTS_PATH, [])
    baseline_path = RESULTS_PATH.parents[2] / "outputs" / "baseline" / "result.json"
    baseline = _load_json(baseline_path, None)
    passed = sum(1 for r in rows if r.get("status", "").startswith("accepted"))
    return {
        "runs": rows,
        "baseline": baseline,
        "total": len(rows),
        "accepted": passed,
        "pass_rate": passed / len(rows) if rows else None,
    }


@router.get("/suite")
def suite() -> dict:
    return {"requirements": _load_json(SUITE_PATH, [])}


@router.get("/reference")
def reference() -> dict:
    """Original brief, rendered as markdown for the UI reference tab."""
    return {"markdown": TASKS_PATH.read_text() if TASKS_PATH.exists() else ""}
