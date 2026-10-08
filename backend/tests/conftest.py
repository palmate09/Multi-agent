"""Shared pytest configuration.

The offline suite must be fast and hermetic: every test stubs the LLM layer so
no test can reach a real model, and no stray ``.env`` can influence the result.
Tests that genuinely exercise generation live behind the ``live_llm`` marker.
"""

import os

os.environ.setdefault("SKIP_OLLAMA", "1")
os.environ.setdefault("AGENT_TEAM_TESTING", "1")
# A developer's .env must never influence a test run.
os.environ["PYTHON_DOTENV_DISABLED"] = "1"

import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))


def pytest_collection_modifyitems(config, items):
    """Skip ``live_llm`` tests unless Ollama actually answers.

    Without this, ``pytest -m live_llm`` on a machine with no models pulled
    hangs for the full timeout on every case instead of reporting a skip.
    """
    if not any(item.get_closest_marker("live_llm") for item in items):
        return
    import urllib.error
    import urllib.request

    try:
        with urllib.request.urlopen("http://localhost:11434/api/tags", timeout=2) as r:
            import json

            names = {m.get("name", "") for m in json.loads(r.read().decode()).get("models", [])}
    except Exception:
        pytest.skip("no Ollama on localhost:11434")
    if not any(n.startswith("qwen2.5-coder") for n in names):
        pytest.skip("no qwen2.5-coder model pulled")


@pytest.fixture()
def no_llm(monkeypatch):
    """Make every agent's LLM call fail fast and loudly.

    Used to assert the pipeline *blocks* rather than silently producing a
    fallback answer, which is the regression this suite exists for.
    """
    from agents import designer, developer, llm, pm, reviewer, tester

    def boom(*a, **k):
        raise llm.GenerationError("stubbed: no backend available")

    for mod in (pm, designer, developer, tester):
        monkeypatch.setattr(mod, "require", boom)
    monkeypatch.setattr(reviewer, "generate", lambda *a, **k: ("", {"ok": False}))


@pytest.fixture()
def fake_pipeline(monkeypatch):
    """Stub ``run_team`` with a small, valid library application.

    The API suite is about HTTP behaviour — lifecycle, streaming, artifact
    access, traversal defence — not about generation. Generation has its own
    ``live_llm`` tests. Stubbing here keeps the suite hermetic and fast.
    """
    import json

    from agents import reviewer
    from app.services import runs as runs_mod
    from schemas.messages import (
        CoverageReport,
        GraphState,
        ReviewReport,
        TestReport,
        TestSuite,
        UserStories,
    )

    app_py = (
        "from fastapi import FastAPI\n"
        "from sqlalchemy import Column, Integer, String\n"
        "from sqlalchemy.orm import declarative_base\n"
        "\n"
        "Base = declarative_base()\n"
        "app = FastAPI()\n"
        "\n"
        "\n"
        "class Book(Base):\n"
        '    __tablename__ = "books"\n'
        "    id = Column(Integer, primary_key=True)\n"
        "    isbn = Column(String)\n"
        "\n"
        "\n"
        '@app.get("/health")\n'
        "def health():\n"
        '    return {"status": "ok"}\n'
        "\n"
        "\n"
        '@app.get("/books")\n'
        "def books():\n"
        "    return []\n"
    )
    test_py = (
        "import app\n"
        "from fastapi.testclient import TestClient\n"
        "\n"
        "\n"
        "def test_health():\n"
        "    assert TestClient(app.app).get('/health').status_code == 200\n"
    )
    spec_yaml = (
        "openapi: 3.1.0\n"
        "info:\n  title: Library API\n  version: 1.0.0\n"
        "paths:\n"
        "  /health:\n    get:\n      responses:\n        '200':\n          description: ok\n"
        "  /books:\n    get:\n      responses:\n        '200':\n          description: ok\n"
    )

    def fake_run_team(requirement, out_dir, on_event=None, run_id=None, **kw):
        from pathlib import Path

        from schemas.messages import ApiSpec, CodeBundle

        out = Path(out_dir)
        (out / "code").mkdir(parents=True, exist_ok=True)
        (out / "tests").mkdir(parents=True, exist_ok=True)
        (out / "code" / "app.py").write_text(app_py)
        (out / "tests" / "test_app.py").write_text(test_py)
        (out / "spec.yaml").write_text(spec_yaml)
        (out / "stories.json").write_text(
            UserStories(
                stories=[{"id": "US1", "title": "List books", "acceptance": ["GET /books 200"]}]
            ).model_dump_json(indent=2)
        )
        (out / "coverage.json").write_text(
            CoverageReport(covered=["books"], missing=[], conclusive=True).model_dump_json(indent=2)
        )
        (out / "summary.json").write_text(
            json.dumps(
                {
                    "status": "accepted_no_review" if kw.get("skip_reviewer") else "accepted",
                    "error": None,
                    "tests_passed": 1,
                    "tests_failed": 0,
                    "attempts_dev": 0,
                    "attempts_test": 0,
                    "review_rounds": 0,
                    "entrypoint": "app",
                    "files": ["app.py"],
                    "wall_time": 0.5,
                },
                indent=2,
            )
        )
        st = GraphState(
            requirement=requirement,
            run_id=run_id or out.name,
            stories=UserStories(
                stories=[{"id": "US1", "title": "List books", "acceptance": ["GET /books 200"]}]
            ),
            spec=ApiSpec(
                openapi_yaml=spec_yaml,
                endpoints=["/health", "/books"],
                entrypoint="app.py",
                files=["app.py"],
                domain_terms=["books"],
            ),
            code=CodeBundle(files={"app.py": app_py}, entry_module="app", entry_attr="app"),
            tests=TestSuite(files={"test_app.py": test_py}),
            report=TestReport(passed=1, failed=0),
            review=ReviewReport(),
            coverage=CoverageReport(covered=["books"], missing=[], conclusive=True),
            status="accepted_no_review" if kw.get("skip_reviewer") else "accepted",
        )
        if on_event:
            on_event("pipeline", "start", {"run_id": st.run_id, "nodes": []})
            on_event("final", "done", {"status": st.status})
        return st

    monkeypatch.setattr(runs_mod, "run_team", fake_run_team)
    monkeypatch.setattr(reviewer, "generate", lambda *a, **k: ("", {"ok": False}))
    return fake_run_team
