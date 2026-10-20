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

# Plan files the UI must always have, whatever the model decided. A separate
# stylesheet is what makes the design-system checks meaningful; inline styling
# was how generated pages ended up as bare browser defaults.
_REQUIRED_FILES = ("index.html", "app.js", "styles.css")

# Endpoints a page never renders. Excluded from the "every endpoint is bound"
# check so a correct UI does not fail for omitting the health probe.
_NON_UI_PATHS = frozenset({"/health", "/openapi.json", "/docs", "/redoc", "/"})


# ------------------------------------------------------------------ design ----
_UI_DESIGNER_SOP = (
    "You are a UI Designer. Plan the browser UI for the REST API below.\n"
    "The UI is PLAIN STATIC FILES: no React, no build step, no npm, no bundler. "
    "It is served from a sub-path, so every API call must be RELATIVE "
    "(`fetch('books')`, never `fetch('/books')`).\n"
    "Design a polished product, not a wireframe: a compact header with the "
    "product name and one-line purpose, a primary content surface, a secondary "
    "panel for forms/details, one accent colour on a calm neutral palette, and "
    "generous whitespace. Plan loading, empty and error states for every async "
    "view. The layout must be responsive: comfortable max-width on desktop and "
    "a single column on phones. Name real components for cards, tables, forms, "
    "buttons, badges and alerts, and plan the complete user workflow the API "
    "supports rather than only a list view.\n"
    "Reply with EXACTLY one ```json block and no prose, containing exactly "
    "these keys:\n"
    '  "pages": the pages of the UI, e.g. ["/", "/books"]\n'
    '  "components": the reusable pieces, e.g. ["book-table", "book-form"]\n'
    '  "data_bindings": one entry per endpoint the UI calls, written as '
    '"<METHOD> <path> -> <what it renders>", e.g. "GET /books -> table of books"\n'
    '  "files": the files to create; must include "index.html", "app.js" and '
    '"styles.css"\n'
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
    missing_required = [n for n in _REQUIRED_FILES if n not in names]
    if missing_required:
        return False, f"files must include {missing_required}"
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


_HTTP_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"})


def _binding_pairs(text: str) -> list[tuple[str, str]]:
    """Every ``METHOD /path`` pair a binding mentions.

    A designer binding like ``GET / -> renders the home page and triggers
    GET /tasks`` describes two things: the page load itself and the fetch
    behind it. The first-match-only parser above is what the contract
    validator needs; gate checks must consider all pairs a binding names.
    """
    pairs: list[tuple[str, str]] = []
    for m in re.finditer(r"\s*([A-Za-z]+)\s+(\S+)", text):
        method = m.group(1).upper()
        path = m.group(2).split("->")[0].strip().rstrip("/")
        if method in _HTTP_METHODS and path.startswith("/"):
            pairs.append((method, normalise_path(path)))
    return pairs


def design_ui(
    spec: ApiSpec,
    stories: UserStories | None = None,
    attempts: int = 3,
    followup: str = "",
) -> UiSpec:
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
        if stories.analysis.strip():
            listing = f"\nPM analysis:\n{stories.analysis}\n" + listing
    endpoints = "\n".join(f"  {m} {p}" for p, m in sorted(declared))
    prompt = (
        f"OpenAPI spec:\n{spec.openapi_yaml[:4000]}\n{listing}\n"
        f"The API declares these endpoints:\n{endpoints}\n"
        "Plan the UI and output the json block."
    )
    if spec.kind == "fullstack":
        prompt += (
            "\nThis interface is the frontend of a full-stack application, and it is the "
            "landing page of the product: plan the screens a person actually uses (an "
            "overview, a collection view, a detail or form view, and the states in "
            "between), not a single dashboard of unrelated widgets. The pages are "
            "client-side views of the API below, so every one of them must be "
            "reachable from the data the API exposes.\n"
        )
    if followup.strip():
        prompt += (
            "\nThis is a CHANGE to an interface that already exists. The new pages and "
            "components must include the change, and everything the app already offers "
            "must still be reachable:\n"
            f"{followup.strip()}\n"
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
    "You are a senior product designer and frontend engineer. Build a polished, "
    "production-quality web UI for the API below using plain static files.\n"
    "Hard constraints (violating any of them breaks the preview):\n"
    "- Create EXACTLY these files, and no others:\n{file_plan}\n"
    "- index.html must have a lang, a charset, a descriptive title, "
    '`<meta name="viewport" content="width=device-width, initial-scale=1">`, '
    "a link to styles.css, and exactly one mount element "
    '`<div id="root"></div>`, and `<script src="app.js" defer></script>`.\n'
    "- THE INTERFACE MUST RENDER ON LOAD. app.js must find that mount element "
    "and render into it: use the id `root` in both files, and start from a "
    "`DOMContentLoaded` listener or a top-level call (the script is deferred, so "
    "the DOM is already parsed when it runs). A file of helper functions that "
    "nothing ever calls renders a BLANK PAGE: the preview shows an empty white "
    "frame, which is the most common way a generated interface looks broken.\n"
    "- All API calls must be RELATIVE: `fetch('books')`. NEVER "
    "`fetch('/books')`, never `http://localhost:8000/...`. The UI is served "
    "from a sub-path and a root-anchored URL escapes it to a different server.\n"
    "- No external origins: no CDN, no Google Fonts, no remote <script> or "
    "<link>. The machine running the preview has no internet access.\n"
    "- No `innerHTML` built from data, no `eval`, no `new Function`. Create "
    "elements with createElement/textContent instead. Never assign to "
    "`innerHTML` at all, not even from a template literal: a template with data "
    "inside it is still an injection.\n"
    "- Render what these endpoints actually return:\n{bindings}\n"
    "- Plain ES2020 JavaScript, no imports, no modules, no bundler.\n"
    "Visual quality bar — a bare wireframe is a failed deliverable:\n"
    "- Build a design system in styles.css: a :root block with custom properties "
    "for background, surface, text, muted text, accent, accent contrast, border, "
    "radius, shadow and spacing. Use those variables everywhere instead of raw "
    "hex values in component rules.\n"
    "- Use a modern system font stack (system-ui, -apple-system, Segoe UI, "
    "Roboto, sans-serif), 15-16px body text, a clear heading scale and a "
    "comfortable line length.\n"
    "- Compose the page with a centered max-width container, a compact header "
    "(product name plus one-line purpose), card/panel surfaces with 1px borders "
    "and soft shadows, and generous 16/24/32px spacing. Never leave raw "
    "browser-default buttons, inputs or tables unstyled.\n"
    "- Style all states: primary and secondary buttons with hover, active and "
    "disabled styles; inputs with labels and a visible focus ring; tables or "
    "cards for collections; status badges; and a styled error alert. Provide a "
    "loading indicator, an empty state with a helpful action, and an error "
    "state for every async fetch.\n"
    "- Add `:focus-visible` outlines and keep controls keyboard reachable. Use "
    "aria-live for status and error updates.\n"
    "- Be responsive: a `@media (max-width: 640px)` breakpoint, a single-column "
    "layout on phones, and touch targets at least 44px tall.\n"
    "- Aim for a finished look: roughly 150-350 lines of organised, commented "
    "CSS. A 20-line stylesheet or an unstyled page is not acceptable.\n"
    "Clean, modern code — this is what the generated files should look like:\n"
    "- One mount point and one id: `root`, in index.html and in app.js. Never "
    "leave a second container id that nothing renders into.\n"
    "- No dead code: every function you define is called by the page. Do not "
    "leave helper functions behind that nothing uses.\n"
    "- Modern JavaScript: `const`/`let` (never `var`), arrow functions, "
    "`async`/`await` for fetches, template literals, `classList` and `dataset` "
    "instead of hand-built class strings, `URLSearchParams` for query state, "
    "`Intl.DateTimeFormat` for dates, and `AbortController` when a fetch can be "
    "superseded.\n"
    "- No `alert()`, `prompt()`, `confirm()` or `document.write`: report a "
    "problem in the interface, not in a browser dialog.\n"
    "- Semantic HTML: `<main>`, `<header>`, `<section>`, `<form>`, real "
    "`<button>` and `<label>` elements, and `<template>` for repeated rows. "
    "index.html must wrap the app in a `<main>` element that holds the mount "
    'point: `<body><header>...</header><main id="root"></main></body>`.\n'
    "- Modern CSS: custom properties, `clamp()` for fluid type, "
    "`:focus-visible`, `prefers-reduced-motion` for animation, and "
    "`prefers-color-scheme` so the page follows the reader's theme.\n"
    "- Keep it readable: short functions with clear names, section comments in "
    "the CSS, and no JavaScript file over roughly 400 lines.\n"
    "Reply with file blocks in this exact format and no prose:\n"
    "### <filename>\n```\n<complete file>\n```"
)


def _file_plan(ui_spec: UiSpec) -> list[str]:
    names = [f for f in (ui_spec.files or []) if f]
    if not names:
        names = ["index.html", "app.js", "styles.css"]
    missing = [required for required in _REQUIRED_FILES if required not in names]
    return missing + names


# Extra rules for the interface of a full-stack application: the page is the
# application's own landing page, served by the same process that serves the
# API, so the URL conventions differ from a page bolted on beside a service.
_FULLSTACK_UI_RULES = (
    "\nThis interface is the frontend OF THE APPLICATION ITSELF: the server "
    "serves this page at its root path, and the same origin serves the API.\n"
    "- Call the API with RELATIVE urls built from the spec's exact paths, "
    "without the leading slash: a spec path `/api/items` is called as "
    "`fetch('api/items')`. Never `fetch('/api/items')` and never an absolute "
    "host: the app may be served from a sub-path, and a root-anchored url "
    "escapes it to a different server.\n"
    "- Use hash-based routing for client-side views (`location.hash`, e.g. "
    "`#/tasks`), so deep links work without a server-side rewrite rule.\n"
    "- The page is the product: render the main view on load, and let the "
    "person create, edit and delete through the interface without reloading "
    "the page.\n"
)


def build_ui(
    spec: ApiSpec,
    stories: UserStories | None,
    ui_spec: UiSpec,
    entry_module: str = "",
    entry_attr: str = "",
    feedback: str = "",
    followup: str = "",
) -> CodeBundle:
    """Implement the UI contract. Raises ``GenerationError`` on failure.

    ``feedback`` carries the gate's findings from a rejected first attempt, so
    the repair pass is targeted rather than another blind generation.
    """
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
        if stories.analysis.strip():
            listing = f"\nPM analysis:\n{stories.analysis}\n" + listing
    repair = ""
    if feedback.strip():
        repair = (
            "\nThe previous version of this UI failed these checks. Fix every one "
            "of them in the new files:\n" + feedback.strip() + "\n"
            "Change only what these findings require: the checks that already "
            "pass must keep passing.\n"
        )
    prompt = (
        f"API endpoints:\n{endpoint_lines}\n\n"
        f"UI contract:\n{json.dumps(ui_spec.model_dump(), indent=2)[:3000]}\n{listing}\n"
        f"Files to create (exactly these):\n{plan_text}\n{repair}"
        + (
            f"\nThis is a CHANGE to an interface that already exists. The new files must "
            f"show the change and keep everything the app already offers:\n{followup.strip()}\n"
            if followup.strip()
            else ""
        )
        + "Emit the file blocks."
    )
    text, _meta = require(
        prompt,
        (
            _UI_DEVELOPER_SOP + _FULLSTACK_UI_RULES
            if spec.kind == "fullstack"
            else _UI_DEVELOPER_SOP
        ).format(file_plan=plan_text, bindings=binding_lines),
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
    # A binding may point at the page itself ("GET / -> renders the home page
    # and triggers GET /tasks"). The page is served by the platform, so a
    # binding counts as declared when ANY method/path it mentions is either an
    # API path the spec declares or the URL of one of the UI's own pages.
    page_paths = {normalise_path(p) for p in ui_spec.pages}
    undeclared = []
    for binding in ui_spec.data_bindings:
        if not any(
            path in declared_paths or (method == "GET" and path in page_paths)
            for method, path in _binding_pairs(binding)
        ):
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

    css = "\n".join(body for name, body in files.items() if name.endswith(".css"))

    # 8. mobile viewport: without it every phone renders a zoomed-out desktop.
    viewport = bool(re.search(r"<meta[^>]+name\s*=\s*[\"']viewport[\"']", html, re.I))
    checks.append(
        UiCheck(
            name="viewport_meta",
            ok=viewport,
            detail=(
                "viewport meta found"
                if viewport
                else 'index.html is missing <meta name="viewport" content="width=device-width, initial-scale=1">'
            ),
        )
    )

    # 9. a real page title (not the URL as the tab label)
    title = re.search(r"<title>\s*(.*?)\s*</title>", html, re.I | re.S)
    titled = bool(title and title.group(1).strip())
    checks.append(
        UiCheck(
            name="html_title",
            ok=titled,
            detail=(
                f"title: {title.group(1).strip()[:80]}"
                if titled and title
                else "index.html has no non-empty <title>"
            ),
        )
    )

    # 10. the stylesheet is actually linked
    styles_linked = bool(re.search(r"<link[^>]+rel\s*=\s*[\"']stylesheet[\"'][^>]*>", html, re.I))
    checks.append(
        UiCheck(
            name="stylesheet_link",
            ok=styles_linked,
            detail=(
                "stylesheet linked" if styles_linked else "index.html does not link a stylesheet"
            ),
        )
    )

    # 11. design tokens, not one-off hex values scattered through the CSS
    token_count = len(re.findall(r"--[a-zA-Z0-9-]+\s*:", css))
    has_tokens = bool(re.search(r":root\s*\{", css)) and token_count >= 4
    checks.append(
        UiCheck(
            name="design_tokens",
            ok=has_tokens,
            detail=(
                f"{token_count} custom properties under :root"
                if has_tokens
                else "styles.css needs a :root block with at least four custom properties"
            ),
        )
    )

    # 12. a responsive breakpoint
    responsive = bool(re.search(r"@media\b", css))
    checks.append(
        UiCheck(
            name="responsive_css",
            ok=responsive,
            detail=(
                "responsive breakpoint found"
                if responsive
                else "styles.css has no @media breakpoint"
            ),
        )
    )

    # 13. keyboard focus is visible
    focus = bool(re.search(r":focus(?:-visible)?\b", css))
    checks.append(
        UiCheck(
            name="focus_states",
            ok=focus,
            detail=(
                "focus styles found" if focus else "styles.css has no :focus or :focus-visible rule"
            ),
        )
    )

    # 14. the interface actually renders. The single most common way a
    # generated UI looks broken: the page loads, the CSS applies, the title is
    # right -- and the body is empty, because the script defines a pile of
    # render functions and never calls one. A real run produced exactly that,
    # and the preview showed a blank white frame with no error anywhere.
    mount_ids = re.findall(r'id\s*=\s*["\']([A-Za-z][\w-]*)["\']', html)
    mount_id = next((m for m in mount_ids if m in {"root", "app", "main"}), None)
    mounts = bool(mount_id) and bool(
        re.search(
            rf"getElementById\(\s*['\"]{mount_id}['\"]\s*\)|querySelector\(\s*['\"]#{mount_id}['\"]\s*\)",
            js,
        )
    )
    starts = bool(
        re.search(
            r"DOMContentLoaded|window\.onload|addEventListener\(\s*['\"]load['\"]|document\.readyState",
            js,
        )
    ) or bool(
        # A call statement at the top level: the script is deferred, so the
        # DOM is already parsed and a top-level call is enough to start.
        re.search(
            r"(?m)^(?![ \t])(?!(?:function|const|let|var|class|if|for|while|return|switch|try)\b)"
            r"([A-Za-z_$][\w$]*)\s*\(",
            js,
        )
    )
    checks.append(
        UiCheck(
            name="renders_on_load",
            ok=mounts and starts,
            detail=(
                (
                    f"app.js does not render into the mount element "
                    f"{('#' + mount_id) if mount_id else '(none found in index.html)'}"
                    if not mounts
                    else "app.js never starts: no DOMContentLoaded listener and no "
                    "top-level render call, so the page stays blank"
                )
                if not (mounts and starts)
                else f"app.js renders into #{mount_id} on load"
            ),
        )
    )

    # 15. clean, modern JavaScript: no `var`, no crude browser dialogs, no
    # document.write. These are the tells of code nobody meant to ship.
    crude = sorted(
        {"var" for _ in re.finditer(r"(?m)^\s*var\s+[A-Za-z_$]", js)}
        | {token for token in ("document.write", "alert(", "prompt(", "confirm(") if token in js}
    )
    checks.append(
        UiCheck(
            name="modern_js",
            ok=not crude,
            detail=(
                f"outdated or crude construct(s): {crude} -- use const/let instead of "
                "var, and report a problem in the interface instead of alert(), "
                "prompt(), confirm() or document.write"
                if crude
                else "const/let throughout, no browser dialogs"
            ),
        )
    )

    # 16. semantic landmarks: a screen reader and a keyboard user both need
    # to know where the content starts.
    landmarks = [
        tag
        for tag in ("main", "header", "nav", "section", "form")
        if re.search(rf"<{tag}\b", html, re.I)
    ]
    checks.append(
        UiCheck(
            name="semantic_html",
            ok=bool(landmarks),
            detail=(
                f"semantic landmark(s) found: {landmarks}"
                if landmarks
                else "index.html must wrap the app in a <main> element that holds the "
                "mount point (a <header> for the title bar and a <form> for a form)"
            ),
        )
    )

    return UiReport(ok=all(c.ok for c in checks), checks=checks)


def gate_feedback(report: UiReport) -> str:
    """Render gate failures as instructions for a UI repair pass."""
    return "\n".join(f"- {c.name}: {c.detail}" for c in report.failures)


def is_ui_eligible(spec: ApiSpec | None) -> bool:
    """Anything with an HTTP contract gets an interface; a program has nothing to fetch from.

    A full-stack application always has one: the interface is part of the
    product, not an extra. An api-kind project only gets one when the run
    asked for it.
    """
    if not spec or not spec.openapi_yaml:
        return False
    return getattr(spec, "kind", "api") in {"api", "fullstack"}
