"""Designer: stories -> OpenAPI YAML + a file plan for the Developer.

The plan is the anti-template mechanism: the Designer chooses the module layout
from the requirement's complexity, and the Developer is told exactly which files
to emit. Nothing here knows what a task manager is.
"""

from __future__ import annotations

import re

from agents.coverage import domain_terms
from agents.introspect import spec_paths
from agents.llm import require
from schemas.messages import ApiSpec, UserStories

SOP = (
    "You are an API Designer. Produce an OpenAPI 3.1 document for the requested "
    "API. Reply with EXACTLY two fenced blocks and no prose:\n"
    "1) a ```yaml block containing only the OpenAPI document\n"
    "2) a ```json block with exactly two keys: "
    '"entrypoint" (the python module filename that will hold the FastAPI app) '
    'and "files" (array of every .py filename you want created, including the entrypoint)'
)

_PLAN_RE = re.compile(r"```json\s*(\{.*?\})\s*```", re.S)
_ANY_FENCE = re.compile(r"```[^\n`]*\n(.*?)(?:\n```|\Z)", re.S)

# Below this many endpoints a single module is cleaner than a package.
SMALL_APP_ENDPOINTS = 4


def _valid(yaml_text: str) -> tuple[bool, str]:
    """Domain-agnostic OpenAPI validation.

    Deliberately checks *shape*, never a particular path. The previous version
    rejected any document without ``/tasks``, which made every non-task-manager
    requirement fall through to the hardcoded template.
    """
    try:
        import yaml

        doc = yaml.safe_load(yaml_text)
    except Exception as e:
        return False, f"yaml: {e}"
    if not isinstance(doc, dict):
        return False, "document is not a mapping"
    if "openapi" not in doc:
        return False, "missing 'openapi' version key"
    paths = doc.get("paths")
    if not isinstance(paths, dict) or not paths:
        return False, "missing or empty 'paths'"
    for path in paths:
        if not isinstance(path, str) or not path.startswith("/"):
            return False, f"invalid path key: {path!r}"
    try:
        from openapi_spec_validator import validate

        validate(doc)
    except ImportError:
        # Validator not installed: shape checks above are what we have.
        pass
    except Exception as e:
        return False, f"spec: {e}"
    return True, ""


def _plan_files(entrypoint: str, endpoints: int) -> list[str]:
    """Suggest a layout sized to the app.

    Only used when the model omits the plan; the Developer follows whatever the
    model chooses, so this is a fallback hint rather than a mandate.
    """
    if endpoints <= SMALL_APP_ENDPOINTS:
        return [entrypoint]
    return [entrypoint, "models.py", "schemas_pyd.py"]


def _parse_plan(text: str, endpoints: list[str]) -> tuple[str, list[str]]:
    """Extract ``(entrypoint, files)`` from the designer's JSON block."""
    match = _PLAN_RE.search(text)
    if match:
        try:
            import json

            data = json.loads(match.group(1))
            entry = str(data.get("entrypoint") or "").strip().lstrip("./")
            files = [
                str(f).strip().lstrip("./") for f in (data.get("files") or []) if str(f).strip()
            ]
            if entry and not entry.endswith(".py"):
                entry += ".py"
            files = [f if f.endswith(".py") else f"{f}.py" for f in files]
            if entry:
                if not files:
                    files = [entry]
                if entry not in files:
                    files.insert(0, entry)
                return entry, files
        except Exception:
            pass
    entry = "main.py"
    return entry, _plan_files(entry, len(endpoints))


def _yaml_block(text: str) -> str:
    """Return the yaml fenced block, or the whole text when it is bare YAML."""
    for block in _ANY_FENCE.findall(text):
        candidate = block.strip()
        if candidate and ("openapi:" in candidate or candidate.lstrip().startswith("openapi")):
            return candidate
    return text.strip()


def stories_to_spec(stories: UserStories, requirement: str = "") -> ApiSpec:
    """Generate a spec, retrying up to 3 times, then raising.

    Raises :class:`agents.llm.GenerationError` when the model never produces a
    usable document. It no longer falls back to a built-in task-manager spec,
    which is what allowed a library request to be reported as a task manager.
    """
    listing = "\n".join(f"- {s.id}: {s.title} ({'; '.join(s.acceptance)})" for s in stories.stories)
    prompt = (
        f"Requirement:\n{requirement}\n\nStories:\n{listing}\n"
        if requirement
        else f"Stories:\n{listing}\n"
    )
    errors: list[str] = []
    for _ in range(3):
        text, _meta = require(f"{prompt}Output the yaml and json blocks.", SOP, role="designer")
        candidate = _yaml_block(text)
        if candidate and "openapi" in candidate:
            ok, why = _valid(candidate)
            if ok:
                pairs, _paths = spec_paths(candidate)
                endpoints = sorted({p for p, _ in pairs}) or sorted(
                    {p for p, _ in _pair_fallback(candidate)}
                )
                entrypoint, files = _parse_plan(text, endpoints)
                return ApiSpec(
                    openapi_yaml=candidate,
                    endpoints=endpoints,
                    models=[],
                    entrypoint=entrypoint,
                    files=files,
                    domain_terms=domain_terms(requirement),
                )
            errors.append(why)
    from agents.llm import GenerationError

    raise GenerationError(
        "Designer could not produce a valid OpenAPI document in 3 attempts: " + "; ".join(errors)
    )


def _pair_fallback(yaml_text: str) -> set[tuple[str, str]]:
    try:
        import yaml

        doc = yaml.safe_load(yaml_text) or {}
    except Exception:
        return set()
    out: set[tuple[str, str]] = set()
    for path, ops in (doc.get("paths") or {}).items():
        if isinstance(ops, dict):
            for method in ops:
                out.add((path, str(method).upper()))
    return out
