"""Agent contracts: spec validation, plan handling, and fail-loud behaviour.

The central regression under test is that a failed generation must raise rather
than substitute a fixed task-manager answer.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agents import designer, developer, llm, pm, tester
from agents.blocks import parse_file_blocks, strip_fence
from schemas.messages import ApiSpec, UserStories


def _stories() -> UserStories:
    return UserStories(
        stories=[{"id": "US1", "title": "Borrow", "acceptance": ["POST /loans 201"]}]
    )


# ------------------------------------------------------------------ blocks ---
def test_strip_fence_returns_inner_body():
    assert strip_fence("```python\nimport main\n```").strip() == "import main"


def test_strip_fence_passes_through_bare_code():
    assert strip_fence("import main") == "import main"


def test_parse_named_file_blocks():
    text = "### app.py\n```python\nx = 1\n```\n\n### models.py\n```python\ny = 2\n```"
    files = parse_file_blocks(text)
    assert set(files) == {"app.py", "models.py"}
    assert "x = 1" in files["app.py"]


def test_parse_requirements_block():
    text = "### requirements.txt\n```\nfastapi\nsqlalchemy\n```"
    assert parse_file_blocks(text)["requirements.txt"].strip() == "fastapi\nsqlalchemy"


def test_parse_unnamed_fence_becomes_single_file():
    files = parse_file_blocks("```python\ndef test_x():\n    pass\n```")
    assert len(files) == 1
    assert "def test_x" in next(iter(files.values()))


# ---------------------------------------------------------------- designer ---
def test_valid_non_task_spec_is_accepted():
    """The old gate rejected anything without /tasks. It must not."""
    spec = """openapi: 3.1.0
info: {title: Library API, version: 1.0.0}
paths:
  /loans:
    post:
      responses: {'201': {description: created}}
"""
    ok, why = designer._valid(spec)
    assert ok, why


@pytest.mark.parametrize(
    "bad,fragment",
    [
        ("just some prose", "not a mapping"),
        ("paths:\n  /a: {}", "openapi"),
        ("openapi: 3.1.0", "paths"),
        ("openapi: 3.1.0\npaths: {}\n", "paths"),
        ("openapi: 3.1.0\npaths:\n  health: {get: {}}\n", "invalid path key"),
    ],
)
def test_malformed_specs_are_rejected(bad, fragment):
    ok, why = designer._valid(bad)
    assert not ok
    assert fragment in why


def test_designer_extracts_plan(monkeypatch):
    reply = """Here is the spec:
