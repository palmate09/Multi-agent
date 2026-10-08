"""Single-agent baseline: one model, one shot, no team and no fix loop.

This is the number the multi-agent pipeline is supposed to beat. It shares the
Developer's prompts but gets no Designer, no independent Tester, no triage, no
reflexion and no reviewer — one call generates code and tests together, then
pytest runs once and that is the result.

It previously imported the Developer's template bundle, which made it a
re-run of the template rather than a measurement of anything.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agents.blocks import parse_file_blocks
from agents.introspect import find_app_object
from agents.llm import GenerationError, require
from sandbox.runner import run_tests
from schemas.messages import CodeBundle, TestSuite

SOP = (
    "You are a single developer building a FastAPI + SQLAlchemy + SQLite REST API.\n"
    "In ONE response produce the application AND its pytest suite.\n"
    "Reply with file blocks only, in this format:\n"
    "### <filename>\n```python\n<complete code>\n```\n"
    "Rules: one module may hold everything for a small API; expose the app as "
    "`app = FastAPI()`; import every symbol you use; tests must import your "
    "entrypoint module and use TestClient, never their own FastAPI()."
)


def run_once(requirement: str) -> tuple[CodeBundle, TestSuite]:
    """One generation, no iteration."""
    text, _meta = require(
        f"Requirement:\n{requirement}\n\nEmit the application and its tests.", SOP, role="developer"
    )
    files = parse_file_blocks(text)
    tests = {n: v for n, v in files.items() if n.startswith("test_")}
    code = {n: v for n, v in files.items() if not n.startswith("test_")}
    if not code or not tests:
        raise GenerationError(f"baseline produced code={sorted(code)} tests={sorted(tests)}")
    module, attr = find_app_object(code) or ("", "app")
    return (
        CodeBundle(files=code, entry_module=module, entry_attr=attr),
        TestSuite(files=tests),
    )


def main(requirement: str = "", out: str = "outputs/baseline") -> dict:
    requirement = requirement or (
        "Build a REST API for managing tasks with SQLite persistence. "
        "Endpoints: GET /health, CRUD /tasks with title validation "
        "(422 on empty), 404 on unknown id."
    )
    out_dir = Path(out)
    out_dir.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    result: dict = {"requirement": requirement}
    try:
        code, tests = run_once(requirement)
    except GenerationError as exc:
        result.update({"status": "blocked", "error": str(exc), "passed": 0, "failed": 0})
        (out_dir / "result.json").write_text(json.dumps(result, indent=2))
        print(json.dumps(result, indent=2))
        return result

    for rel, c in code.files.items():
        p = out_dir / "code" / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(c)
    for rel, c in tests.files.items():
        p = out_dir / "tests" / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(c)

    report = run_tests(code, tests)
    result.update(
        {
            "status": "green" if report.ok else "red",
            "passed": report.passed,
            "failed": report.failed,
            "attempted": 1,
            "wall_time": round(time.time() - t0, 1),
            "raw_tail": report.raw[-1500:],
        }
    )
    (out_dir / "result.json").write_text(json.dumps(result, indent=2))
    print(json.dumps(result, indent=2))
    return result


if __name__ == "__main__":
    main()
