"""Designer: requirement -> contract.

Three contracts, chosen by :mod:`agents.kind`:

``fullstack`` a web application a person uses: an OpenAPI document for the
              backend, plus a page-serving app that hosts its own frontend.
              The endpoint set is chosen so an interface can bind to it, and
              the paths live under ``/api`` so they never collide with the
              app's own pages and assets.
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


def _repair_openapi(text: str) -> str:
    """Fix the shape slips a model keeps making in an OpenAPI document.

    ``content: {application/json: null}`` and ``description: null`` are the
    two that come up constantly, and each one makes the whole document fail
    strict validation. A live run died on a null where a body was never meant
    to be: three identical documents, three identical rejections, no app.
    Repairing the slip is not inventing a contract -- the paths, methods and
    status codes the model wrote are left exactly as they are.
    """
    try:
        import yaml

        doc = yaml.safe_load(text)
    except Exception:
        return text
    if not isinstance(doc, dict):
        return text
    if not _repair_responses(doc):
        return text
    try:
        return yaml.safe_dump(doc, sort_keys=False, allow_unicode=True)
    except Exception:
        return text


def _repair_responses(doc: dict) -> bool:
    """Drop null media types and fill in null descriptions. True if changed."""
    changed = False
    paths = doc.get("paths")
    if not isinstance(paths, dict):
        return False
    for path_item in paths.values():
        if not isinstance(path_item, dict):
            continue
        for method in ("get", "put", "post", "delete", "patch", "head", "options"):
            operation = path_item.get(method)
            if not isinstance(operation, dict):
                continue
            responses = operation.get("responses")
            if not isinstance(responses, dict):
                continue
            for response in responses.values():
                if not isinstance(response, dict):
                    continue
                if response.get("description") is None:
                    response["description"] = "Successful response"
                    changed = True
                content = response.get("content")
                if not isinstance(content, dict):
                    continue
                for media, media_object in list(content.items()):
                    if media_object is None:
                        del content[media]
                        changed = True
                        continue
                    if not isinstance(media_object, dict):
                        continue
                    if media_object.get("schema") is None:
                        media_object.pop("schema", None)
                        changed = True
                    if not media_object:
                        del content[media]
                        changed = True
                if not content:
                    response.pop("content", None)
                    changed = True
    return changed


def _compact_error(why: str) -> str:
    """A one-line reason for the model to act on.

    The validator's complaint runs to kilobytes of schema paths. Sending that
    back verbatim wastes the context and hides the one sentence that matters.
    """
    lines = [line.strip() for line in why.splitlines() if line.strip()]
    if not lines:
        return "the document was rejected"
    head = lines[0]
    where = next((line for line in lines if line.startswith("On instance")), "")
    return f"{head} (at {where[len('On instance') :].strip()})" if where else head


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
            # A plan may also name frontend files ("static/index.html"): keep
            # anything that already carries an extension, and give bare module
            # names the .py suffix.
            files = [f if "." in f.rsplit("/", 1)[-1] else f"{f}.py" for f in files]
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


def _pm_analysis(stories: UserStories) -> str:
    """The PM's depth pass, when the model supplied one."""
    analysis = getattr(stories, "analysis", "").strip()
    return (
        f"\nPM analysis (actors, entities, workflows, failure cases):\n{analysis}\n"
        if analysis
        else ""
    )


def _design_api(
    stories: UserStories,
    requirement: str,
    plan: Plan | None = None,
    want_ui: bool = False,
    kind: str = "api",
) -> ApiSpec:
    listing = "\n".join(f"- {s.id}: {s.title} ({'; '.join(s.acceptance)})" for s in stories.stories)
    prompt = (
        f"Requirement:\n{requirement}\n{_pm_analysis(stories)}\nStories:\n{listing}\n"
        if requirement
        else f"{_pm_analysis(stories)}\nStories:\n{listing}\n"
    ) + plan_block(plan)
    if kind == "fullstack":
        prompt += (
            "\nThis is a FULL-STACK web application: a person will open it in a browser"
            " and use it. The OpenAPI document below is the BACKEND of that application,"
            " and the application serves its own pages and assets.\n"
            "- Prefix every path with /api so the backend never collides with the"
            " application's own pages, assets or routes.\n"
            "- Expose the endpoints the interface needs: list, create, read, update,"
            " delete and every status-changing action the stories mention.\n"
            "- Declare request and response schemas for every body, so the interface"
            " can render real fields instead of guessing them.\n"
            "- Use plain JSON bodies and integer ids.\n"
        )
    elif want_ui:
        prompt += (
            "\nThe run asked for a web UI: the HTTP contract must expose the endpoints a"
            " static page needs (list, create, update, delete the main resource, plus"
            " any status-changing action), so a UI can bind to them.\n"
        )
    errors: list[str] = []
    correction = ""
    for _ in range(3):
        text, _meta = require(
            f"{prompt}{correction}Output the yaml and json blocks.", _API_SOP, role="designer"
        )
        candidate = _yaml_block(text)
        if candidate and "openapi" in candidate:
            candidate = _repair_openapi(candidate)
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
                    kind=kind,
                )
            errors.append(why)
            # Tell the model what was wrong. A blind re-roll of the same
            # prompt produced the same rejected document three times, and the
            # run died on a document nobody had explained.
            correction = (
                "\nYour previous OpenAPI document was rejected: "
                f"{_compact_error(why)}\n"
                "Rewrite the complete document with that fixed. Do not explain it.\n"
            )
            continue
        errors.append("no yaml block in reply")
        correction = (
            "\nYour previous reply did not contain a ```yaml block with the OpenAPI "
            "document. Reply with the two fenced blocks and nothing else.\n"
        )
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
        f"Requirement:\n{requirement}\n{_pm_analysis(stories)}\nExpected behaviours:\n{listing}\n"
        if requirement
        else f"{_pm_analysis(stories)}\nExpected behaviours:\n{listing}\n"
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


