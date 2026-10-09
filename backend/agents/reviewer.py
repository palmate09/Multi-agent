"""Reviewer: code + spec + tests + ruff/bandit -> ranked comments.

Every check here is derived from the spec, the stories or the code itself.
The previous version grepped for ``/tasks``, ``min_length`` and 422/404
literals, which made a correct library API fail review with "Missing endpoint
/tasks" while a task manager passed regardless of what was actually built.
"""

from __future__ import annotations

import ast
import re

from agents.introspect import (
    all_routes,
    declared_status_codes,
    normalise_path,
    uncovered_spec_endpoints,
)
from agents.llm import GenerationError, generate
from schemas.messages import ApiSpec, CodeBundle, ReviewComment, ReviewReport, TestReport, TestSuite

# Patterns that are unsafe in any domain.
_UNSAFE = re.compile(
    r"\beval\s*\(|\bexec\s*\(|f['\"].{0,80}?\bSELECT\b|f['\"].{0,80}?\bINSERT\b", re.I | re.S
)


def _all_code(code: CodeBundle) -> str:
    return "\n".join(code.files.values())


def _has_orm(code: CodeBundle) -> bool:
    """Persistence is expected, whatever the model called the layer."""
    blob = _all_code(code).lower()
    return any(
        marker in blob
        for marker in ("sqlalchemy", "sqlite3", "create_engine", "sessionmaker", "declarative_base")
    )


def _declares_validation(code: CodeBundle) -> bool:
    """Is there any request validation at all?

    Checked generically: a Pydantic model, or an explicit 422/400 branch. The
    old check demanded the literal string ``min_length``, which only exists in
    one library's idiom.
    """
    for name, body in code.files.items():
        if not name.endswith(".py"):
            continue
        try:
            tree = ast.parse(body)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                for base in node.bases:
                    base_name = base.id if isinstance(base, ast.Name) else getattr(base, "attr", "")
                    if base_name == "BaseModel":
                        return True
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "Field"
            ):
                return True
    blob = _all_code(code)
    return bool(re.search(r"\b(422|400)\b", blob))


def _static_checks(
    code: CodeBundle, spec: ApiSpec, tests: TestSuite, report: TestReport
) -> list[ReviewComment]:
    out: list[ReviewComment] = []

    # 1. Every endpoint the spec declares must exist in the code.
    if spec.openapi_yaml:
        for path, method in uncovered_spec_endpoints(spec.openapi_yaml, code.files):
            out.append(
                ReviewComment(
                    severity="blocker",
                    message=f"Spec declares {method} {path} but no route implements it",
                    location="spec",
                )
            )

    # 2. The app object must be importable, or nothing else matters.
    if not code.entry_module:
        out.append(
            ReviewComment(
                severity="blocker",
                message="No module exposes a FastAPI application object at module level",
                location="code",
            )
        )

    # 3. The declared error codes must actually be raised somewhere.
    body = _all_code(code)
    if spec.openapi_yaml:
        declared = _declared_codes(spec.openapi_yaml) - {"200", "201", "204"}
        for code_str in sorted(declared):
            if code_str not in body:
                out.append(
                    ReviewComment(
                        severity="major",
                        message=f"Spec declares HTTP {code_str} but the code never returns it",
                        location="code",
                    )
                )

    # 4. No dead routes: a route nobody can reach is usually a wiring mistake.
    spec_paths_set = {normalise_path(p) for p, _ in all_routes(code.files)}
    if spec.openapi_yaml:
        _, declared_paths = _spec_path_set(spec.openapi_yaml)
        for path in sorted(declared_paths - spec_paths_set):
            out.append(
                ReviewComment(
                    severity="minor",
                    message=f"Spec describes {path} but the code exposes no route for it",
                    location="spec",
                )
            )

    # 5. Persistence.
    if not _has_orm(code):
        out.append(
            ReviewComment(
                severity="major", message="No database/ORM layer detected", location="code"
            )
        )

    # 6. Request validation must exist in some form.
    if not _declares_validation(code):
        out.append(
            ReviewComment(
                severity="major",
                message="No request validation (no pydantic model and no 4xx branch)",
                location="code",
            )
        )

    # 7. Unsafe constructs, whatever the domain.
    for name, src in code.files.items():
        if name.endswith(".py") and _UNSAFE.search(src):
            out.append(
                ReviewComment(
                    severity="blocker",
                    message="Unsafe pattern (eval/exec or interpolated SQL)",
                    location=name,
                )
            )

    # 8. Tests must exist and cover the declared error codes.
    tests_body = "\n".join(tests.files.values())
    if "def test_" not in tests_body:
        out.append(ReviewComment(severity="blocker", message="No tests found", location="tests"))
    else:
        for needed in sorted({"404", "422"} & _declared_codes(spec.openapi_yaml)):
            if needed not in tests_body:
                out.append(
                    ReviewComment(
                        severity="major",
                        message=f"Tests never exercise HTTP {needed}",
                        location="tests",
                    )
                )

    # 9. The suite must import the app under test.
    if code.entry_module and code.entry_module not in tests_body:
        out.append(
            ReviewComment(
                severity="blocker",
                message=f"Tests never import the application module {code.entry_module!r}",
                location="tests",
            )
        )

    return out


