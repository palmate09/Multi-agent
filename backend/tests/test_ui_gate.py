"""The UI gate and the UI agents' parsing, offline and without a model.

The gate is the only thing standing between a 3b model's UI and the preview, so
each check is tested as its own case rather than through one happy-path bundle.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agents import ui
from agents.blocks import parse_file_blocks
from agents.llm import GenerationError
from schemas.messages import ApiSpec, CodeBundle, UiSpec

SPEC_YAML = (
    "openapi: 3.1.0\n"
    "info: {title: Recipes, version: 1.0.0}\n"
    "paths:\n"
    "  /recipes:\n"
    "    get:\n      responses: {'200': {description: ok}}\n"
    "    post:\n      responses: {'201': {description: ok}}\n"
    "  /health:\n"
    "    get:\n      responses: {'200': {description: ok}}\n"
)

GOOD_HTML = (
    '<!doctype html>\n<html><head><link rel="stylesheet" href="styles.css">'
    '<script src="app.js" defer></script></head>'
    '<body><div id="root"></div></body></html>'
)
GOOD_JS = (
    "async function load() {\n"
    "  const res = await fetch('recipes');\n"
    "  const data = await res.json();\n"
    "  const list = document.createElement('ul');\n"
    "  for (const r of data) {\n"
    "    const li = document.createElement('li');\n"
    "    li.textContent = r.title;\n"
    "    list.appendChild(li);\n"
    "  }\n"
    "  document.getElementById('root').appendChild(list);\n"
    "}\n"
    "load();\n"
)
GOOD_CSS = "body { font-family: system-ui; }"

GOOD_FILES = {"index.html": GOOD_HTML, "app.js": GOOD_JS, "styles.css": GOOD_CSS}
GOOD_SPEC = UiSpec(
    pages=["/"],
    components=["recipe-list"],
    data_bindings=["GET /recipes -> list of recipes", "POST /recipes -> add form"],
    files=["index.html", "app.js", "styles.css"],
)


@pytest.fixture()
def api_spec():
    return ApiSpec(openapi_yaml=SPEC_YAML, endpoints=["/recipes", "/health"], files=["main.py"])


def _check(report, name):
    return next(c for c in report.checks if c.name == name)


# ------------------------------------------------------------------- gate ----
def test_all_checks_pass_on_a_good_ui(api_spec):
    report = ui.gate(api_spec, GOOD_SPEC, GOOD_FILES)
    assert report.ok, [c.detail for c in report.failures]
    assert len(report.checks) == 7


def test_missing_file_is_reported(api_spec):
    report = ui.gate(api_spec, GOOD_SPEC, {**GOOD_FILES, "styles.css": "   "})
    assert not report.ok
    assert not _check(report, "missing_files").ok
    assert "styles.css" in _check(report, "missing_files").detail


def test_missing_index_html_fails_even_with_the_script_present(api_spec):
    files = {k: v for k, v in GOOD_FILES.items() if k != "index.html"}
    report = ui.gate(api_spec, GOOD_SPEC, files)
    assert not report.ok
    assert not _check(report, "missing_files").ok
    assert not _check(report, "no_mount").ok


def test_rooted_fetch_is_caught(api_spec):
    """The failure that matters most: a root-anchored URL hits the platform API."""
    js = GOOD_JS.replace("fetch('recipes')", "fetch('/recipes')")
    report = ui.gate(api_spec, GOOD_SPEC, {**GOOD_FILES, "app.js": js})
    assert not report.ok
    assert not _check(report, "rooted_url").ok
    assert "/preview/" in _check(report, "rooted_url").detail


def test_relative_fetch_is_allowed(api_spec):
    assert _check(ui.gate(api_spec, GOOD_SPEC, GOOD_FILES), "rooted_url").ok


def test_external_cdn_is_caught(api_spec):
    html = GOOD_HTML.replace('href="styles.css"', 'href="https://unpkg.com/x.css"')
    report = ui.gate(api_spec, GOOD_SPEC, {**GOOD_FILES, "index.html": html})
    assert not _check(report, "external_asset").ok


def test_interpolated_innerhtml_is_caught(api_spec):
    js = GOOD_JS.replace(
        "li.textContent = r.title;",
        'root.innerHTML = data.map(r => `<li>${r.title}</li>`).join("");',
    )
    report = ui.gate(api_spec, GOOD_SPEC, {**GOOD_FILES, "app.js": js})
    assert not _check(report, "xss_innerhtml").ok


def test_static_innerhtml_is_allowed(api_spec):
    """A literal markup string is not an XSS sink; only interpolation is."""
    js = "document.getElementById('root').innerHTML = '<p>loading</p>';\n"
    assert _check(ui.gate(api_spec, GOOD_SPEC, {**GOOD_FILES, "app.js": js}), "xss_innerhtml").ok


def test_binding_to_an_undeclared_endpoint_is_caught(api_spec):
    spec = GOOD_SPEC.model_copy(update={"data_bindings": ["GET /nonexistent -> table"]})
    report = ui.gate(api_spec, spec, GOOD_FILES)
    assert not _check(report, "undeclared_endpoint").ok


def test_unbound_spec_endpoint_is_reported(api_spec):
    yaml = SPEC_YAML + "  /tags:\n    get:\n      responses: {'200': {description: ok}}\n"
    spec = ApiSpec(openapi_yaml=yaml, files=["main.py"])
    ui_spec = UiSpec(pages=["/"], data_bindings=["GET /recipes -> list"], files=["index.html"])
    report = ui.gate(spec, ui_spec, {"index.html": GOOD_HTML})
    unbound = _check(report, "unbound_endpoint")
    assert not unbound.ok
    assert "/tags" in unbound.detail


def test_health_endpoint_does_not_have_to_be_bound(api_spec):
    """A page never renders the health probe, so it must not be a gate failure."""
    unbound = _check(ui.gate(api_spec, GOOD_SPEC, GOOD_FILES), "unbound_endpoint")
    assert unbound.ok, unbound.detail


def test_missing_mount_element_is_caught(api_spec):
    html = GOOD_HTML.replace('id="root"', 'id="something-else"')
    report = ui.gate(api_spec, GOOD_SPEC, {**GOOD_FILES, "index.html": html})
    assert not _check(report, "no_mount").ok


def test_missing_script_tag_is_caught(api_spec):
    html = GOOD_HTML.replace('<script src="app.js" defer></script>', "")
    report = ui.gate(api_spec, GOOD_SPEC, {**GOOD_FILES, "index.html": html})
    assert not _check(report, "no_mount").ok


def test_path_parameters_match_regardless_of_name(api_spec):
    """A UI binding is allowed to name a different parameter than the spec."""
    yaml = (
        SPEC_YAML
        + "  /recipes/{recipe_id}:\n    get:\n      responses: {'200': {description: ok}}\n"
    )
    spec = ApiSpec(openapi_yaml=yaml, files=["main.py"])
    ui_spec = UiSpec(
        pages=["/"], data_bindings=["GET /recipes/{id} -> detail"], files=["index.html"]
    )
    report = ui.gate(spec, ui_spec, {"index.html": GOOD_HTML})
    assert _check(report, "undeclared_endpoint").ok


# ---------------------------------------------------------------- designer ----
def _json_reply(payload: str) -> str:
    return f"Here is the plan.\n```json\n{payload}\n```\n"


def test_designer_accepts_a_valid_contract(api_spec, monkeypatch):
    monkeypatch.setattr(
        ui,
        "require",
        lambda *a, **k: (
            _json_reply(
                '{"pages": ["/"], "components": ["list"], '
                '"data_bindings": ["GET /recipes -> table"], '
                '"files": ["index.html", "app.js", "styles.css"]}'
            ),
            {},
        ),
    )
    spec = ui.design_ui(api_spec)
    assert spec.pages == ["/"]
    assert spec.files[0] == "index.html"


@pytest.mark.parametrize(
    "payload, fragment",
    [
        # No index.html: the preview has nothing to load.
        (
            '{"pages": ["/"], "data_bindings": ["GET /recipes -> t"], "files": ["app.js"]}',
            "index.html",
        ),
        # No pages.
        ('{"data_bindings": ["GET /recipes -> t"], "files": ["index.html"]}', "pages"),
        # No bindings at all.
        ('{"files": ["index.html"], "pages": ["/"]}', "data_bindings"),
        # Binds only to an endpoint the API does not serve.
        (
            '{"files": ["index.html"], "pages": ["/"], "data_bindings": ["GET /nope -> t"]}',
            "the API declares",
        ),
    ],
)
def test_designer_rejects_a_bad_contract(api_spec, monkeypatch, payload, fragment):
    monkeypatch.setattr(ui, "require", lambda *a, **k: (_json_reply(payload), {}))
    with pytest.raises(GenerationError) as exc:
        ui.design_ui(api_spec, attempts=2)
    assert fragment in str(exc.value)


def test_designer_rejects_prose(api_spec, monkeypatch):
    monkeypatch.setattr(
        ui, "require", lambda *a, **k: ("I would build a dashboard for recipes.", {})
    )
    with pytest.raises(GenerationError) as exc:
        ui.design_ui(api_spec, attempts=2)
    assert "json block" in str(exc.value)


def test_designer_refuses_when_there_is_no_api(monkeypatch):
    monkeypatch.setattr(ui, "require", lambda *a, **k: (_json_reply("{}"), {}))
    with pytest.raises(GenerationError):
        ui.design_ui(ApiSpec(openapi_yaml="", kind="api"))


# --------------------------------------------------------------- developer ----
def test_developer_parse_guard_rejects_a_headerless_reply(api_spec, monkeypatch):
    """A prose reply must never become a stray ``test_tasks.py``.

    ``parse_file_blocks`` falls back to exactly that filename when a reply has
    no ``###`` headers, which in the preview directory would be a ``.py`` file
    sitting next to index.html and would also satisfy the sandbox's "is there
    python?" check.
    """
    monkeypatch.setattr(ui, "require", lambda *a, **k: ("Here is your UI, hope it helps!", {}))
    with pytest.raises(GenerationError) as exc:
        ui.build_ui(api_spec, None, GOOD_SPEC)
    assert "filename" in str(exc.value)
    # The shared parser's own behaviour, asserted so the guard has a reason.
    assert parse_file_blocks("no headers here") == {"test_tasks.py": "no headers here"}


def test_developer_keeps_only_planned_files(api_spec, monkeypatch):
    reply = (
        "### index.html\n```\n" + GOOD_HTML + "\n```\n"
        "### app.js\n```\n" + GOOD_JS + "\n```\n"
        "### styles.css\n```\n" + GOOD_CSS + "\n```\n"
        "### surprise.py\n```\nprint('nope')\n```\n"
    )
    monkeypatch.setattr(ui, "require", lambda *a, **k: (reply, {}))
    bundle = ui.build_ui(api_spec, None, GOOD_SPEC)
    assert set(bundle.files) == {"index.html", "app.js", "styles.css"}
    assert bundle.entry_module == ""  # no ASGI app in a UI bundle


def test_developer_rejects_a_bundle_without_index_html(api_spec, monkeypatch):
    reply = "### app.js\n```\n" + GOOD_JS + "\n```\n### styles.css\n```\n" + GOOD_CSS + "\n```\n"
    monkeypatch.setattr(ui, "require", lambda *a, **k: (reply, {}))
    with pytest.raises(GenerationError) as exc:
        ui.build_ui(api_spec, None, GOOD_SPEC)
    assert "index.html" in str(exc.value)


def test_developer_returns_a_code_bundle(api_spec, monkeypatch):
    reply = (
        "### index.html\n```\n" + GOOD_HTML + "\n```\n"
        "### app.js\n```\n" + GOOD_JS + "\n```\n"
        "### styles.css\n```\n" + GOOD_CSS + "\n```\n"
    )
    monkeypatch.setattr(ui, "require", lambda *a, **k: (reply, {}))
    bundle = ui.build_ui(api_spec, None, GOOD_SPEC, entry_module="main", entry_attr="app")
    assert isinstance(bundle, CodeBundle)
    assert bundle.entry_module == "main"


# ------------------------------------------------------------- eligibility ----
def test_only_api_projects_are_ui_eligible():
    assert ui.is_ui_eligible(ApiSpec(openapi_yaml=SPEC_YAML)) is True
    assert ui.is_ui_eligible(ApiSpec(openapi_yaml="", kind="program")) is False
    assert ui.is_ui_eligible(None) is False
