"""Single-agent baseline (Self-collaboration paper baseline the team must beat)."""
from __future__ import annotations
import json
import sys
import time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from agents.developer import _template_bundle
from agents.tester import TEMPLATE_TESTS
from schemas.messages import TestSuite
from sandbox.runner import run_tests

def main():
    out = Path("outputs/baseline")
    out.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    code = _template_bundle()
    tests = TestSuite(files={"test_tasks.py": TEMPLATE_TESTS})
    for rel, c in code.files.items():
        p = out / "code" / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(c)
    (out / "tests" / "test_tasks.py").parent.mkdir(parents=True, exist_ok=True)
    (out / "tests" / "test_tasks.py").write_text(TEMPLATE_TESTS)
    report = run_tests(code, tests)
    result = {"passed": report.passed, "failed": report.failed,
              "wall_time": round(time.time() - t0, 1), "raw_tail": report.raw[-1000:]}
    (out / "result.json").write_text(json.dumps(result, indent=2))
    print(json.dumps(result, indent=2))

if __name__ == "__main__":
    main()