def _declared_codes(spec_yaml: str) -> set[str]:
    """Status codes the spec declares, across every operation."""
    return declared_status_codes(spec_yaml)


def _spec_path_set(spec_yaml: str) -> tuple[set, set[str]]:
    from agents.introspect import spec_paths

    _pairs, paths = spec_paths(spec_yaml)
    return _pairs, {normalise_path(p) for p in paths}


def review(
    code: CodeBundle,
    spec: ApiSpec,
    tests: TestSuite,
    report: TestReport,
    ruff_out: str = "",
    bandit_out: str = "",
) -> ReviewReport:
    """Static findings first, then the LLM may add to them.

    The LLM is consulted for additional findings rather than only when the
    static pass came back empty, which is how it could never contribute before.
    Its suggestions are advisory (major at most) unless a static check
    corroborates them.
    """
    comments = _static_checks(code, spec, tests, report)

    if "E9" in ruff_out or "F821" in ruff_out or "SyntaxError" in (report.raw or ""):
        comments.append(
            ReviewComment(severity="blocker", message="Ruff/syntax failure", location="ruff")
        )
    if "HIGH" in (bandit_out or "").upper() and "Issue" in bandit_out:
        comments.append(
            ReviewComment(
                severity="major", message="Bandit high-severity finding", location="bandit"
            )
        )

    if code.entry_module:
        summary = (
            f"Files: {sorted(code.files)}\n"
            f"Routes: {sorted(all_routes(code.files))}\n"
            f"Entrypoint: {code.entry_module}.{code.entry_attr}\n"
            f"Spec paths: {sorted(_spec_path_set(spec.openapi_yaml)[1])}\n"
            f"Static findings: {[c.message for c in comments]}\n"
        )
        try:
            text, _meta = generate(
                f"{summary}\nList any further blocker/major/minor issues as one per line, "
                "or reply OK if the implementation faithfully matches the spec.",
                "You are a strict code reviewer. Only report issues you can point at.",
                role="reviewer",
            )
        except GenerationError:
            text = ""
        for line in (text or "").splitlines():
            line = line.strip(" -*\t")
            if not line or line.upper().startswith("OK"):
                continue
            lowered = line.lower()
            for level in ("blocker", "major", "minor"):
                if lowered.startswith(level):
                    # Advisory only: an uncorroborated model opinion must not
                    # block acceptance on its own.
                    comments.append(
                        ReviewComment(
                            severity="major" if level == "blocker" else level,
                            message=f"reviewer: {line[:280]}",
                            location="llm",
                        )
                    )
                    break

    if not comments:
        comments.append(
            ReviewComment(
                severity="minor",
                message="No findings: every spec endpoint is implemented and tests cover it.",
                location="review",
            )
        )
    return ReviewReport(comments=comments)
