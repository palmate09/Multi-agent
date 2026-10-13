"""Offline end-to-end smoke: pipeline -> artifacts -> playground -> proxy.

Runs the real workflow and the real playground against the same on-disk run
directory the API serves from. The only thing stubbed is the LLM, so what is
exercised here is exactly the wiring a live run uses.
"""

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_settings
from app.main import create_app
from app.services.playground import reset_playground
from evals.metrics import summarize_run
from graph.workflow import run_team

SPEC_YAML = (
    "openapi: 3.1.0\n"
    "info: {title: Recipes, version: 1.0.0}\n"
    "paths:\n"
    "  /recipes:\n"
    "    get:\n      responses: {'200': {description: ok}}\n"
    "    post:\n      responses: {'201': {description: ok}}\n"
    "  /health:\n    get:\n      responses: {'200': {description: ok}}\n"
)

# A generated app that does NOT mount its static files, because the Developer
# node runs before the UI exists and never sees it. This is what a real Gemini
# run produced, so the gateway has to serve the page itself.
APP_PY = '''"""Recipe collection API with a static web UI."""
from fastapi import FastAPI

app = FastAPI()


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/recipes")
def recipes():
    return [{"title": "soup"}, {"title": "salad"}]
'''

TEST_PY = (
    "import main\n"
    "from fastapi.testclient import TestClient\n"
    "\n"
    "def test_recipes():\n"
    "    assert TestClient(main.app).get('/recipes').status_code == 200\n"
)

UI_FILES = {
    "index.html": (
        '<!doctype html><html><head><link rel="stylesheet" href="styles.css">'
        '<script src="app.js" defer></script></head>'
        '<body><h1>Recipes</h1><div id="root"></div></body></html>'
    ),
    "app.js": (
        "async function go() {\n"
        "  const r = await fetch('recipes');\n"
        "  const list = document.getElementById('root');\n"
        "  for (const x of await r.json()) {\n"
        "    const li = document.createElement('li');\n"
        "    li.textContent = x.title;\n"
        "    list.appendChild(li);\n"
        "  }\n"
        "}\n"
        "go();\n"
    ),
    "styles.css": "body { font-family: system-ui; }",
}

UI_SPEC_JSON = """{
  "pages": ["/"],
  "components": ["recipe-list"],
  "data_bindings": ["GET /recipes -> list of recipes"],
  "files": ["index.html", "app.js", "styles.css"]
}"""


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    """Stub every agent so the run needs no model backend."""
    from agents import designer, developer, pm, reasoner, tester, ui
    from schemas.messages import (
        ApiSpec,
        CodeBundle,
        Plan,
        TestSuite,
        UiSpec,
        UserStories,
    )

    monkeypatch.setattr(reasoner, "plan", lambda requirement, stories=None: Plan(approach="stub"))
    monkeypatch.setattr(
        pm,
        "requirement_to_stories",
        lambda r: UserStories(
            stories=[{"id": "US1", "title": "List recipes", "acceptance": ["GET /recipes"]}]
        ),
    )
    monkeypatch.setattr(
        designer,
        "stories_to_spec",
        lambda stories, requirement="", plan=None: ApiSpec(
            openapi_yaml=SPEC_YAML,
            endpoints=["/recipes", "/health"],
            entrypoint="main.py",
            files=["main.py"],
            domain_terms=["recipes"],
        ),
    )
    monkeypatch.setattr(
        developer,
        "spec_to_code",
        lambda spec, plan=None: CodeBundle(
            files={"main.py": APP_PY}, entry_module="main", entry_attr="app"
        ),
    )
    monkeypatch.setattr(
        tester,
        "spec_to_tests",
        lambda spec, stories, entry_module="", entry_attr="", code=None: TestSuite(
            files={"test_app.py": TEST_PY}
        ),
    )
    monkeypatch.setattr(
        ui,
        "design_ui",
        lambda s, stories=None, attempts=3: UiSpec.model_validate_json(UI_SPEC_JSON),
    )
    monkeypatch.setattr(
        ui,
        "build_ui",
        lambda s, stories, us, entry_module="", entry_attr="": CodeBundle(files=dict(UI_FILES)),
    )


def test_full_stack_run_then_playground(monkeypatch, tmp_path):
    from agents import ui
    from schemas.messages import UiCheck, UiReport

    monkeypatch.setattr(
        ui,
        "gate",
        lambda s, us, f: UiReport(ok=True, checks=[UiCheck(name="missing_files", ok=True)]),
    )

    runs = tmp_path / "runs"
    out = runs / "smoke-1"

    st = run_team(
        "Build a REST API for a recipe collection with a web UI.",
        out_dir=str(out),
        want_ui=True,
        run_id="smoke-1",
    )
    assert st.status == "accepted", st.error
    assert st.spec.has_ui and st.ui_spec and st.ui_code and st.ui_report

    # The metrics see the UI half of the run.
    summary = summarize_run(str(out))
    assert summary["ui_pass_rate"] == 1.0
    assert summary["preview_booted"] is None  # nothing previewed it yet

    # Now boot it through the real service and proxy it through the real app.
    monkeypatch.setenv("RUNS_DIR", str(runs))
    get_settings.cache_clear()
    reset_playground()
    app = create_app()
    try:
        with TestClient(app) as c:
            resp = c.post("/api/runs/smoke-1/playground/start")
            assert resp.status_code == 202, resp.text
            import time

            deadline = time.time() + 40
            body = {}
            while time.time() < deadline:
                body = c.get("/api/runs/smoke-1/playground").json()
                if body["state"] in {"running", "crashed"}:
                    break
                time.sleep(0.3)
            assert body["state"] == "running", body.get("error")

            # The UI page is served by the gateway from the run's ui/ dir --
            # the app mounts nothing, exactly like a real generation.
            page = c.get("/preview/smoke-1/")
            assert page.status_code == 200, page.text
            assert "Recipes" in page.text
            assert 'src="app.js"' in page.text
            assert page.headers["content-type"].startswith("text/html")

            # Its sibling assets resolve from the same mount.
            assert c.get("/preview/smoke-1/app.js").status_code == 200
            assert c.get("/preview/smoke-1/styles.css").status_code == 200

            # ...and its relative fetch is proxied to the app, which is the
            # whole reason the gate rejects root-anchored URLs.
            assert c.get("/preview/smoke-1/recipes").json() == [
                {"title": "soup"},
                {"title": "salad"},
            ]

            # The durable record the metrics read.
            assert summarize_run(str(out))["preview_booted"] is True

            assert c.post("/api/runs/smoke-1/playground/stop").json()["stopped"] is True
    finally:
        reset_playground()
        get_settings.cache_clear()
