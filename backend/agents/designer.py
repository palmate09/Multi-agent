"""Designer: requirement -> contract.

Two contracts, chosen by :mod:`agents.kind`:

``api``      an OpenAPI 3.1 document. The file plan rides along in a second
             fenced block so the Developer emits exactly the modules intended.
``program``  a plain-language contract for a self-contained piece of software —
             a game, a simulation, a library. There is no OpenAPI document,
             because inventing ``POST /game/roll`` for a request to build a
             ludo game answers a question nobody asked. The contract instead
             names the modules, the entrypoint, the public interface and the
             behaviours that must hold.

Either way the layout follows the requirement's complexity; nothing is fixed.
"""

from __future__ import annotations

import json
import re

from agents.coverage import domain_terms
from agents.introspect import spec_paths
from agents.kind import detect_kind, kind_hint
from agents.llm import GenerationError, require
from agents.reasoner import plan_block
from schemas.messages import ApiSpec, Plan, UserStories

SMALL_APP_ENDPOINTS = 4

_API_SOP = (
    "You are an API Designer. Produce an OpenAPI 3.1 document for the requested "
    "service. Reply with EXACTLY two fenced blocks and no prose:\n"
    "1) a ```yaml block containing only the OpenAPI document\n"
    '2) a ```json block with exactly two keys: "entrypoint" (the python module '
    'filename that will hold the FastAPI app) and "files" (array of every .py '
    "filename you want created, including the entrypoint)"
)

_PROGRAM_SOP = (
    "You are designing a self-contained Python program: a game, a simulation, "
    "an algorithm or a library. It is NOT a web service, so there is NO OpenAPI "
    "document and NO HTTP layer.\n"
    "Reply with EXACTLY one ```json block and no prose, containing exactly "
    "these keys:\n"
    '  "summary": one sentence describing what the program does\n'
    '  "entrypoint": the .py filename holding the top-level entry point\n'
    '  "files": every .py filename to create, including the entrypoint\n'
    '  "public_api": the public interface the implementation must expose, as '
    'dotted names, e.g. ["LudoGame", "LudoGame.roll_dice", "LudoGame.move_token"]\n'
    '  "behaviours": concrete, testable statements of required behaviour, e.g. '
    '["a new game has exactly 3 players in turn order", "roll_dice returns an '
    'integer between 1 and 6 inclusive"]\n'
    "Split into several modules only when the program genuinely needs it; a "
    "single well-organised module is preferred over artificial fragmentation."
)

_FENCE = re.compile(r"```[^\n`]*\n(.*?)(?:\n```|\Z)", re.S)
_JSON_BLOCK = re.compile(r"```json\s*(\{.*?\})\s*```", re.S)