_FOLLOWUP_SOP = (
    "You are an API Designer. This is a CHANGE to an HTTP contract that already "
    "exists. The OpenAPI document below is the current contract. Apply the "
    "change request to it and reply with the COMPLETE updated document.\n"
    "Rules:\n"
    "- Keep every existing path, method and status code the change does not "
    "remove. A follow-up that quietly drops a capability is a bug.\n"
    "- Add the new endpoints the request implies, with the request and response "
    "schemas the interface will render.\n"
    "- Keep the paths under the same prefix the document already uses.\n"
    "Reply with EXACTLY two fenced blocks and no prose:\n"
    "1) a ```yaml block containing only the complete OpenAPI document\n"
    '2) a ```json block with exactly two keys: "entrypoint" (the python module '
    'filename that will hold the FastAPI app) and "files" (array of every .py '
    "filename you want created, including the entrypoint)"
)


def update_spec(
    previous: ApiSpec,
    request: str,
    stories: UserStories,
    requirement: str = "",
) -> ApiSpec:
    """The updated contract for an app that already has one.

    The whole document comes back, not a patch: a model asked for a diff
    reliably returns a fragment, and a fragment cannot be validated, tested or
    served.
    """
    listing = "\n".join(f"- {s.id}: {s.title} ({'; '.join(s.acceptance)})" for s in stories.stories)
    prompt = (
        f"Original requirement:\n{requirement}\n\n"
        f"Current stories:\n{listing}\n\n"
        f"Change request:\n{request}\n\n"
        f"Current OpenAPI contract:\n{previous.openapi_yaml[:16000]}\n"
    )
    errors: list[str] = []
    correction = ""
    for _ in range(3):
        text, _meta = require(
            f"{prompt}{correction}Output the yaml and json blocks.",
            _FOLLOWUP_SOP,
            role="designer",
        )
        candidate = _yaml_block(text)
        if not candidate or "openapi" not in candidate:
            errors.append("no yaml block in reply")
            correction = (
                "\nYour previous reply did not contain a ```yaml block with the complete "
                "OpenAPI document. Reply with the two blocks and nothing else.\n"
            )
            continue
        candidate = _repair_openapi(candidate)
        ok, why = _valid(candidate)
        if not ok:
            errors.append(why)
            correction = (
                "\nYour previous OpenAPI document was rejected: "
                f"{_compact_error(why)}\nRewrite the complete document with that fixed.\n"
            )
            continue
        pairs, _paths = spec_paths(candidate)
        endpoints = sorted({p for p, _ in pairs})
        entrypoint, files = _parse_api_plan(text, endpoints)
        return ApiSpec(
            openapi_yaml=candidate,
            endpoints=endpoints,
            entrypoint=entrypoint,
            files=files,
            domain_terms=domain_terms(requirement),
            kind=previous.kind,
        )
    raise GenerationError(
        "Designer could not apply the change to the contract: " + "; ".join(errors)
    )


def stories_to_spec(
    stories: UserStories,
    requirement: str = "",
    plan: Plan | None = None,
    want_ui: bool = False,
) -> ApiSpec:
    """Design the contract for this requirement.

    Raises :class:`agents.llm.GenerationError` rather than falling back to a
    built-in answer, which is what once let a library request be reported as a
    successfully generated task manager.

    ``plan`` is the Reasoner's read on the requirement. It is passed through as
    context only — the contract is still graded against the requirement, not
    against the plan.

    ``want_ui`` shapes the contract when the run asked for a web app: the kind
    vote leans toward a full-stack application and the endpoint set leans
    toward what an interface can bind to.
    """
    kind = detect_kind(requirement, want_ui=want_ui)
    if kind == "program":
        spec = _design_program(stories, requirement, plan)
    else:
        spec = _design_api(stories, requirement, plan, want_ui=want_ui, kind=kind)
    # One line the workflow logs, so a misclassification is visible immediately.
    spec.contract_text = spec.contract_text or ""
    spec.kind = kind
    return spec


def describe(spec: ApiSpec, requirement: str, want_ui: bool = False) -> str:
    """Human-readable one-liner for the run log."""
    if spec.kind == "program":
        return (
            f"{kind_hint(requirement)}; entrypoint={spec.entrypoint}; "
            f"public_api={spec.public_api[:4]}"
        )
    return (
        f"{kind_hint(requirement, want_ui=want_ui)}; endpoints={len(spec.endpoints)}; "
        f"entrypoint={spec.entrypoint}"
    )
