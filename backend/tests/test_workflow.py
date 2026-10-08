"""Workflow behaviour: blocking, the coverage gate, and the fix loop.

The headline regression is that a run whose agents all fail must end
``blocked`` — never ``accepted`` with a substituted task manager.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from graph.workflow import run_stub, run_team
from schemas.messages import TestFailure, TestReport, TestSuite

LIBRARY_APP = """from fastapi import FastAPI
from sqlalchemy import Column, Integer, String, create_engine
from sqlalchemy.orm import declarative_base

Base = declarative_base()
app = FastAPI()


class Book(Base):
    __tablename__ = "books"
    id = Column(Integer, primary_key=True)
    isbn = Column(String)


class Loan(Base):
    __tablename__ = "loans"
    id = Column(Integer, primary_key=True)
    isbn = Column(String)


@app.post("/loans")
def borrow(isbn: str):
    return {"isbn": isbn}
"""


def _bundle(body: str = LIBRARY_APP):
    """A CodeBundle carrying the domain vocabulary the coverage gate needs."""
    from schemas.messages import CodeBundle

    return CodeBundle(files={"app.py": body}, entry_module="app", entry_attr="app")


LIBRARY = (
    "Build a REST API for a library that lends books. POST /loans to borrow an "
    "ISBN, GET /books, 404 on unknown ISBN."
)


def test_stub_lists_every_node():
    out = run_stub("outputs/stub_test")
    assert out["ok"] is True
    assert len(out["nodes"]) == 8


def test_all_agent_failures_block_the_run(no_llm, tmp_path):
    """No backend, no answer: the run must say so."""
    st = run_team(LIBRARY, out_dir=str(tmp_path / "blocked"))
    assert st.status == "blocked", st.status
    assert "no backend" in st.error.lower() or "stubbed" in st.error.lower()
    summary = (tmp_path / "blocked" / "summary.json").read_text()
    assert '"blocked"' in summary


def test_blocked_run_writes_no_task_manager_code(no_llm, tmp_path):
    """The substituted-template regression: nothing may be invented."""
    st = run_team(LIBRARY, out_dir=str(tmp_path / "blocked2"))
    assert st.code is None
    assert not (tmp_path / "blocked2" / "code").exists()


def _spec_for_library():
    from schemas.messages import ApiSpec

    return ApiSpec(
        openapi_yaml=(
            "openapi: 3.1.0\ninfo: {title: Library, version: 1.0.0}\npaths:\n"
            "  /loans:\n    post:\n      responses: {'201': {description: ok}}\n"
        ),
        endpoints=["/loans"],
        entrypoint="app.py",
        files=["app.py"],
        domain_terms=["library", "books", "loans", "isbn"],
    )


def test_domain_missed_is_its_own_status(monkeypatch, tmp_path):
    """A generation that ignores the domain must not be graded on its tests."""
    from agents import designer, developer, pm
    from schemas.messages import CodeBundle, UserStories

    monkeypatch.setattr(
        pm,
        "requirement_to_stories",
        lambda r: UserStories(stories=[{"id": "US1", "title": "T", "acceptance": ["x"]}]),
    )
    monkeypatch.setattr(designer, "stories_to_spec", lambda s, requirement="": _spec_for_library())
    # Correct, working code — for the wrong domain entirely.
    monkeypatch.setattr(
        developer,
        "spec_to_code",
        lambda spec: CodeBundle(
            files={
                "app.py": (
                    "from fastapi import FastAPI\n"
                    "from sqlalchemy import Column, Integer, create_engine\n"
                    "app = FastAPI()\n"
                    "class Task(Base):\n    __tablename__='tasks'\n"
                    "    id = Column(Integer, primary_key=True)\n"
                    '@app.post("/tasks")\n'
                    "def create():\n    return {}\n"
                )
            },
            entry_module="app",
            entry_attr="app",
        ),
    )
    st = run_team(LIBRARY, out_dir=str(tmp_path / "missed"))
    assert st.status == "domain_missed", st.status
    assert st.coverage is not None and st.coverage.missing
    assert (tmp_path / "missed" / "coverage.json").exists()


def test_coverage_gate_runs_before_tests_are_graded(monkeypatch, tmp_path):
    """With a wrong-domain bundle the Tester must never be invoked."""
    from agents import developer, pm, tester
    from schemas.messages import CodeBundle, UserStories

    monkeypatch.setattr(
        pm,
        "requirement_to_stories",
        lambda r: UserStories(stories=[{"id": "US1", "title": "T", "acceptance": ["x"]}]),
    )
    from agents import designer

    monkeypatch.setattr(designer, "stories_to_spec", lambda s, requirement="": _spec_for_library())
    monkeypatch.setattr(
        developer,
        "spec_to_code",
        lambda spec: CodeBundle(
            files={"app.py": "app = FastAPI()\n"}, entry_module="app", entry_attr="app"
        ),
    )

    def must_not_run(*a, **k):
        raise AssertionError("Tester ran before the coverage gate")

    monkeypatch.setattr(tester, "spec_to_tests", must_not_run)
    st = run_team(LIBRARY, out_dir=str(tmp_path / "gate"))
    assert st.status == "domain_missed"


def test_fix_loop_stops_when_patch_changes_nothing(monkeypatch, tmp_path):
    """An unchanged bundle means every retry repeats the same attempt."""
    from agents import designer, developer, pm, reviewer, tester
    from graph import workflow as Workflow
    from schemas.messages import CodeBundle, UserStories

    monkeypatch.setattr(
        pm,
        "requirement_to_stories",
        lambda r: UserStories(stories=[{"id": "US1", "title": "T", "acceptance": ["x"]}]),
    )
    monkeypatch.setattr(designer, "stories_to_spec", lambda s, requirement="": _spec_for_library())
    monkeypatch.setattr(developer, "spec_to_code", lambda spec: _bundle())
    monkeypatch.setattr(
        developer,
        "patch_code",
        lambda code, report, refl: CodeBundle(
            files=dict(code.files),
            entry_module=code.entry_module,
            entry_attr=code.entry_attr,
        ),
    )
    monkeypatch.setattr(reviewer, "generate", lambda *a, **k: ("", {"ok": False}))
    monkeypatch.setattr(
        tester,
        "spec_to_tests",
        lambda *a, **k: TestSuite(files={"test_app.py": "def test_x(): assert True\n"}),
    )
    failing = TestReport(
        passed=0,
        failed=1,
        failures=[TestFailure(name="test_app.py::test_borrow", error="boom")],
        raw="",
    )
    monkeypatch.setattr(Workflow.Runner, "run_tests", lambda *a, **k: failing)
    st = run_team(LIBRARY, out_dir=str(tmp_path / "loop"))
    assert st.status == "unresolved"
    assert st.retry_dev == 0, "must stop before spending the retry budget"
    assert "no change" in st.error


def test_fix_loop_applies_a_real_change(monkeypatch, tmp_path):
    from agents import designer, developer, pm, reviewer, tester
    from graph import workflow as Workflow
    from schemas.messages import CodeBundle, UserStories

    monkeypatch.setattr(
        pm,
        "requirement_to_stories",
        lambda r: UserStories(stories=[{"id": "US1", "title": "T", "acceptance": ["x"]}]),
    )
    monkeypatch.setattr(designer, "stories_to_spec", lambda s, requirement="": _spec_for_library())
    monkeypatch.setattr(developer, "spec_to_code", lambda spec: _bundle())
    monkeypatch.setattr(
        developer,
        "patch_code",
        lambda code, report, refl: CodeBundle(
            files={"app.py": code.files["app.py"] + "\n# patched\n"},
            entry_module=code.entry_module,
            entry_attr=code.entry_attr,
        ),
    )
    monkeypatch.setattr(reviewer, "generate", lambda *a, **k: ("", {"ok": False}))
    monkeypatch.setattr(
        tester,
        "spec_to_tests",
        lambda *a, **k: TestSuite(files={"test_app.py": "def test_x(): assert True\n"}),
    )
    calls = {"n": 0}

    def fake_run_tests(code, tests, **k):
        calls["n"] += 1
        # Fail once, then go green so the patch is observed as useful.
        return (
            TestReport(passed=1, failed=0, failures=[], raw="")
            if calls["n"] > 1
            else TestReport(
                passed=0, failed=1, failures=[TestFailure(name="t", error="boom")], raw=""
            )
        )

    monkeypatch.setattr(Workflow.Runner, "run_tests", fake_run_tests)
    monkeypatch.setattr(Workflow.Runner, "static_analysis", lambda *a, **k: ("", ""))
    st = run_team(LIBRARY, out_dir=str(tmp_path / "real"))
    assert st.retry_dev == 1
    assert "# patched" in (tmp_path / "real" / "code" / "app.py").read_text()


def test_boot_failure_is_reported(monkeypatch, tmp_path):
    """Code that does not import must not be graded as unresolved-silently."""
    from agents import designer, developer, pm
    from graph import workflow as Workflow
    from schemas.messages import UserStories

    monkeypatch.setattr(
        pm,
        "requirement_to_stories",
        lambda r: UserStories(stories=[{"id": "US1", "title": "T", "acceptance": ["x"]}]),
    )
    monkeypatch.setattr(designer, "stories_to_spec", lambda s, requirement="": _spec_for_library())
    monkeypatch.setattr(
        developer,
        "spec_to_code",
        lambda spec: _bundle("import nonexistent_module_xyz\n" + LIBRARY_APP),
    )
    monkeypatch.setattr(
        Workflow.Runner,
        "boot_check",
        lambda code, timeout=60: (False, "ModuleNotFoundError: nonexistent_module_xyz"),
    )
    st = run_team(LIBRARY, out_dir=str(tmp_path / "boot"))
    assert st.status == "unresolved"
    assert "failed to import" in st.error
    assert (tmp_path / "boot" / "boot.txt").exists()


def test_collection_error_is_not_reported_as_green():
    from schemas.messages import TestReport as TR

    assert TR(passed=0, failed=0, raw="ERROR collecting test_app.py").ok is False
    assert TR(passed=7, failed=0).ok is True


def test_parse_pytest_counts_collection_errors():
    from sandbox.runner import _parse_pytest

    out = (
        "==================================== ERRORS ============================\n"
        'ERROR collecting test_app.py\nE   File "test_app.py", line 1\nE     ```python\n'
    )
    report = _parse_pytest(out)
    assert report.passed == 0
    assert report.failed >= 1
    assert report.ok is False


@pytest.mark.live_llm
def test_live_library_generation(tmp_path):
    """End-to-end against a real local model. Not part of the offline suite."""
    st = run_team(LIBRARY, out_dir=str(tmp_path / "live"))
    assert st.status in ("accepted", "unresolved", "unresolved_review"), (st.status, st.error)
    assert st.spec is not None
    assert "/loans" in st.spec.endpoints
    # The requested domain, not a task manager.
    blob = " ".join((st.code.files if st.code else {}).values()).lower()
    assert "task" not in blob
    assert "loan" in blob or "isbn" in blob


def test_boot_failure_enters_the_fix_loop(monkeypatch, tmp_path):
    """A failing import is fixable and must not end the run on first sight."""
    from agents import designer, developer, pm, reviewer, tester
    from graph import workflow as Workflow
    from schemas.messages import CodeBundle, TestSuite, UserStories

    monkeypatch.setattr(
        pm,
        "requirement_to_stories",
        lambda r: UserStories(stories=[{"id": "US1", "title": "T", "acceptance": ["x"]}]),
    )
    monkeypatch.setattr(designer, "stories_to_spec", lambda s, requirement="": _spec_for_library())
    monkeypatch.setattr(developer, "spec_to_code", lambda spec: _bundle())
    monkeypatch.setattr(
        tester,
        "spec_to_tests",
        lambda *a, **k: TestSuite(
            files={
                "test_app.py": (
                    "import app\nfrom fastapi.testclient import TestClient\n"
                    "def test_h():\n    assert TestClient(app.app).get('/health').status_code == 200\n"
                )
            }
        ),
    )
    monkeypatch.setattr(reviewer, "generate", lambda *a, **k: ("", {"ok": False}))

    boots = {"n": 0}

    def fake_boot(code, timeout=60):
        boots["n"] += 1
        # Fails once with a fixable error, then imports.
        if boots["n"] == 1:
            return False, "ImportError: cannot import name 'Loan' from 'models'"
        return True, "boot-ok FastAPI"

    monkeypatch.setattr(Workflow.Runner, "boot_check", fake_boot)
    monkeypatch.setattr(Workflow.Runner, "static_analysis", lambda *a, **k: ("", ""))
    monkeypatch.setattr(
        developer,
        "patch_code",
        lambda code, report, refl: CodeBundle(
            files={"app.py": code.files["app.py"] + "\n# fixed import\n"},
            entry_module=code.entry_module,
            entry_attr=code.entry_attr,
        ),
    )
    monkeypatch.setattr(
        Workflow.Runner,
        "run_tests",
        lambda *a, **k: TestReport(passed=1, failed=0, failures=[], raw=""),
    )

    st = run_team(LIBRARY, out_dir=str(tmp_path / "bootloop"))
    assert st.retry_dev == 1, st.status
    assert st.status == "accepted", st.status
