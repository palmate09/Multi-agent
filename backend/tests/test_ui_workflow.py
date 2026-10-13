"""UI node traversal: the pipeline walks them, resume restores them, and the
UI verdict never changes the run's terminal status.

Agents are stubbed. The property under test is orchestration, not generation.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agents import ui
from graph.workflow import reconstruct_state, run_team
from schemas.messages import ApiSpec, CodeBundle, Plan, UiCheck, UiReport, UiSpec

SPEC_YAML = (
    "openapi: 3.1.0\n"
    "info: {title: Recipes, version: 1.0.0}\n"
    "paths:\n"
    "  /recipes:\n"
    "    get:\n      responses: {'200': {description: ok}}\n"
    "    post:\n      responses: {'201': {description: ok}}\n"
)

APP_PY = """from fastapi import FastAPI

app = FastAPI()


@app.get("/recipes")
def recipes():
    return [{"title": "soup"}]
"""

TEST_PY = (
    "import main\n"
    "from fastapi.testclient import TestClient\n"
    "\n"
    "def test_recipes():\n"
    "    assert TestClient(main.app).get('/recipes').status_code == 200\n"
)

UI_FILES = {
    "index.html": '<html><body><div id="root"></div><script src="app.js" defer></script></body></html>',
    "app.js": "async function go() { const r = await fetch('recipes'); document.getElementById('root').textContent = JSON.stringify(await r.json()); }\ngo();\n",
    "styles.css": "body {}",
}
UI_SPEC = UiSpec(
    pages=["/"],
    components=["recipe-list"],
    data_bindings=["GET /recipes -> list"],
    files=["index.html", "app.js", "styles.css"],
)

RECIPE = "Build a REST API for a recipe collection. GET /recipes, POST /recipes, 404 on unknown id."


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    """Stub every node so the walk is deterministic and needs no backend."""
    from agents import designer, developer, pm, reasoner, tester

    monkeypatch.setattr(reasoner, "plan", lambda requirement, stories=None: Plan(approach="stub"))
    monkeypatch.setattr(
        pm,
        "requirement_to_stories",
        lambda r: __import__("schemas.messages", fromlist=["UserStories"]).UserStories(
            stories=[{"id": "US1", "title": "List recipes", "acceptance": ["GET /recipes"]}]
        ),
    )
    monkeypatch.setattr(
        designer,
        "stories_to_spec",
        lambda stories, requirement="", plan=None: ApiSpec(
            openapi_yaml=SPEC_YAML,
            endpoints=["/recipes"],
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
        lambda spec, stories, entry_module="", entry_attr="", code=None: __import__(
            "schemas.messages", fromlist=["TestSuite"]
        ).TestSuite(files={"test_app.py": TEST_PY}),
    )


def _stub_ui(monkeypatch, *, spec=None, files=None, report=None):
    """Make the three UI nodes deterministic, failing where asked to."""
    monkeypatch.setattr(ui, "design_ui", lambda s, stories=None, attempts=3: spec or UI_SPEC)
    monkeypatch.setattr(
        ui,
        "build_ui",
        lambda s, stories, us, entry_module="", entry_attr="": CodeBundle(
            files=dict(files or UI_FILES)
        ),
    )
    monkeypatch.setattr(
        ui,
        "gate",
        lambda s, us, f: (
            report or UiReport(ok=True, checks=[UiCheck(name="missing_files", ok=True)])
        ),
    )


def test_ui_nodes_run_when_requested(monkeypatch, tmp_path):
    _stub_ui(monkeypatch)
    events = []
    st = run_team(
        RECIPE, out_dir=str(tmp_path / "r"), want_ui=True, on_event=lambda *a: events.append(a)
    )

    assert st.status == "accepted", st.status
    assert st.spec.has_ui is True
    assert st.ui_spec is not None
    assert set(st.ui_code.files) == set(UI_FILES)
    assert st.ui_report is not None and st.ui_report.ok

    nodes = {node for node, phase, _ in events}
    for node in ("ui_designer", "ui_developer", "ui_gate"):
        assert node in nodes, f"{node} never emitted; got {sorted(nodes)}"
    # Ordering: the UI is built only after the backend is green.
    order = [n for n, _, _ in events]
    assert order.index("ui_developer") > order.index("runner")


def test_ui_nodes_are_skipped_by_default(monkeypatch, tmp_path):
    """The default path is untouched: no UI nodes, no UI artifacts."""
    _stub_ui(monkeypatch)
    st = run_team(RECIPE, out_dir=str(tmp_path / "no-ui"))
    assert st.status == "accepted", st.status
    assert st.spec.has_ui is False
    assert st.ui_spec is None
    assert not (tmp_path / "no-ui" / "ui").exists()


def test_ui_artifacts_are_written(monkeypatch, tmp_path):
    _stub_ui(monkeypatch)
    out = tmp_path / "artifacts"
    run_team(RECIPE, out_dir=str(out), want_ui=True)
    assert (out / "ui_contract.json").exists()
    assert (out / "ui_report.json").exists()
    assert (out / "ui" / "index.html").read_text().startswith("<html>")
    assert (out / "ui" / "app.js").exists()
    # plan.json records the choice, which is what makes a resume work.
    assert '"has_ui": true' in (out / "plan.json").read_text()
    assert '"ui_files"' in (out / "summary.json").read_text()


def test_a_failed_gate_leaves_the_status_accepted(monkeypatch, tmp_path):
    """The headline guarantee: a bad UI is reported, never fatal."""
    _stub_ui(
        monkeypatch,
        report=UiReport(
            ok=False,
            checks=[
                UiCheck(name="missing_files", ok=False, detail="styles.css absent"),
                UiCheck(name="rooted_url", ok=False, detail="fetch('/recipes')"),
            ],
        ),
    )
    st = run_team(RECIPE, out_dir=str(tmp_path / "bad-ui"), want_ui=True)
    assert st.status == "accepted", st.status
    assert st.ui_report is not None
    assert st.ui_report.ok is False
    assert [c.name for c in st.ui_report.failures] == ["missing_files", "rooted_url"]
    assert st.error == ""


def test_ui_generation_failure_leaves_the_status_accepted(monkeypatch, tmp_path):
    from agents.llm import GenerationError

    _stub_ui(monkeypatch)

    def boom(*a, **k):
        raise GenerationError("no backend answered")

    monkeypatch.setattr(ui, "design_ui", boom)
    st = run_team(RECIPE, out_dir=str(tmp_path / "ui-fail"), want_ui=True)
    assert st.status == "accepted", st.status
    assert st.ui_report is not None and not st.ui_report.ok
    assert st.ui_report.failures[0].name == "generation"
    assert (tmp_path / "ui-fail" / "ui_report.json").exists()


def test_review_reports_ui_findings_as_minor_only(monkeypatch, tmp_path):
    """A UI problem must never reach the review as a blocker."""
    _stub_ui(
        monkeypatch,
        report=UiReport(
            ok=False, checks=[UiCheck(name="rooted_url", ok=False, detail="root-anchored URL")]
        ),
    )
    st = run_team(RECIPE, out_dir=str(tmp_path / "review"), want_ui=True)
    assert st.review is not None
    ui_comments = [c for c in st.review.comments if c.location.startswith("ui/")]
    assert ui_comments, "the UI gate's verdict was not surfaced in the review"
    assert all(c.severity == "minor" for c in ui_comments)
    assert st.status == "accepted"


def test_program_projects_skip_the_ui(monkeypatch, tmp_path):
    """A program has no endpoints to bind a page to."""
    from agents import designer

    monkeypatch.setattr(
        designer,
        "stories_to_spec",
        lambda stories, requirement="", plan=None: ApiSpec(
            openapi_yaml="",
            endpoints=[],
            entrypoint="game.py",
            files=["game.py"],
            kind="program",
            public_api=["Game"],
            contract_text='{"behaviours": ["a new game has 2 players", "play returns a token"]}',
        ),
    )
    _stub_ui(monkeypatch)
    st = run_team("Build a ludo game with 2 players.", out_dir=str(tmp_path / "prog"), want_ui=True)
    assert st.skip_ui is True
    assert st.spec.has_ui is False
    assert st.ui_spec is None


# -------------------------------------------------------------------- resume ----
def test_resume_restores_the_ui_half(tmp_path):
    out = tmp_path / "resume"
    _saved = {
        "spec.yaml": SPEC_YAML,
        "plan.json": '{"kind": "api", "entrypoint": "main.py", "files": ["main.py"], "has_ui": true}',
        "code/main.py": APP_PY,
        "tests/test_app.py": TEST_PY,
        "ui_contract.json": UI_SPEC.model_dump_json(indent=2),
        "ui/index.html": UI_FILES["index.html"],
        "ui/app.js": UI_FILES["app.js"],
        "ui/styles.css": UI_FILES["styles.css"],
        "ui_report.json": UiReport(
            ok=False, checks=[UiCheck(name="external_asset", ok=False, detail="cdn")]
        ).model_dump_json(indent=2),
    }
    for rel, body in _saved.items():
        p = out / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body)

    st = reconstruct_state(out)
    assert st.ui_spec is not None
    assert st.ui_spec.data_bindings == ["GET /recipes -> list"]
    assert st.ui_code is not None
    assert set(st.ui_code.files) == set(UI_FILES)
    assert st.ui_report is not None and not st.ui_report.ok
    assert st.ui_report.failures[0].name == "external_asset"
    assert st.spec.has_ui is True


def test_resume_does_not_regenerate_a_restored_ui(monkeypatch, tmp_path):
    """A restored UI is adopted, not re-prompted for."""
    out = tmp_path / "adopt"
    for rel, body in {
        "spec.yaml": SPEC_YAML,
        "plan.json": '{"kind": "api", "entrypoint": "main.py", "files": ["main.py"], "has_ui": true}',
        "code/main.py": APP_PY,
        "tests/test_app.py": TEST_PY,
        "ui_contract.json": UI_SPEC.model_dump_json(indent=2),
        "ui/index.html": UI_FILES["index.html"],
        "ui/app.js": UI_FILES["app.js"],
        "ui/styles.css": UI_FILES["styles.css"],
    }.items():
        p = out / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body)

    def boom(*a, **k):
        raise AssertionError("the UI was regenerated instead of adopted")

    _stub_ui(monkeypatch)
    monkeypatch.setattr(ui, "design_ui", boom)
    monkeypatch.setattr(ui, "build_ui", boom)
    st = run_team(RECIPE, out_dir=str(out), want_ui=True, resume=reconstruct_state(out))
    assert st.ui_spec is not None
    assert st.ui_code is not None
    assert st.status in {"accepted", "accepted_no_review"}, st.status


def test_reconstruct_ignores_malformed_ui_artifacts(tmp_path):
    """A corrupt artifact means "not done", never an exception."""
    out = tmp_path / "corrupt"
    (out / "code").mkdir(parents=True)
    (out / "code" / "main.py").write_text(APP_PY)
    (out / "spec.yaml").write_text(SPEC_YAML)
    (out / "ui_contract.json").write_text("{not json")
    (out / "ui_report.json").write_text("[]")
    (out / "ui").mkdir()
    (out / "ui" / "index.html").write_text(UI_FILES["index.html"])

    st = reconstruct_state(out)
    assert st.ui_spec is None
    assert st.ui_report is None
    assert st.ui_code is not None  # the files on disk are still usable
