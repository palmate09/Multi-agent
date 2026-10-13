"""Frontend generation: spec + stories -> a static, no-build web UI.

Three entry points, in pipeline order:

``design_ui``   the UI Designer. Returns the page/component contract, which the
                Developer implements literally. Its output is validated against
                the *generated API's* OpenAPI document, so the UI can only bind
                to endpoints that actually exist.
``build_ui``    the UI Developer. Emits ``### filename`` blocks of plain
                HTML/JS/CSS. No build step, no npm, no bundler.
``gate``        pure static checks. No LLM, milliseconds. The verdict is
                advisory: a bad UI is reported, never fatal.

Why relative URLs, enforced in :func:`gate` and not merely in the prompt: the
UI is served at ``/preview/<run_id>/``, so ``fetch('/books')`` resolves against
the *platform's* host and hits this application's own API instead of the
sandboxed one. The single most likely way for a generated UI to look broken
while every check passes.
"""

from __future__ import annotations

import json
import re

from agents.blocks import parse_file_blocks
from agents.introspect import normalise_path, spec_paths
from agents.llm import GenerationError, require
from schemas.messages import ApiSpec, CodeBundle, UiCheck, UiReport, UiSpec, UserStories

# The Designer is told to reply with one JSON block; the gate is what makes
# that binding instruction true.
_JSON_BLOCK = re.compile(r"```json\s*(\{.*?\})\s*```", re.S)

# Any of these means the browser would try to reach the internet, which the
# preview sandbox has no route to. Matched on the raw file text.
_EXTERNAL = re.compile(
    r"(?:src|href)\s*=\s*[\"']\s*(?://|//)|unpkg\.com|cdnjs\.cloudflare|"
    r"cdn\.jsdelivr|googleapis\.com|fonts\.g",
    re.I,
)

# ``fetch('/books')`` / axios("/books") / url = '/books'. A root-anchored path
# is the one that silently escapes the /preview/<run_id>/ mount.
_ROOTED_FETCH = re.compile(
    r"""(?:fetch|axios\.(?:get|post|put|patch|delete)|open)\s*\(\s*[`'"]/(?!/)""",
    re.I,
)

# innerHTML fed from data: a template literal (`${...}`), a concatenation, or
# an accumulating `+=`. A static markup string is fine and stays allowed.
_INNERHTML_ASSIGN = re.compile(r"innerHTML\s*(\+?=)\s*([^;\n]+)")
_INNERHTML_UNSAFE = re.compile(r"\$\{|\+\s*[A-Za-z_$]|\+\s*'|\+\s*\"")

# Plan files the UI must always have, whatever the model decided.
_REQUIRED_FILES = ("index.html",)

# Endpoints a page never renders. Excluded from the "every endpoint is bound"
# check so a correct UI does not fail for omitting the health probe.
_NON_UI_PATHS = frozenset({"/health", "/openapi.json", "/docs", "/redoc", "/"})


# ------------------------------------------------------------------ design ----
_UI_DESIGNER_SOP = (
    "You are a UI Designer. Plan the browser UI for the REST API below.\n"
    "The UI is PLAIN STATIC FILES: no React, no build step, no npm, no bundler. "
    "It is served from a sub-path, so every API call must be RELATIVE "
    "(`fetch('books')`, never `fetch('/books')`).\n"
    "Reply with EXACTLY one ```json block and no prose, containing exactly "
    "these keys:\n"
    '  "pages": the pages of the UI, e.g. ["/", "/books"]\n'
    '  "components": the reusable pieces, e.g. ["book-table", "book-form"]\n'
    '  "data_bindings": one entry per endpoint the UI calls, written as '
    '"<METHOD> <path> -> <what it renders>", e.g. "GET /books -> table of books"\n'
    '  "files": the files to create; must include "index.html", plus typically '
    '"app.js" and "styles.css"\n'
    "Only bind to endpoints that exist in the API below. Every endpoint you "
    "bind to must appear verbatim in this list:\n{endpoints}"
)


