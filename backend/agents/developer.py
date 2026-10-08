"""Developer: spec -> CodeBundle.

The template bundle that used to live here (fixed ``database.py`` /
``models.py`` / ``schemas_pyd.py`` / ``main.py``) is gone. It was substituted
whenever generation failed, so a library request silently became a task
manager. Now the file layout comes from the Designer's plan, and a failed
generation raises instead of substituting anything.
"""

from __future__ import annotations

import re

from agents.blocks import parse_file_blocks
from agents.introspect import find_app_object
from agents.llm import GenerationError, require
from schemas.messages import ApiSpec, CodeBundle, TestReport

SOP = (
    "You are the Developer. Implement the given OpenAPI spec as a runnable "
    "FastAPI application.\n"
    "Rules:\n"
    "- Create EXACTLY these files, and no others:\n{file_plan}\n"
    "- The module {entrypoint} must create and expose the ASGI app, e.g. `app = FastAPI()`.\n"
    "- Use SQLAlchemy with SQLite for persistence unless the spec says otherwise.\n"
    "- You MUST create the tables at import time, before any request is served, "
    "with `Base.metadata.create_all(bind=engine)` right after defining the models. "
    "The test database starts out empty and nothing else will create the schema, so "
    "omitting this makes every request fail with 'no such table'.\n"
    "- Return error responses the spec declares (404, 422, 409...).\n"
    "- Import every symbol you use; do not reference an undefined name.\n"
    "- Split modules as listed in the file plan only. Do not invent extra files.\n"
    "Reply with file blocks in this exact format and no prose:\n"
    "### <filename>\n```python\n<complete code>\n```\n"
    "For the dependency list reply with one final block:\n"
    "### requirements.txt\n```\n<one package per line>\n```"
)


def _file_plan_block(spec: ApiSpec) -> tuple[list[str], str]:
    """Return ``(filenames, human-readable plan)`` for the prompt."""
    names = [f for f in (spec.files or []) if f]
    if spec.entrypoint and spec.entrypoint not in names:
        names.insert(0, spec.entrypoint)
    if not names:
        names = [spec.entrypoint or "main.py"]
    plan = "\n".join(f"- {n}" for n in names)
    return names, plan


def _finish(files: dict[str, str], required: list[str]) -> CodeBundle:
    """Trim to the planned files and resolve the entrypoint from the code.

    Files outside the plan are dropped: a model that appends a stray scratch
    file otherwise produces a bundle that fails lint and confuses the Tester
    about what the entrypoint is.
    """
    keep = set(required)
    trimmed = {name: body for name, body in files.items() if name in keep}
    # requirements.txt is always allowed through; the runner needs it.
    if "requirements.txt" in files:
        trimmed.setdefault("requirements.txt", files["requirements.txt"])
    found = find_app_object(trimmed)
    entry_module = entry_attr = ""
    if found:
        entry_module, entry_attr = found
    return CodeBundle(files=trimmed, entry_module=entry_module, entry_attr=entry_attr or "app")


def _meaningful_change(before: dict[str, str], after: dict[str, str]) -> bool:
    """True when ``after`` differs from ``before`` beyond trailing whitespace.

    The file parser strips fences, so a model that re-emits an unchanged file
    returns bodies differing only by a trailing newline. Counting that as a
    repair makes the loop believe it made progress and burns the retry budget
    re-running identical code.
    """
    if set(before) != set(after):
        return True
    return any((before[k] or "").rstrip() != (after[k] or "").rstrip() for k in before)


def _merged(before: dict[str, str], patch: dict[str, str]) -> dict[str, str]:
    merged = dict(before)
    merged.update({k: v for k, v in patch.items() if v.strip()})
    return merged


def spec_to_code(spec: ApiSpec) -> CodeBundle:
    """Generate the application. Raises ``GenerationError`` on failure."""
    required, plan = _file_plan_block(spec)
    prompt = (
        f"OpenAPI spec:\n{spec.openapi_yaml[:6000]}\n\n"
        f"Files to create (exactly these):\n{plan}\n"
        f"Entrypoint module: {required[0]}\n"
    )
    text, _meta = require(
        f"{prompt}Emit the file blocks.",
        SOP.format(file_plan=plan, entrypoint=required[0]),
        role="developer",
    )
    files = parse_file_blocks(text)
    py_files = [n for n in files if n.endswith(".py") and files[n].strip()]
    if not py_files:
        raise GenerationError(
            f"Developer produced no usable python files; planned {required}, got {sorted(files)}"
        )
    # The entrypoint must actually exist or nothing downstream can import the app.
    if required[0] not in files:
        raise GenerationError(
            f"Developer omitted the planned entrypoint {required[0]!r}; got {sorted(py_files)}"
        )
    bundle = _finish(files, required)
    if not bundle.entry_module:
        raise GenerationError(
            f"No module in {sorted(bundle.files)} assigns a FastAPI() app to a module-level "
            "name, so the application cannot be imported or tested"
        )
    return bundle