```yaml
openapi: 3.1.0
info: {title: L, version: 1.0.0}
paths:
  /loans:
    post:
      responses: {'201': {description: ok}}
```
```json
{"entrypoint": "service.py", "files": ["service.py", "models.py"]}
```
"""
    monkeypatch.setattr(designer, "require", lambda *a, **k: (reply, {"ok": True}))
    spec = designer.stories_to_spec(_stories(), requirement="Build a REST API for a library that lends books")
    assert spec.entrypoint == "service.py"
    assert spec.files == ["service.py", "models.py"]
    assert "/loans" in spec.endpoints


def test_designer_defaults_plan_when_model_omits_it(monkeypatch):
    reply = "```yaml\nopenapi: 3.1.0\ninfo: {title: L, version: 1.0.0}\npaths:\n  /loans:\n    post:\n      responses: {'201': {description: ok}}\n```"
    monkeypatch.setattr(designer, "require", lambda *a, **k: (reply, {"ok": True}))
    spec = designer.stories_to_spec(_stories(), requirement="Build a REST API for a library that lends books")
    assert spec.entrypoint.endswith(".py")
    assert spec.entrypoint in spec.files


def test_designer_raises_after_three_bad_attempts(monkeypatch):
    calls = {"n": 0}

    def always_bad(*a, **k):
        calls["n"] += 1
        return ("```yaml\nnot: a-spec\n```", {"ok": True})

    monkeypatch.setattr(designer, "require", always_bad)
    with pytest.raises(llm.GenerationError, match="valid OpenAPI"):
        designer.stories_to_spec(_stories(), requirement="Build a REST API for a library that lends books")
    assert calls["n"] == 3


def test_designer_has_no_task_manager_template():
    """The hardcoded task-manager spec must not come back."""
    assert not hasattr(designer, "TEMPLATE_SPEC")


# --------------------------------------------------------------- developer ---
APP_REPLY = """### app.py
```python
from fastapi import FastAPI
app = FastAPI()

@app.get("/health")
def health():
    return {"status": "ok"}
```
"""


def test_developer_uses_the_planned_entrypoint(monkeypatch):
    spec = ApiSpec(openapi_yaml="openapi: 3.1.0", entrypoint="service.py", files=["service.py"])
    reply = APP_REPLY.replace("app.py", "service.py")
    monkeypatch.setattr(developer, "require", lambda *a, **k: (reply, {"ok": True}))
    bundle = developer.spec_to_code(spec)
    assert set(bundle.files) == {"service.py"}
    assert bundle.entry_module == "service"
    assert bundle.entry_attr == "app"


def test_developer_rejects_missing_entrypoint(monkeypatch):
    spec = ApiSpec(openapi_yaml="x", entrypoint="service.py", files=["service.py"])
    monkeypatch.setattr(developer, "require", lambda *a, **k: (APP_REPLY, {"ok": True}))
    with pytest.raises(llm.GenerationError, match="entrypoint"):
        developer.spec_to_code(spec)


def test_developer_rejects_bundle_without_app_object(monkeypatch):
    spec = ApiSpec(openapi_yaml="x", entrypoint="app.py", files=["app.py"])
    reply = "### app.py\n```python\nx = 1\n```"
    monkeypatch.setattr(developer, "require", lambda *a, **k: (reply, {"ok": True}))
    with pytest.raises(llm.GenerationError, match="FastAPI"):
        developer.spec_to_code(spec)


def test_developer_rejects_empty_generation(monkeypatch):
    spec = ApiSpec(openapi_yaml="x", entrypoint="app.py", files=["app.py"])
    monkeypatch.setattr(developer, "require", lambda *a, **k: ("", {"ok": False}))
    with pytest.raises(llm.GenerationError):
        developer.spec_to_code(spec)


def test_developer_drops_files_outside_the_plan(monkeypatch):
    spec = ApiSpec(openapi_yaml="x", entrypoint="app.py", files=["app.py", "models.py"])
    reply = APP_REPLY + "\n### scratch.py\n```python\nprint('junk')\n```"
    monkeypatch.setattr(developer, "require", lambda *a, **k: (reply, {"ok": True}))
    bundle = developer.spec_to_code(spec)
    assert "scratch.py" not in bundle.files


def test_developer_prompt_lists_the_planned_files(monkeypatch):
    spec = ApiSpec(openapi_yaml="x", entrypoint="service.py", files=["service.py", "models.py"])
    seen = {}

    def spy(prompt, *a, **k):
        seen["prompt"] = prompt
        return (APP_REPLY.replace("app.py", "service.py"), {"ok": True})

    monkeypatch.setattr(developer, "require", spy)
    developer.spec_to_code(spec)
    assert "service.py" in seen["prompt"]
    assert "models.py" in seen["prompt"]


def test_patch_prompt_contains_current_code(monkeypatch):
    from schemas.messages import CodeBundle, TestFailure, TestReport

    seen = {}

    def spy(prompt, *a, **k):
        seen["prompt"] = prompt
        return (APP_REPLY, {"ok": True})

    monkeypatch.setattr(developer, "require", spy)
    code = CodeBundle(files={"app.py": "MARKER_SOURCE_LINE"}, entry_module="app", entry_attr="app")
    developer.patch_code(
        code,
        TestReport(passed=0, failed=1, failures=[TestFailure(name="t", error="e")]),
        "a reflection",
    )
    assert "MARKER_SOURCE_LINE" in seen["prompt"]


def test_patch_returns_original_when_llm_fails(monkeypatch):
    from schemas.messages import CodeBundle, TestFailure, TestReport

    def boom(*a, **k):
        raise llm.GenerationError("nope")

    monkeypatch.setattr(developer, "require", boom)
    code = CodeBundle(files={"app.py": "x = 1"}, entry_module="app", entry_attr="app")
    out = developer.patch_code(
        code, TestReport(passed=0, failed=1, failures=[TestFailure(name="t", error="e")]), "r"
    )
    assert out.files == code.files


def test_developer_has_no_template_bundle():
    assert not hasattr(developer, "_template_bundle")
    assert not hasattr(developer, "MAIN_PY")


# ------------------------------------------------------------------- tester ---
def test_tester_prompt_names_the_discovered_entrypoint(monkeypatch):
    seen = {}

    def spy(prompt, *a, **k):
        seen["prompt"] = prompt
        return (
            "```python\nimport service\nfrom fastapi.testclient import TestClient\n"
            "def test_h():\n    assert TestClient(service.app).get('/health').status_code == 200\n```",
            {"ok": True},
        )

    monkeypatch.setattr(tester, "require", spy)
    tester.spec_to_tests(
        ApiSpec(openapi_yaml="openapi: 3.1.0"), _stories(), entry_module="service", entry_attr="app"
    )
    assert "service" in seen["prompt"]


def test_tester_rejects_a_self_testing_suite(monkeypatch):
    """A suite that builds its own app passes without testing the real one."""
    stub = """```python
from fastapi import FastAPI
from fastapi.testclient import TestClient
app = FastAPI()
def test_h():
    assert True
```"""
    monkeypatch.setattr(tester, "require", lambda *a, **k: (stub, {"ok": True}))
    with pytest.raises(llm.GenerationError, match="does not import"):
        tester.spec_to_tests(ApiSpec(openapi_yaml="x"), _stories(), entry_module="app")


def test_tester_requires_an_entrypoint(monkeypatch):
    with pytest.raises(llm.GenerationError, match="entrypoint"):
        tester.spec_to_tests(ApiSpec(openapi_yaml="x"), _stories(), entry_module="")


def test_tester_requires_test_functions(monkeypatch):
    monkeypatch.setattr(
        tester, "require", lambda *a, **k: ("import app\nprint('hi')", {"ok": True})
    )
    with pytest.raises(llm.GenerationError, match="no test functions"):
        tester.spec_to_tests(ApiSpec(openapi_yaml="x"), _stories(), entry_module="app")


def test_tester_has_no_task_template():
    assert not hasattr(tester, "TEMPLATE_TESTS")


# ----------------------------------------------------------------------- pm ---
def test_pm_parses_json_wrapped_in_prose(monkeypatch):
    reply = 'Sure! Here are the stories:\n```json\n{"stories": [{"id": "US1", "title": "Borrow", "acceptance": ["201"]}]}\n```'
    monkeypatch.setattr(pm, "require", lambda *a, **k: (reply, {"ok": True}))
    stories = pm.requirement_to_stories("a library that lends books")
    assert stories.stories[0].title == "Borrow"


def test_pm_raises_instead_of_inventing_task_stories(monkeypatch):
    def boom(*a, **k):
        raise llm.GenerationError("no backend")

    monkeypatch.setattr(pm, "require", boom)
    with pytest.raises(llm.GenerationError):
        pm.requirement_to_stories("a library that lends books")


def test_pm_retries_once_on_unparseable_json(monkeypatch):
    replies = iter(
        ["not json at all", '{"stories": [{"id": "US1", "title": "T", "acceptance": []}]}']
    )
    calls = {"n": 0}

    def flaky(*a, **k):
        calls["n"] += 1
        return (next(replies), {"ok": True})

    monkeypatch.setattr(pm, "require", flaky)
    assert pm.requirement_to_stories("x").stories[0].title == "T"
    assert calls["n"] == 2


# ------------------------------------------------------------- lint repair ---
def test_repair_lint_feeds_the_exact_ruff_error(monkeypatch):
    """Undefined names are repaired from ruff output, not from pytest churn."""
    seen = {}

    def spy(prompt, *a, **k):
        seen["prompt"] = prompt
        return (
            "### app.py\n```python\nfrom fastapi import FastAPI\nfrom sqlalchemy import Boolean\napp = FastAPI()\n```",
            {"ok": True},
        )

    monkeypatch.setattr(developer, "require", spy)
    from schemas.messages import CodeBundle

    code = CodeBundle(
        files={
            "app.py": "from fastapi import FastAPI\napp = FastAPI()\nclass X:\n    c = Boolean()\n"
        },
        entry_module="app",
        entry_attr="app",
    )
    out = developer.repair_lint(code, "app.py:4:20: F821 Undefined name `Boolean`")
    assert "F821" in seen["prompt"]
    assert "Boolean" in out.files["app.py"]


def test_repair_lint_is_a_no_op_when_clean(monkeypatch):
    def must_not_run(*a, **k):
        raise AssertionError("no repair needed")

    monkeypatch.setattr(developer, "require", must_not_run)
    from schemas.messages import CodeBundle

    code = CodeBundle(files={"app.py": "app = FastAPI()\n"}, entry_module="app", entry_attr="app")
    assert developer.repair_lint(code, "All checks passed!").files == code.files


def test_repair_lint_stops_when_the_model_returns_the_same_code(monkeypatch):
    """A repair that changes nothing must not loop."""
    monkeypatch.setattr(
        developer,
        "require",
        lambda *a, **k: (
            "### app.py\n```python\nfrom fastapi import FastAPI\napp = FastAPI()\n```",
            {"ok": True},
        ),
    )
    from schemas.messages import CodeBundle

    src = "from fastapi import FastAPI\napp = FastAPI()\n"
    code = CodeBundle(files={"app.py": src}, entry_module="app", entry_attr="app")
    calls = {"n": 0}

    def counting(*a, **k):
        calls["n"] += 1
        return ("### app.py\n```python\n" + src + "```", {"ok": True})

    monkeypatch.setattr(developer, "require", counting)
    out = developer.repair_lint(code, "app.py:1:1: F821 Undefined name `Boolean`")
    assert out.files == code.files
    assert calls["n"] == 1, "must not retry an unchanged repair"


def test_trailing_whitespace_only_is_not_a_repair():
    """The parser strips fences, so a re-emitted file can differ by a newline."""
    from agents.developer import _meaningful_change

    src = {"app.py": "import os\n"}
    assert _meaningful_change(src, {"app.py": "import os"}) is False
    assert _meaningful_change(src, {"app.py": "import sys\n"}) is True
    assert _meaningful_change(src, {"app.py": "import os\n", "b.py": "x=1\n"}) is True


# ------------------------------------------------------------------ triage ---
def test_triage_routes_error_body_shape_assertions_to_the_tester(monkeypatch):
    """A test demanding an envelope the spec never defined is a test bug.

    Real signature from a run where the app correctly returned 404 but with
    FastAPI's {"detail": ...} while the test demanded a top-level "error".
    Routing that to the Developer makes it rewrite correct code.
    """
    monkeypatch.setattr(pm, "require", lambda *a, **k: ("code_bug", {"ok": True}))
    from schemas.messages import TestFailure, TestReport

    report = TestReport(
        passed=4,
        failed=4,
        failures=[
            TestFailure(
                name="test_borrow_unknown_isbn",
                error="AssertionError: assert 'error' in {'detail': {'error': 'Book not found'}}",
            )
        ],
    )
    assert pm.triage(report) == "test_bug"


@pytest.mark.parametrize(
    "error",
    [
        "sqlalchemy.exc.OperationalError: no such table: loans",
        "ModuleNotFoundError: No module named 'models'",
        "AssertionError: assert 201 == 200",
        "AttributeError: 'NoneType' object has no attribute 'id'",
    ],
)
def test_triage_keeps_real_code_bugs_with_the_developer(monkeypatch, error):
    monkeypatch.setattr(pm, "require", lambda *a, **k: ("code_bug", {"ok": True}))
    from schemas.messages import TestFailure, TestReport

    report = TestReport(passed=4, failed=1, failures=[TestFailure(name="t", error=error)])
    assert pm.triage(report) == "code_bug", error


def test_tester_prompt_forbids_over_specified_error_bodies(monkeypatch):
    """The root cause of the misrouted run: the Tester over-specified."""
    seen = {}

    def spy(prompt, *a, **k):
        seen["prompt"] = prompt
        return (
            "```python\nimport app\nfrom fastapi.testclient import TestClient\n"
            "def test_h():\n    assert TestClient(app.app).get('/health').status_code == 200\n```",
            {"ok": True},
        )

    monkeypatch.setattr(tester, "require", spy)
    tester.spec_to_tests(
        ApiSpec(openapi_yaml="openapi: 3.1.0"), _stories(), entry_module="app", entry_attr="app"
    )
    sop = tester._API_SOP
    assert "shape of an error body" in sop
    assert "status code" in sop


def test_triage_routes_impossible_error_assertion_to_the_tester(monkeypatch):
    """A test demanding 4xx for a request that legitimately returns 200.

    Real signature from a deployed run: the suite did GET /appointments/ and
    asserted 4xx, but a trailing-slash request on a collection is a valid list
    request. No implementation can satisfy it.
    """
    monkeypatch.setattr(pm, "require", lambda *a, **k: ("code_bug", {"ok": True}))
    from schemas.messages import TestFailure, TestReport

    report = TestReport(
        passed=9,
        failed=1,
        failures=[
            TestFailure(
                name="test_get_appointment_by_id_boundary_empty_string",
                error=(
                    "assert 400 <= resp.status_code < 500\n"
                    "E  assert 400 <= 200\n"
                    "E   + where 200 = <Response [200 OK]>.status_code"
                ),
            )
        ],
    )
    assert pm.triage(report) == "test_bug"


@pytest.mark.parametrize(
    "error",
    [
        "sqlalchemy.exc.OperationalError: no such table: appointments",
        "ModuleNotFoundError: No module named 'models'",
        "AssertionError: assert 404 == 200",
        "AssertionError: assert 201 == 200",
        "AttributeError: 'NoneType' object has no attribute 'id'",
    ],
)
def test_triage_never_misroutes_real_code_bugs(monkeypatch, error):
    """The deterministic signatures must not swallow genuine code bugs."""
    monkeypatch.setattr(pm, "require", lambda *a, **k: ("code_bug", {"ok": True}))
    from schemas.messages import TestFailure, TestReport

    report = TestReport(passed=9, failed=1, failures=[TestFailure(name="t", error=error)])
    assert pm.triage(report) == "code_bug", error


PICKLE_SESSION_RAW = (
    "copy.py:162: in deepcopy y = _reconstruct(x, memo, *rv) "
    "sqlalchemy.orm.session.Session object identity_map "
    "x = <module 'sqlite3.dbapi2'> "
    "TypeError: cannot pickle 'module' object"
)


def test_triage_routes_unpicklable_session_to_the_developer(monkeypatch):
    """All 9 tests dying in deepcopy of a Session is a code bug, full stop.

    Real signature from run-20261009-131219: every handler took
    ``db=SessionLocal()`` as a default, FastAPI deepcopied the session while
    building each route, and nothing could pass. The LLM triage misrouted this
    to the Tester twice, so the deterministic rule must overrule the model.
    """
    monkeypatch.setattr(pm, "require", lambda *a, **k: ("test_bug", {"ok": True}))
    from schemas.messages import TestFailure, TestReport

    report = TestReport(
        passed=0,
        failed=9,
        failures=[
            TestFailure(name="t", error="TypeError: cannot pickle 'module' object")
        ],
        raw=PICKLE_SESSION_RAW,
    )
    assert pm.triage(report) == "code_bug"


def test_reflect_diagnoses_session_default_argument_without_llm():
    """The reflection must name the SessionLocal() default-arg cause directly.

    The truncated one-liner ("TypeError: cannot pickl...") hides it, so the
    diagnosis reads the runner output, not the model.
    """
    from schemas.messages import TestFailure, TestReport

    report = TestReport(
        passed=0,
        failed=9,
        failures=[
            TestFailure(name="t", error="TypeError: cannot pickle 'module' object")
        ],
        raw=PICKLE_SESSION_RAW,
    )
    text = developer.reflect(report)
    assert "SessionLocal()" in text
    assert "Depends(get_db)" in text


def test_reviewer_blocks_session_as_handler_default():
    """``db=SessionLocal()`` on a route handler must fail review as a blocker."""
    from agents import reviewer
    from schemas.messages import CodeBundle, TestReport, TestSuite

    code = CodeBundle(
        files={
            "main.py": (
                "from fastapi import FastAPI\n"
                "app = FastAPI()\n"
                "@app.get('/x')\n"
                "def read_x(db=SessionLocal()):\n"
                "    return []\n"
            )
        },
        entry_module="main",
        entry_attr="app",
    )
    spec = ApiSpec(openapi_yaml="x", entrypoint="main.py", files=["main.py"])
    comments = reviewer._api_checks(
        code, spec, TestSuite(files={}), TestReport(passed=0, failed=0)
    )
    blockers = [c for c in comments if c.severity == "blocker"]
    assert any("Depends(get_db)" in c.message for c in blockers)


def test_reviewer_allows_depends_injected_session():
    """``db: Session = Depends(get_db)`` must not trip the session check."""
    from agents import reviewer
    from schemas.messages import CodeBundle, TestReport, TestSuite

    code = CodeBundle(
        files={
            "main.py": (
                "from fastapi import Depends, FastAPI\n"
                "app = FastAPI()\n"
                "@app.get('/x')\n"
                "def read_x(db=Depends(get_db)):\n"
                "    return []\n"
            )
        },
        entry_module="main",
        entry_attr="app",
    )
    spec = ApiSpec(openapi_yaml="x", entrypoint="main.py", files=["main.py"])
    comments = reviewer._api_checks(
        code, spec, TestSuite(files={}), TestReport(passed=0, failed=0)
    )
    assert not [c for c in comments if "default argument" in c.message]


def test_tester_prompt_forbids_impossible_error_assertions():
    assert "must be satisfiable" in tester._API_SOP
    assert "valid list request and returns" in tester._API_SOP


def test_cloud_order_supports_provider_chains(monkeypatch):
    """LLM_PROVIDER=groq,gemini tries Groq first, Gemini only on decline."""
    monkeypatch.setenv("LLM_PROVIDER", "groq,gemini")
    assert [name for _, name in llm.cloud_order()] == ["groq/free", "gemini"]
    monkeypatch.setenv("LLM_PROVIDER", "groq")
    assert [name for _, name in llm.cloud_order()] == ["groq/free"]
    monkeypatch.setenv("LLM_PROVIDER", "bogus,gemini")
    assert [name for _, name in llm.cloud_order()] == ["gemini"]
    monkeypatch.setenv("LLM_PROVIDER", "bogus")
    assert len(llm.cloud_order()) > 2, "unknown name falls back to auto"