def _valid_ui_spec(data: object, declared: set[tuple[str, str]]) -> tuple[bool, str]:
    """Shape plus substance: a contract that binds to nothing is not a contract."""
    if not isinstance(data, dict):
        return False, "contract is not a JSON object"
    files = data.get("files")
    if not isinstance(files, list) or not files:
        return False, "contract has no 'files'"
    names = [str(f).strip().lstrip("./") for f in files if str(f).strip()]
    if not any(n == "index.html" for n in names):
        return False, f"files must include index.html, got {names}"
    pages = data.get("pages")
    if not isinstance(pages, list) or not pages:
        return False, "contract has no 'pages'; there is nothing to render"
    bindings = data.get("data_bindings")
    if not isinstance(bindings, list) or not bindings:
        return False, "contract has no 'data_bindings'; the UI would call nothing"
    # Must bind to something the API actually serves. Accepting a contract
    # whose only binding is `GET /nope` would hand the Developer a page that
    # can only ever render an error.
    declared_paths = {normalise_path(p) for p, _ in declared}
    resolved = [_resolve_binding(str(b)) for b in bindings]
    if not any(r and r[1] in declared_paths for r in resolved):
        return False, (
            "no data_binding names an endpoint the API declares "
            f"(declared: {sorted(declared_paths)})"
        )
    return True, ""


def _resolve_binding(text: str) -> tuple[str, str] | None:
    """Parse ``"GET /books -> table"`` into ``("GET", "/books")``, or None."""
    m = re.match(r"\s*([A-Za-z]+)\s+(\S+)", text)
    if not m:
        return None
    method, path = m.group(1).upper(), m.group(2).split("->")[0].strip().rstrip("/")
    if not path.startswith("/"):
        return None
    return method, normalise_path(path)


def design_ui(spec: ApiSpec, stories: UserStories | None = None, attempts: int = 3) -> UiSpec:
    """Plan the UI for a generated API. Raises ``GenerationError`` on failure.

    Three attempts, same shape as ``designer._design_api``: a contract the model
    cannot express in three tries is a real failure, and there is no template to
    fall back to. Substituting a canned dashboard is exactly the failure mode
    this repo stopped tolerating.
    """
    declared, _paths = spec_paths(spec.openapi_yaml)
    if not declared:
        raise GenerationError(
            "no API endpoints to bind a UI to: the generated spec declares no paths"
        )
    listing = ""
    if stories and stories.stories:
        listing = "\nStories:\n" + "\n".join(
            f"- {s.title}: {'; '.join(s.acceptance)}" for s in stories.stories
        )
    endpoints = "\n".join(f"  {m} {p}" for p, m in sorted(declared))
    prompt = (
        f"OpenAPI spec:\n{spec.openapi_yaml[:4000]}\n{listing}\n"
        f"The API declares these endpoints:\n{endpoints}\n"
        "Plan the UI and output the json block."
    )
    errors: list[str] = []
    for _ in range(max(1, attempts)):
        text, _meta = require(prompt, _UI_DESIGNER_SOP.format(endpoints=endpoints), role="designer")
        match = _JSON_BLOCK.search(text)
        if not match:
            errors.append("no fenced json block in reply")
            continue
        try:
            data = json.loads(match.group(1))
        except json.JSONDecodeError as exc:
            errors.append(f"json: {exc}")
            continue
        ok, why = _valid_ui_spec(data, declared)
        if not ok:
            errors.append(why)
            continue
        return UiSpec(
            pages=[str(p) for p in data.get("pages", [])],
            components=[str(c) for c in data.get("components", [])],
            data_bindings=[str(b) for b in data.get("data_bindings", [])],
            files=[str(f).strip().lstrip("./") for f in data.get("files", [])],
            contract_text=json.dumps(data, indent=2),
        )
    raise GenerationError(
        "UI Designer could not produce a usable UI contract in "
        f"{max(1, attempts)} attempts: " + "; ".join(errors)
    )


