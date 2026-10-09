"""Tester: spec + stories -> pytest suite. NEVER reads code (AgentCoder rule).

The 63-line task-manager suite that used to live here is gone. It was the
fallback whenever generation failed, which meant a library request was graded
by task-manager tests against a task-manager app. Now the suite is generated
from the spec, bound to the real entrypoint discovered from the code, and a
generation failure raises rather than substituting anything.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from agents.blocks import strip_fence
from agents.introspect import declared_status_codes, public_surface, spec_paths
from agents.llm import GenerationError, require
from schemas.messages import ApiSpec, CodeBundle, TestSuite, UserStories

_API_SOP = (
    "You are the Tester. Write ONE pytest file for the application described by the "
    "spec below. You have NOT seen the implementation and must infer behaviour from "
    "the spec alone.\n"
    "Hard requirements:\n"
    "- The application module is `{module}` and the ASGI app object is `{module}.{attr}`.\n"
    "- Start with `import {module}` and use `{module}.{attr}` with fastapi.testclient.TestClient.\n"
    "- NEVER construct your own FastAPI() or APIRouter(); that would test a stub, not the app.\n"
    "- Point the app at a throwaway SQLite file via the database path the code reads, "
    "and delete stale test databases before importing.\n"
    "- Cover, for every endpoint: the success path, the validation/error status codes the "
    "spec declares (e.g. 404 for unknown ids, 422 for missing fields), and at least one "
    "boundary case.\n"
    "Assert ONLY what the spec states. In particular:\n"
    "- Read the spec's validation rules BEFORE writing a success-path test. Every declared "
    "constraint must hold in your payload: required fields present, formats valid, values in "
    "range, timestamps in the future, cross-field rules honoured (e.g. two ids must differ). "
    "A payload that violates a declared rule gets the error status, not the success one.\n"
    "- Do not assert on the *shape of an error body* (keys like 'error' or 'detail'). The spec "
    "fixes the status code, not the envelope.\n"
    "- Only assert a 4xx status for a request the spec declares invalid. A genuinely valid "
    "request must be asserted to SUCCEED; `GET /items/` with a trailing slash is a valid "
    "list request and returns 200.\n"
    "- Do not assert an exact status code the spec does not declare.\n"
    "- Prefer asserting on data the spec describes over incidental response keys.\n"
    "Every test you write must be satisfiable by a correct implementation of this spec.\n"
    "Output ONE fenced ```python block containing only the test file. No prose."
)

_PROGRAM_SOP = (
    "You are the Tester. Write ONE pytest file for the Python program described by the "
    "contract below. It is NOT a web service: there is no HTTP layer, no FastAPI app and "
    "no TestClient. Test the program's logic directly.\n"
    "Hard requirements:\n"
    "- Import the public names from the modules that define them:\n{public_api}\n"
    "- Use ONLY the exact names listed above. Do NOT invent getter methods, helper "
    "functions, or convenience wrappers that are not in the list. If the contract lists "
    "'LudoGame', construct LudoGame() and call its methods directly — do not assume "
    "get_current_player() or get_board_state() exist unless they are listed.\n"
    "- Access attributes directly when the contract does not list a getter (e.g. "
    "game.current_player, not game.get_current_player()).\n"
    "- Do NOT guess the internal shape of returned data. If the contract does not name a "
    'field (e.g. a token\'s "id" key or a "status" field), do not index into it. Test '
    "through the declared behaviours and public methods instead, or assert on values the "
    "methods themselves return.\n"
    "- Functions return plain Python values (ints, strings, lists, dicts, objects). Do NOT "
    "expect HTTP-style responses: no status codes, no {{'status': 200}} wrappers, no string "
    "token IDs unless the contract explicitly declares them. If the contract says "
    "move_token returns True/False, assert on that boolean — not on a dict.\n"
    "- Importing those modules must have no side effects: no printing, no input(), no "
    "infinite loops, no network or filesystem writes at import time.\n"
    "- Drive real logic with real calls. Never re-implement the program inside the test "
    "and never assert a tautology.\n"
    "- Follow arrange-act-assert: every assertion about changed state must come after "
    "the method call that changes it. Rolling the dice does not move a token; only "
    "assert a new position after calling the move method.\n"
    "- Control randomness explicitly. If the program uses randomness, seed it "
    "(e.g. random.seed(0)) so tests are deterministic.\n"
    "- For each required behaviour, write at least one test that would fail if that "
    "behaviour were broken. Cover the main path plus edge cases: empty state, invalid "
    "input, boundary values, win/lose conditions, and state that must not change.\n"
    "- Test properties rather than incidental details: types and ranges, membership, "
    "counts, ordering, and that illegal actions are rejected.\n"
    "Output ONE fenced ```python block containing only the test file. No prose."
)


_FABRICATES_APP = re.compile(r"=\s*(?:FastAPI|APIRouter)\s*\(", re.M)
_IMPORTS_TARGET = re.compile(r"^\s*(?:import\s+{m}\b|from\s+{m}\s+import)", re.M)


def _tests_the_real_app(body: str, module: str) -> bool:
    """True when the suite exercises the generated app rather than a stub.

    The Tester never sees the code, so it will sometimes inline its own FastAPI
    app. Those tests pass while the real application is never imported, which
    makes the whole gate meaningless.
    """
    pattern = _IMPORTS_TARGET.pattern.format(m=re.escape(module))
    return re.search(pattern, body, re.M) is not None and not _FABRICATES_APP.search(body)


def _status_codes_in_spec(spec_yaml: str) -> set[str]:
    """Error codes the spec declares, so the Tester is told to cover them."""
    return {c for c in declared_status_codes(spec_yaml) if c not in ("200", "201", "204")}


def spec_to_tests(
    spec: ApiSpec,
    stories: UserStories,
    entry_module: str = "main",
    entry_attr: str = "app",
    code: CodeBundle | None = None,
) -> TestSuite:
    """Generate the suite. Raises ``GenerationError`` on failure.

    ``entry_module``/``entry_attr`` come from inspecting the generated code, so
    a module named ``app.py`` with ``app = FastAPI()`` is handled without any
    special case for a name. A non-service project skips HTTP entirely.
    """
    listing = "\n".join(f"- {s.title}: {'; '.join(s.acceptance)}" for s in stories.stories)
    if spec.kind == "program":
        return _program_tests(spec, listing, code=code)
    return _api_tests(spec, listing, entry_module, entry_attr)


def _api_tests(spec: ApiSpec, listing: str, entry_module: str, entry_attr: str) -> TestSuite:
    if not entry_module:
        raise GenerationError("Tester needs the application entrypoint, which was not resolved")
    pairs, _paths = spec_paths(spec.openapi_yaml)
    endpoint_list = (
        "\n".join(f"- {m} {p}" for p, m in sorted(pairs)) or "(read from the spec below)"
    )
    codes = sorted(_status_codes_in_spec(spec.openapi_yaml))
    prompt = (
        f"Spec:\n{spec.openapi_yaml[:6000]}\n\n"
        f"Endpoints:\n{endpoint_list}\n\n"
        f"Stories:\n{listing}\n\n"
        f"Error status codes the spec declares: {codes or 'none explicitly'}\n"
        f"Application module: {entry_module} (app object: {entry_module}.{entry_attr})\n"
    )
    text, _meta = require(
        f"{prompt}Emit the pytest file.",
        _API_SOP.format(module=entry_module, attr=entry_attr),
        role="tester",
    )
    body = strip_fence(text)
    if "def test_" not in body:
        raise GenerationError("Tester produced no test functions")
    if not _tests_the_real_app(body, entry_module):
        raise GenerationError(
            f"Tester suite does not import {entry_module!r} or declares its own FastAPI app, "
            "so it would not exercise the generated application"
        )
    return TestSuite(files={"test_app.py": body})


def _program_tests(spec: ApiSpec, listing: str, code: CodeBundle | None = None) -> TestSuite:
    """Tests for a self-contained program: import the public names and assert."""
    if not spec.public_api:
        raise GenerationError("Tester needs a declared public interface to import")
    public = "\n".join(f"- {name}" for name in spec.public_api)
    try:
        behaviours = (json.loads(spec.contract_text) or {}).get("behaviours") or []
    except Exception:
        behaviours = []
    behaviour_lines = "\n".join(f"- {b}" for b in behaviours) or "- see the contract"
    prompt = (
        f"Contract:\n{spec.contract_text[:6000]}\n\n"
        f"Public names to import (defined across these modules):\n{public}\n\n"
        f"Required behaviours, each needing at least one test:\n{behaviour_lines}\n\n"
        f"Expected behaviours from the requirement:\n{listing}\n\n"
        f"Entrypoint module: {spec.entrypoint[:-3] if spec.entrypoint.endswith('.py') else spec.entrypoint}\n"
    )
    if code is not None:
        surface = public_surface(code.files, spec.public_api)
        if surface:
            # Signatures only, no bodies: enough to know what a call returns,
            # not enough to write an implementation-mirroring test.
            prompt += (
                "\nThe declared interface as it actually exists (signatures and docstring "
                f"leads only):\n{surface}\n\nMatch your assertions to these signatures. Do not "
                "assume a different data shape.\n"
            )
    text, _meta = require(
        f"{prompt}Emit the pytest file.",
        _PROGRAM_SOP.format(public_api=public),
        role="tester",
    )
    body = strip_fence(text)
    if "def test_" not in body:
        raise GenerationError("Tester produced no test functions")
    if not _imports_public_api(body, spec):
        raise GenerationError(
            "Tester suite does not import the declared public interface, so it would not "
            "exercise the generated program"
        )
    if re.search(r"=\s*(?:FastAPI|APIRouter)\s*\(", body):
        raise GenerationError(
            "Tester suite builds its own web app, but this project is a program, not a service"
        )
    return TestSuite(files={"test_program.py": body})


def _imports_public_api(body: str, spec: ApiSpec) -> bool:
    """True when the suite imports something from the program's own modules.

    A suite that imports nothing from the project cannot be testing it. Only the
    top-level package/module of each declared symbol is checked, because the
    contract names dotted paths whose defining module the Tester has to infer.
    """
    modules = {
        Path(f).stem
        for f in spec.files
        if f.endswith(".py") and Path(f).stem not in {"__init__", "requirements"}
    }
    for module in modules:
        if re.search(
            rf"^\s*(?:import\s+{re.escape(module)}\b|from\s+{re.escape(module)}\s+import)",
            body,
            re.M,
        ):
            return True
    return False
