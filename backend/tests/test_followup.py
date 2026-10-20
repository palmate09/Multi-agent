"""Follow-ups: one change to an app that already exists.

A follow-up is not a new run of the same requirement. It starts from the
run's artifacts and applies one instruction, so the result is the same product
with one thing different -- and the follow-ups of one run are executed one at a
time, in the order they were given.
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agents import designer, developer, pm
from schemas.messages import ApiSpec, CodeBundle, UserStories


def _stories() -> UserStories:
    return UserStories(
        stories=[
            {"id": "US1", "title": "List books", "acceptance": ["GET /books returns 200"]},
            {"id": "US2", "title": "Borrow a book", "acceptance": ["POST /loans 201"]},
        ]
    )


def _spec() -> ApiSpec:
    return ApiSpec(
        openapi_yaml=(
            "openapi: 3.1.0\n"
            "info: {title: Library, version: 1.0.0}\n"
            "paths:\n"
            "  /books:\n    get:\n      responses:\n        '200':\n          description: ok\n"
            "  /loans:\n    post:\n      responses:\n        '201':\n          description: ok\n"
        ),
        endpoints=["/books", "/loans"],
        entrypoint="app.py",
        files=["app.py"],
        kind="api",
    )


# ------------------------------------------------------------------- the PM ---
def test_the_pm_keeps_the_existing_stories_when_applying_a_change(monkeypatch):
    reply = (
        '{"analysis": "Adds a reservation window.", "complexity": "moderate", "stories": ['
        '{"id": "US1", "title": "List books", "acceptance": ["GET /books returns 200"]},'
        '{"id": "US2", "title": "Borrow a book", "acceptance": ["POST /loans 201"]},'
        '{"id": "US3", "title": "Reserve a book for a date",'
        ' "acceptance": ["POST /reservations 201"]}]}'
    )
    seen = {}

    def spy(prompt, *a, **k):
        seen["prompt"] = prompt
        return (reply, {"ok": True})

    monkeypatch.setattr(pm, "require", spy)
    stories = pm.update_stories(
        "Build a library API", "Let people reserve a book for a date", _stories()
    )
    assert [s.id for s in stories.stories] == ["US1", "US2", "US3"]
    assert stories.stories[-1].title == "Reserve a book for a date"
    # The change is applied to what is already there, not to a blank page.
    assert "List books" in seen["prompt"]
    assert "reserve a book for a date" in seen["prompt"]


def test_a_change_may_not_drop_an_existing_story(monkeypatch):
    """A follow-up that quietly loses a capability is the worst way to lose one."""
    dropped = (
        '{"analysis": "Adds a reservation window.", "complexity": "moderate", "stories": ['
        '{"id": "US1", "title": "List books", "acceptance": ["GET /books returns 200"]},'
        '{"id": "US3", "title": "Reserve a book", "acceptance": ["POST /reservations 201"]}]}'
    )
    kept = (
        '{"analysis": "Adds a reservation window.", "complexity": "moderate", "stories": ['
        '{"id": "US1", "title": "List books", "acceptance": ["GET /books returns 200"]},'
        '{"id": "US2", "title": "Borrow a book", "acceptance": ["POST /loans 201"]},'
        '{"id": "US3", "title": "Reserve a book", "acceptance": ["POST /reservations 201"]}]}'
    )
    replies = iter([dropped, kept])
    seen: list[str] = []

    def spy(prompt, *a, **k):
        seen.append(prompt)
        return (next(replies), {"ok": True})

    monkeypatch.setattr(pm, "require", spy)
    stories = pm.update_stories(
        "Build a library API", "Let people reserve a book for a date", _stories()
    )
    assert [s.id for s in stories.stories] == ["US1", "US2", "US3"]
    assert len(seen) == 2
    assert "dropped these existing stories" in seen[1]


# -------------------------------------------------------------- the designer ---
def test_the_designer_returns_the_whole_updated_contract(monkeypatch):
    reply = (
        "```yaml\n"
        "openapi: 3.1.0\n"
        "info: {title: Library, version: 1.0.0}\n"
        "paths:\n"
        "  /books:\n    get:\n      responses:\n        '200':\n          description: ok\n"
        "  /loans:\n    post:\n      responses:\n        '201':\n          description: ok\n"
        "  /reservations:\n    post:\n      responses:\n        '201':\n          description: ok\n"
        "```\n"
        '```json\n{"entrypoint": "app.py", "files": ["app.py"]}\n```'
    )
    seen = {}

    def spy(prompt, *a, **k):
        seen["prompt"] = prompt
        return (reply, {"ok": True})

    monkeypatch.setattr(designer, "require", spy)
    spec = designer.update_spec(
        _spec(), "Let people reserve a book for a date", _stories(), requirement="Library"
    )
    # The whole document comes back: the old paths survive and the new one is
    # added. A fragment could not be validated, tested or served.
    assert "/books" in spec.endpoints
    assert "/reservations" in spec.endpoints
    assert "Current OpenAPI contract" in seen["prompt"]


# ------------------------------------------------------------- the developer ---
def test_the_change_prompt_stays_inside_the_provider_budget(monkeypatch):
    """A prompt the provider refuses (HTTP 413) changes nothing at all.

    The first live follow-up died exactly there: the contract, every file and
    the whole test suite in one request. The prompt is budgeted now, and this
    pins that.
    """
    seen = {}

    def spy(prompt, system, *a, **k):
        seen["prompt"] = prompt
        return (
            "### app.py\n```python\nfrom fastapi import FastAPI\napp = FastAPI()\n```",
            {"ok": True},
        )

    monkeypatch.setattr(developer, "require", spy)
    files = {f"mod{i}.py": "x = 1\n" * 2000 for i in range(6)}
    files["main.py"] = "y = 2\n" * 3000
    code = CodeBundle(files=files, entry_module="main", entry_attr="app")
    developer.apply_change(code, _spec(), "add a reservation endpoint")
    assert len(seen["prompt"]) < 26000, len(seen["prompt"])


def test_the_developer_edits_the_existing_code(monkeypatch):
    seen = {}

    def spy(prompt, system, *a, **k):
        seen["prompt"] = prompt
        seen["system"] = system
        return (
            "### app.py\n```python\nfrom fastapi import FastAPI\napp = FastAPI()\n"
            '@app.get("/books")\ndef books():\n    return []\n```',
            {"ok": True},
        )

    monkeypatch.setattr(developer, "require", spy)
    code = CodeBundle(
        files={"app.py": "from fastapi import FastAPI\napp = FastAPI()\n# EXISTING\n"},
        entry_module="app",
        entry_attr="app",
    )
    out = developer.apply_change(code, _spec(), "Add a reservation endpoint")
    assert "EXISTING" in seen["prompt"], "the current code must be in the prompt"
    assert "Add a reservation endpoint" in seen["prompt"]
    assert out.entry_module == "app"


# -------------------------------------------------------------- the pipeline ---
def test_a_follow_up_run_applies_the_change_to_the_previous_app(monkeypatch, tmp_path):
    """The whole point: the change lands on the app that already exists."""
    from graph import workflow as Workflow
    from schemas.messages import GraphState

    calls: dict[str, str] = {}

    monkeypatch.setattr(
        pm,
        "update_stories",
        lambda requirement, request, previous: UserStories(
            stories=[{"id": "US3", "title": "Reserve a book", "acceptance": ["POST /reservations"]}]
        ),
    )
    monkeypatch.setattr(
        designer,
        "update_spec",
        lambda previous, request, stories, requirement="": ApiSpec(
            openapi_yaml=previous.openapi_yaml + "  /reservations:\n    post: {}\n",
            endpoints=[*previous.endpoints, "/reservations"],
            entrypoint=previous.entrypoint,
            files=previous.files,
            kind=previous.kind,
        ),
    )
    monkeypatch.setattr(
        developer,
        "apply_change",
        lambda code, spec, request, tests=None: CodeBundle(
            files={"app.py": "# changed: the library lends books and loans\n"},
            entry_module="app",
            entry_attr="app",
        ),
    )
    monkeypatch.setattr(
        Workflow.Runner,
        "boot_check",
        lambda code, timeout=60, public_api=None, web_root=False: (True, "boot-ok"),
    )
    monkeypatch.setattr(
        Workflow.Runner,
        "run_tests",
        lambda code, tests, **k: __import__("schemas.messages", fromlist=["TestReport"]).TestReport(
            passed=1, failed=0
        ),
    )
    monkeypatch.setattr(Workflow.Runner, "static_analysis", lambda *a, **k: ("", ""))
    from agents import reasoner, reviewer, tester

    monkeypatch.setattr(reviewer, "generate", lambda *a, **k: ("", {"ok": False}))
    monkeypatch.setattr(
        reviewer,
        "review",
        lambda *a, **k: __import__("schemas.messages", fromlist=["ReviewReport"]).ReviewReport(),
    )
    monkeypatch.setattr(
        reasoner,
        "plan",
        lambda requirement, stories=None: __import__("schemas.messages", fromlist=["Plan"]).Plan(
            approach="stub"
        ),
    )
    monkeypatch.setattr(
        tester,
        "spec_to_tests",
        lambda spec, stories, entry_module="", entry_attr="", code=None: __import__(
            "schemas.messages", fromlist=["TestSuite"]
        ).TestSuite(files={"test_app.py": "def test_x():\n    assert True\n"}),
    )

    previous = GraphState(
        requirement="Build a library API",
        run_id="prev",
        stories=_stories(),
        spec=_spec(),
        code=CodeBundle(files={"app.py": "# original\n"}, entry_module="app", entry_attr="app"),
    )
    st = Workflow.run_team(
        "Build a library API",
        out_dir=str(tmp_path / "followup"),
        resume=previous,
        followup="Let people reserve a book for a date",
        followup_of="prev",
    )
    assert st.status in {"accepted", "accepted_no_review"}, st.status
    assert st.followup_prompt == "Let people reserve a book for a date"
    assert st.followup_of == "prev"
    # The updated stories and contract are what the run ships.
    assert st.stories.stories[0].id == "US3"
    assert "/reservations" in st.spec.endpoints
    # The change is in the code, not the original.
    assert "changed" in st.code.files["app.py"]
    assert calls == {}


# ---------------------------------------------------------------- the queue ---
def test_follow_ups_of_one_run_run_one_at_a_time(tmp_path, monkeypatch):
    """Two changes to the same app must not edit the same files at once."""
    from app.config import get_settings
    from app.services import runs as runs_mod

    monkeypatch.setenv("RUNS_DIR", str(tmp_path / "runs"))
    get_settings.cache_clear()
    order: list[str] = []
    running = {"count": 0, "max": 0}

    def fake_run_team(requirement, out_dir, on_event=None, run_id=None, **kw):
        running["count"] += 1
        running["max"] = max(running["max"], running["count"])
        order.append(kw.get("followup") or requirement)
        time.sleep(0.2)
        running["count"] -= 1
        from schemas.messages import GraphState

        st = GraphState(
            requirement=requirement, run_id=run_id or Path(out_dir).name, status="accepted"
        )
        if on_event:
            on_event("final", "done", {"status": "accepted"})
        return st

    monkeypatch.setattr(runs_mod, "run_team", fake_run_team)
    store = runs_mod.RunStore()
    try:
        parent = store.create("Build a library API")
        parent.status = "accepted"
        first = store.add_followup(parent.run_id, "add reservations")
        second = store.add_followup(parent.run_id, "add a due date")
        deadline = time.time() + 20
        while time.time() < deadline:
            if first.status in {"accepted"} and second.status in {"accepted"}:
                break
            time.sleep(0.1)
        assert first.status == "accepted"
        assert second.status == "accepted"
        # One at a time: the second never ran while the first was running.
        assert running["max"] == 1
        # And in the order they were given.
        assert order[-2:] == ["add reservations", "add a due date"]
        followups = store.followups_of(parent.run_id)
        assert [f["prompt"] for f in followups] == ["add reservations", "add a due date"]
    finally:
        store.shutdown()
        get_settings.cache_clear()


# ------------------------------------------------------------------- the API ---
def test_the_api_queues_a_follow_up_and_lists_it(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from app.config import get_settings
    from app.main import create_app
    from app.services import runs as runs_mod

    monkeypatch.setenv("RUNS_DIR", str(tmp_path / "runs"))
    get_settings.cache_clear()
    seen: list[dict] = []

    def fake_run_team(requirement, out_dir, on_event=None, run_id=None, **kw):
        from pathlib import Path

        from schemas.messages import GraphState, UserStories

        out = Path(out_dir)
        (out / "code").mkdir(parents=True, exist_ok=True)
        (out / "stories.json").write_text(
            UserStories(
                stories=[{"id": "US1", "title": "T", "acceptance": ["x"]}]
            ).model_dump_json()
        )
        (out / "code" / "app.py").write_text("x = 1\n")
        seen.append(kw)
        st = GraphState(requirement=requirement, run_id=run_id or out.name, status="accepted")
        if on_event:
            on_event("final", "done", {"status": "accepted"})
        return st

    monkeypatch.setattr(runs_mod, "run_team", fake_run_team)
    app = create_app()
    try:
        with TestClient(app) as client:
            parent = client.post("/api/runs", json={"requirement": "Build a library API"}).json()
            deadline = time.time() + 20
            while time.time() < deadline:
                detail = client.get(f"/api/runs/{parent['run_id']}").json()
                if detail["status"] not in {"queued", "running"}:
                    break
                time.sleep(0.1)
            resp = client.post(
                f"/api/runs/{parent['run_id']}/followups",
                json={"prompt": "add a reservation endpoint"},
            )
            assert resp.status_code == 202, resp.text
            child_id = resp.json()["run_id"]
            assert child_id != parent["run_id"]
            detail = client.get(f"/api/runs/{parent['run_id']}").json()
            assert [f["prompt"] for f in detail["followups"]] == ["add a reservation endpoint"]
            # The child carries the change and the parent it continues.
            deadline = time.time() + 20
            while time.time() < deadline:
                if seen:
                    break
                time.sleep(0.1)
            assert seen, "the follow-up never ran"
            assert seen[-1]["followup"] == "add a reservation endpoint"
            assert seen[-1]["followup_of"] == parent["run_id"]
    finally:
        get_settings.cache_clear()


def test_an_empty_follow_up_is_refused(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from app.config import get_settings
    from app.main import create_app

    monkeypatch.setenv("RUNS_DIR", str(tmp_path / "runs"))
    get_settings.cache_clear()
    app = create_app()
    try:
        with TestClient(app) as client:
            parent = client.post("/api/runs", json={"requirement": "Build a library API"}).json()
            resp = client.post(f"/api/runs/{parent['run_id']}/followups", json={"prompt": "   "})
            assert resp.status_code == 422
    finally:
        get_settings.cache_clear()
