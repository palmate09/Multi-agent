"""Metrics for evaluation.

``spec_coverage`` used to count three hardcoded substrings, which is why the
filter and notes variants all scored 1.0 while silently omitting the feature
they were named for. It is now a real comparison between the endpoints the spec
declares and the routes the generated code exposes.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agents.introspect import all_routes, normalise_path, spec_paths

ACCEPTED = ("accepted",)


def _load(path: Path, default):
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def summarize_run(run_dir: str) -> dict:
    """Summarise one run directory."""
    d = Path(run_dir)
    s = _load(d / "summary.json", {})
    spec_yaml = ""
    spec_file = d / "spec.yaml"
    if spec_file.exists():
        spec_yaml = spec_file.read_text()

    spec_pairs, _paths = spec_paths(spec_yaml)
    coverage = 0.0
    implemented = 0
    if spec_pairs:
        code_files: dict[str, str] = {}
        code_root = d / "code"
        if code_root.exists():
            code_files = {
                str(p.relative_to(code_root)): p.read_text()
                for p in sorted(code_root.rglob("*"))
                if p.is_file() and p.suffix == ".py"
            }
        routes = {normalise_path(p) for p, _ in all_routes(code_files)}
        implemented = sum(1 for p, _ in spec_pairs if normalise_path(p) in routes)
        coverage = implemented / len(spec_pairs)

    passed = s.get("tests_passed", 0)
    failed = s.get("tests_failed", 0)
    return {
        "run": d.name,
        "status": s.get("status"),
        "error": s.get("error"),
        "test_pass_rate": passed / max(1, passed + failed),
        "spec_coverage": round(coverage, 3),
        "spec_endpoints": len(spec_pairs),
        "implemented_endpoints": implemented,
        "attempts_to_green": s.get("attempts_dev", 0),
        "review_rounds": s.get("review_rounds", 0),
        "files": s.get("files", []),
        "entrypoint": s.get("entrypoint"),
        "wall_time": s.get("wall_time"),
    }


def tokens_from_log(log: str = "outputs/llm_log.jsonl") -> dict:
    p = Path(log)
    if not p.exists():
        return {"calls": 0}
    entries = []
    for line in p.read_text(errors="replace").splitlines():
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    by_model: dict[str, int] = {}
    for e in entries:
        by_model[e.get("model", "?")] = by_model.get(e.get("model", "?"), 0) + 1
    failed = sum(1 for e in entries if not e.get("response"))
    return {
        "calls": len(entries),
        "by_model": by_model,
        "empty_responses": failed,
        "total_latency": round(sum(e.get("latency", 0) for e in entries), 1),
    }
