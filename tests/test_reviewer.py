import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from schemas.messages import CodeBundle, TestSuite
from agents import reviewer as R
from agents.designer import TEMPLATE_SPEC
from schemas.messages import ApiSpec

def test_reviewer_finds_planted_bugs():
    bad = CodeBundle(files={"main.py": "from fastapi import FastAPI\napp=FastAPI()\n@app.get('/tasks')\ndef x():\n return []\nquery = f\"SELECT * FROM t WHERE id={1}\"\n"})
    spec = ApiSpec(openapi_yaml=TEMPLATE_SPEC)
    tests = TestSuite(files={"test_tasks.py": "def test_x(): assert True"})
    from schemas.messages import TestReport
    rep = R.review(bad, spec, tests, TestReport(passed=1, failed=0))
    msgs = [c.message for c in rep.comments]
    assert any("validation" in m.lower() or "404" in m for m in msgs), msgs
    assert any("blocker" == c.severity for c in rep.comments), msgs
