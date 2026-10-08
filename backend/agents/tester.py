"""Tester: spec + stories -> pytest suite. NEVER reads code (AgentCoder rule).

The 63-line task-manager suite that used to live here is gone. It was the
fallback whenever generation failed, which meant a library request was graded
by task-manager tests against a task-manager app. Now the suite is generated
from the spec, bound to the real entrypoint discovered from the code, and a
generation failure raises rather than substituting anything.
"""

from __future__ import annotations

import re

from agents.blocks import strip_fence
from agents.introspect import declared_status_codes, spec_paths
from agents.llm import GenerationError, require
from schemas.messages import ApiSpec, TestSuite, UserStories

SOP = (
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
    "- Do not assert on the *shape of an error body* (keys like 'error' or 'detail', message "
    "wording). The spec fixes the status code, not the envelope. `assert response.status_code "
    "== 404` is correct; `assert 'error' in response.json()` is not.\n"
    "- Only assert a 4xx status for a request the spec declares invalid: an unknown id, a "
    "missing required field, a blank value. A request that is genuinely valid must be asserted "
    "to SUCCEED. `GET /appointments/` with a trailing slash is a valid list request and returns "
    "200; asserting it fails is an impossible test.\n"
    "- Do not assert an exact status code the spec does not declare. FastAPI answers 422 for a "
    "schema violation where a hand-written API might answer 400; assert only codes the spec "
    "names, or assert the code is a 4xx client error for a request you have already made "
    "invalid.\n"
    "- Prefer asserting on data the spec describes (fields, ids, counts) over incidental "
    "response keys.\n"
    "Every test you write must be satisfiable by a correct implementation of this spec.\n"
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
) -> TestSuite:
    """Generate the suite. Raises ``GenerationError`` on failure.

    ``entry_module``/``entry_attr`` come from inspecting the generated code, so
    a module named ``app.py`` with ``app = FastAPI()`` is handled without any
    special case for a name.
    """
    if not entry_module:
        raise GenerationError("Tester needs the application entrypoint, which was not resolved")
    listing = "\n".join(f"- {s.title}: {'; '.join(s.acceptance)}" for s in stories.stories)
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
        SOP.format(module=entry_module, attr=entry_attr),
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