# ----------------------------------------------------------------- develop ----
_UI_DEVELOPER_SOP = (
    "You are the UI Developer. Write the complete static web UI described "
    "below, as plain files that a browser can open with no build step.\n"
    "Hard constraints (violating any of them breaks the preview):\n"
    "- Create EXACTLY these files, and no others:\n{file_plan}\n"
    "- All API calls must be RELATIVE: `fetch('books')`. NEVER "
    "`fetch('/books')`, never `http://localhost:8000/...`. The UI is served "
    "from a sub-path and a root-anchored URL escapes it to a different server.\n"
    "- No external origins: no CDN, no Google Fonts, no remote <script> or "
    "<link>. The machine running the preview has no internet access.\n"
    "- No `innerHTML` built from data, no `eval`, no `new Function`. Create "
    "elements with createElement/textContent instead.\n"
    '- index.html must contain a mount element with id="root" or id="app", '
    'and `<script src="app.js" defer></script>`.\n'
    "- Render what these endpoints actually return:\n{bindings}\n"
    "- Plain ES2020 JavaScript, no imports, no modules, no bundler.\n"
    "Reply with file blocks in this exact format and no prose:\n"
    "### <filename>\n```\n<complete file>\n```"
)


def _file_plan(ui_spec: UiSpec) -> list[str]:
    names = [f for f in (ui_spec.files or []) if f]
    if not names:
        names = ["index.html", "app.js", "styles.css"]
    for required in _REQUIRED_FILES:
        if required not in names:
            names.insert(0, required)
    return names


def build_ui(
    spec: ApiSpec,
    stories: UserStories | None,
    ui_spec: UiSpec,
    entry_module: str = "",
    entry_attr: str = "",
) -> CodeBundle:
    """Implement the UI contract. Raises ``GenerationError`` on failure."""
    required = _file_plan(ui_spec)
    plan_text = "\n".join(f"- {n}" for n in required)
    _pairs, paths = spec_paths(spec.openapi_yaml)
    endpoint_lines = "\n".join(f"  {path}" for path in sorted(paths))
    binding_lines = "\n".join(f"  - {b}" for b in ui_spec.data_bindings) or "  - (none declared)"
    listing = ""
    if stories and stories.stories:
        listing = "\nUser stories:\n" + "\n".join(
            f"- {s.title}: {'; '.join(s.acceptance)}" for s in stories.stories
        )
    prompt = (
        f"API endpoints:\n{endpoint_lines}\n\n"
        f"UI contract:\n{json.dumps(ui_spec.model_dump(), indent=2)[:3000]}\n{listing}\n"
        f"Files to create (exactly these):\n{plan_text}\n"
        "Emit the file blocks."
    )
    text, _meta = require(
        prompt,
        _UI_DEVELOPER_SOP.format(file_plan=plan_text, bindings=binding_lines),
        role="developer",
    )
    files = _parse_ui_files(text)
    keep = set(required)
    trimmed = {name: body for name, body in files.items() if name in keep and body.strip()}
    for required_name in _REQUIRED_FILES:
        if not trimmed.get(required_name, "").strip():
            raise GenerationError(
                f"UI Developer omitted the required file {required_name!r}; got {sorted(files)}"
            )
    return CodeBundle(
        files=trimmed,
        entry_module=entry_module or "",
        entry_attr=entry_attr or "",
    )


def _parse_ui_files(text: str) -> dict[str, str]:
    """Parse the reply into files, rejecting the single-unnamed-block fallback.

    ``parse_file_blocks`` falls back to ``{"test_tasks.py": body}`` when a reply
    has no ``###`` headers at all. For the UI that is not a test file — it is a
    prose answer that would land in the preview directory as a stray ``.py``.
    """
    if not re.search(r"###\s+\S+", text):
        raise GenerationError(
            "UI Developer reply has no '### <filename>' headers; refusing to write "
            "a fallback file from a prose answer"
        )
    files = parse_file_blocks(text)
    return {name: body for name, body in files.items() if name.endswith((".html", ".js", ".css"))}