def repair_lint(code: CodeBundle, ruff_out: str, rounds: int = 2) -> CodeBundle:
    """Fix static errors before pytest is involved.

    Undefined names and syntax errors are the most common small-model mistake
    (a generated app routinely uses ``Boolean`` without importing it) and they
    are cheap to detect: ruff costs about a second, while a failing pytest cycle
    costs a full LLM generation plus a test run. Feeding the exact ruff line
    back is far more reliable than hoping the test output leads the model to the
    missing import.
    """
    if not ruff_out:
        return code
    for _ in range(rounds):
        problems = [
            line
            for line in ruff_out.splitlines()
            if re.search(r"\b(F821|F811|E9\d\d|F401)\b|SyntaxError|undefined name", line)
        ]
        if not problems:
            return code
        detail = "\n".join(problems[:20])
        current = "\n\n".join(
            f"### {name}\n```python\n{body[:5000]}\n```" for name, body in code.files.items()
        )
        try:
            text, _meta = require(
                f"Static analysis errors:\n{detail}\n\nCurrent code:\n{current}\n\n"
                "Reply with ONLY the corrected file blocks. Add the missing imports and fix the "
                "reported errors. Do not change anything else. No prose.",
                SOP.format(
                    file_plan="\n- " + "\n- ".join(sorted(code.files)),
                    entrypoint=code.entry_module or "",
                ),
                role="developer",
            )
        except GenerationError:
            return code
        if not text:
            return code
        merged = _merged(code.files, parse_file_blocks(text))
        if not _meaningful_change(code.files, merged):
            return code
        found = find_app_object(merged) or (code.entry_module, code.entry_attr)
        code = CodeBundle(files=merged, entry_module=found[0], entry_attr=found[1] or "app")
    return code


def reflect(report: TestReport) -> str:
    """2-3 sentences on root cause and fix direction (Reflexion)."""
    blob = "; ".join(f"{f.name}: {f.error}" for f in report.failures[:5])
    try:
        text, _meta = require(
            f"Failures: {blob}\nIn 2-3 sentences: the root cause and the fix direction.",
            "You reflect on test failures of a FastAPI app.",
            role="reflection",
        )
        if text and len(text.strip()) > 20:
            return " ".join(text.strip().split())[:600]
    except GenerationError:
        pass
    if report.failures:
        first = report.failures[0]
        return (
            f"Failure in {first.name}: {first.error[:200]}. Likely a missing error branch, "
            "a field mismatch, or an unimplemented endpoint. Fix that endpoint and re-run."
        )
    return "No failures recorded; keep the current implementation."


def patch_code(code: CodeBundle, report: TestReport, reflection: str) -> CodeBundle:
    """Patch the bundle for failing tests.

    The current contents must be in the prompt. Without them the model has
    nothing to patch and regenerates the same bundle every attempt, so the fix
    loop spins without progress. Only the files are re-emitted; the runner
    merges them over the existing bundle.
    """
    current = "\n\n".join(
        f"### {name}\n```python\n{body[:5000]}\n```" for name, body in code.files.items()
    )
    failures = "; ".join(f"{f.name}: {f.error[:200]}" for f in report.failures[:5])
    try:
        text, _meta = require(
            f"Reflection: {reflection}\n\nFailing tests:\n{failures}\n\n"
            f"Current code:\n{current}\n\n"
            "Reply with ONLY the file blocks you changed, complete and runnable. "
            "Change only what fixes the failures. No prose. Remember: the tables must be "
            "created at import time with Base.metadata.create_all(bind=engine), and every "
            "symbol you reference must be imported.",
            SOP.format(
                file_plan="\n- " + "\n- ".join(sorted(code.files)),
                entrypoint=code.entry_module or "",
            ),
            role="developer",
        )
    except GenerationError:
        return code
    if not text or "def " not in text:
        return code
    patched = parse_file_blocks(text)
    merged = dict(code.files)
    merged.update({k: v for k, v in patched.items() if v.strip()})
    if merged == code.files:
        # Nothing changed: the caller stops rather than retrying identical input.
        return code
    found = find_app_object(merged)
    module, attr = found if found else (code.entry_module, code.entry_attr)
    return CodeBundle(files=merged, entry_module=module, entry_attr=attr or "app")