# ------------------------------------------------------------------- api ----
def _valid(yaml_text: str) -> tuple[bool, str]:
    """Domain-agnostic OpenAPI validation.

    Checks *shape*, never a particular path. The original version rejected any
    document without ``/tasks``, which made every non-task-manager requirement
    fall through to a hardcoded task-manager template.
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
        pass
    except Exception as e:
        return False, f"spec: {e}"
    return True, ""


def _yaml_block(text: str) -> str:
    for block in _FENCE.findall(text):
        candidate = block.strip()
        if candidate and "openapi:" in candidate:
            return candidate
    return text.strip()


def _plan_files(entrypoint: str, endpoints: int) -> list[str]:
    if endpoints <= SMALL_APP_ENDPOINTS:
        return [entrypoint]
    return [entrypoint, "models.py", "schemas_pyd.py"]


def _parse_api_plan(text: str, endpoints: list[str]) -> tuple[str, list[str]]:
    match = _JSON_BLOCK.search(text)
    if match:
        try:
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


def _design_api(stories: UserStories, requirement: str, plan: Plan | None = None) -> ApiSpec:
    listing = "\n".join(f"- {s.id}: {s.title} ({'; '.join(s.acceptance)})" for s in stories.stories)
    prompt = (
        f"Requirement:\n{requirement}\n\nStories:\n{listing}\n"
        if requirement
        else f"Stories:\n{listing}\n"
    ) + plan_block(plan)
    errors: list[str] = []
    for _ in range(3):
        text, _meta = require(
            f"{prompt}Output the yaml and json blocks.", _API_SOP, role="designer"
        )
        candidate = _yaml_block(text)
        if candidate and "openapi" in candidate:
            ok, why = _valid(candidate)
            if ok:
                pairs, _paths = spec_paths(candidate)
                endpoints = sorted({p for p, _ in pairs})
                entrypoint, files = _parse_api_plan(text, endpoints)
                return ApiSpec(
                    openapi_yaml=candidate,
                    endpoints=endpoints,
                    entrypoint=entrypoint,
                    files=files,
                    domain_terms=domain_terms(requirement),
                    kind="api",
                )
            errors.append(why)
    raise GenerationError(
        "Designer could not produce a valid OpenAPI document in 3 attempts: " + "; ".join(errors)
    )


# --------------------------------------------------------------- program ----
def _valid_program(data: dict) -> tuple[bool, str]:
    if not isinstance(data, dict):
        return False, "contract is not a JSON object"
    entry = str(data.get("entrypoint") or "").strip()
    if not entry:
        return False, "contract has no 'entrypoint'"
    if not entry.endswith(".py"):
        return False, f"entrypoint must be a .py filename, got {entry!r}"
    files = data.get("files")
    if not isinstance(files, list) or not files:
        return False, "contract has no 'files'"
    if entry not in [str(f) for f in files]:
        return False, f"entrypoint {entry!r} is not listed in 'files'"
    api = data.get("public_api")
    if not isinstance(api, list) or not api:
        return False, "contract has no 'public_api'; the tests have nothing to import"
    behaviours = data.get("behaviours")
    if not isinstance(behaviours, list) or len(behaviours) < 2:
        return False, "contract needs at least two 'behaviours' to test"
    return True, ""


def _design_program(stories: UserStories, requirement: str, plan: Plan | None = None) -> ApiSpec:
    listing = "\n".join(f"- {s.title}: {'; '.join(s.acceptance)}" for s in stories.stories)
    prompt = (
        f"Requirement:\n{requirement}\n\nExpected behaviours:\n{listing}\n"
        if requirement
        else f"Expected behaviours:\n{listing}\n"
    ) + plan_block(plan)
    errors: list[str] = []
    for _ in range(3):
        text, _meta = require(
            f"{prompt}Output the json contract block.", _PROGRAM_SOP, role="designer"
        )
        match = _JSON_BLOCK.search(text) or _FENCE.search(text)
        if not match:
            errors.append("no fenced json block in reply")
            continue
        raw = match.group(1).strip()
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            errors.append(f"json: {e}")
            continue
        ok, why = _valid_program(data)
        if ok:
            entry = str(data["entrypoint"]).lstrip("./")
            files = [str(f).lstrip("./") for f in data["files"]]
            files = [f if f.endswith(".py") else f"{f}.py" for f in files]
            if entry not in files:
                files.insert(0, entry)
            return ApiSpec(
                openapi_yaml="",
                endpoints=[],
                entrypoint=entry,
                files=files,
                domain_terms=domain_terms(requirement),
                kind="program",
                contract_text=json.dumps(data, indent=2),
                public_api=[str(s) for s in data["public_api"]],
            )
        errors.append(why)
    raise GenerationError(
        "Designer could not produce a usable program contract in 3 attempts: " + "; ".join(errors)
    )


def stories_to_spec(
    stories: UserStories, requirement: str = "", plan: Plan | None = None
) -> ApiSpec:
    """Design the contract for this requirement.

    Raises :class:`agents.llm.GenerationError` rather than falling back to a
    built-in answer, which is what once let a library request be reported as a
    successfully generated task manager.

    ``plan`` is the Reasoner's read on the requirement. It is passed through as
    context only — the contract is still graded against the requirement, not
    against the plan.
    """
    kind = detect_kind(requirement)
    spec = (
        _design_api(stories, requirement, plan)
        if kind == "api"
        else _design_program(stories, requirement, plan)
    )
    # One line the workflow logs, so a misclassification is visible immediately.
    spec.contract_text = spec.contract_text or ""
    spec.kind = kind
    return spec


def describe(spec: ApiSpec, requirement: str) -> str:
    """Human-readable one-liner for the run log."""
    if spec.kind == "program":
        return (
            f"{kind_hint(requirement)}; entrypoint={spec.entrypoint}; "
            f"public_api={spec.public_api[:4]}"
        )
    return (
        f"{kind_hint(requirement)}; endpoints={len(spec.endpoints)}; entrypoint={spec.entrypoint}"
    )