# -------------------------------------------------------------------- gate ----
def gate(spec: ApiSpec, ui_spec: UiSpec, files: dict[str, str]) -> UiReport:
    """Static checks over the emitted files. No LLM, never raises.

    Each check is a fact a human could verify by reading one file. They are
    cheap enough to always run, and they catch the failure modes that make a
    generated UI useless: a file that was never written, a URL that escapes the
    preview mount, a CDN the sandbox cannot reach, an XSS sink.
    """
    checks: list[UiCheck] = []
    planned = _file_plan(ui_spec)
    declared, _paths = spec_paths(spec.openapi_yaml)

    # 1. planned files present, non-empty, index.html exists
    missing = [n for n in planned if not (files.get(n) or "").strip()]
    checks.append(
        UiCheck(
            name="missing_files",
            ok=not missing,
            detail=(
                f"planned but absent or empty: {missing}"
                if missing
                else f"all {len(planned)} planned file(s) written"
            ),
        )
    )

    js = "\n".join(body for name, body in files.items() if name.endswith(".js"))
    html = "\n".join(body for name, body in files.items() if name.endswith(".html"))

    # 2. no root-anchored fetch: it would escape /preview/<run_id>/
    rooted = _ROOTED_FETCH.findall(js) or _ROOTED_FETCH.findall(html)
    checks.append(
        UiCheck(
            name="rooted_url",
            ok=not rooted,
            detail=(
                "root-anchored API URL(s): a root-anchored path escapes the "
                "/preview/<run_id>/ mount and hits the platform API instead"
                if rooted
                else "all API calls are relative"
            ),
        )
    )

    # 3. no external origins
    externals = _EXTERNAL.findall(html) + _EXTERNAL.findall(js)
    checks.append(
        UiCheck(
            name="external_asset",
            ok=not externals,
            detail=(
                f"external origin reference(s) {sorted(set(externals))}: the preview "
                "sandbox has no network"
                if externals
                else "no external origins"
            ),
        )
    )

    # 4. no innerHTML interpolation
    xss = [
        line.strip()
        for line in js.splitlines()
        if any(
            op == "+=" or _INNERHTML_UNSAFE.search(value)
            for op, value in _INNERHTML_ASSIGN.findall(line)
        )
    ]
    checks.append(
        UiCheck(
            name="xss_innerhtml",
            ok=not xss,
            detail=(
                f"innerHTML assigned from data: {xss[0][:120]}"
                if xss
                else "no interpolated innerHTML"
            ),
        )
    )

    # 5. every binding maps to an endpoint the spec declares
    declared_paths = {normalise_path(p) for p, _ in declared}
    undeclared = []
    for binding in ui_spec.data_bindings:
        resolved = _resolve_binding(binding)
        if resolved is None or resolved[1] not in declared_paths:
            undeclared.append(binding)
    checks.append(
        UiCheck(
            name="undeclared_endpoint",
            ok=not undeclared,
            detail=(
                f"bound to endpoints the spec does not declare: {undeclared}"
                if undeclared
                else f"all {len(ui_spec.data_bindings)} binding(s) declared by the spec"
            ),
        )
    )

    # 6. every real spec path has at least one binding. Reported, never fatal:
    #    a UI may legitimately cover only the main resource. /health and the
    #    docs paths are excluded — they are infrastructure a page never renders,
    #    so requiring a binding for them would fail every correct UI.
    bound = {r[1] for r in (_resolve_binding(b) for b in ui_spec.data_bindings) if r}
    unbound = sorted(declared_paths - bound - _NON_UI_PATHS)
    checks.append(
        UiCheck(
            name="unbound_endpoint",
            ok=not unbound,
            detail=(
                f"spec endpoint(s) the UI does not bind: {unbound}"
                if unbound
                else "every UI-facing spec endpoint is bound"
            ),
        )
    )

    # 7. mount element + script tag
    has_mount = bool(re.search(r'id\s*=\s*["\'](?:root|app|main)["\']', html)) or bool(
        re.search(r'id\s*=\s*["\'](?:root|app|main)["\']', js)
    )
    has_script = bool(re.search(r"<script[^>]+src\s*=\s*[\"']?app\.js", html))
    checks.append(
        UiCheck(
            name="no_mount",
            ok=has_mount and has_script,
            detail=(
                f"mount element {'found' if has_mount else 'missing'}, "
                f'<script src="app.js"> {"found" if has_script else "missing"}'
            ),
        )
    )

    return UiReport(ok=all(c.ok for c in checks), checks=checks)


def is_ui_eligible(spec: ApiSpec | None) -> bool:
    """Only an HTTP service gets a UI; a program has nothing to fetch from."""
    return bool(spec and getattr(spec, "kind", "api") == "api" and spec.openapi_yaml)
