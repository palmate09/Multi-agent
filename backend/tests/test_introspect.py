"""Entrypoint discovery, route extraction and spec conformance.

These replace tests that asserted the old behaviour: that generation always
produced ``main.py`` and that the spec always declared ``/tasks``.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agents.introspect import (
    all_routes,
    find_app_object,
    normalise_path,
    spec_paths,
    uncovered_spec_endpoints,
)

LIBRARY_APP = """
from fastapi import FastAPI
from sqlalchemy import Column, Integer, String, create_engine
app = FastAPI()

class Loan(Base):
    __tablename__ = "loans"
    id = Column(Integer, primary_key=True)
    isbn = Column(String)

@app.get("/health")
def health():
    return {"status": "ok"}

@app.post("/loans")
def borrow(isbn: str):
    return {"isbn": isbn}

@app.get("/books/{isbn}")
def one(isbn: str):
    return {"isbn": isbn}
"""

LIBRARY_SPEC = """openapi: 3.1.0
info:
  title: Library API
  version: 1.0.0
paths:
  /health:
    get:
      responses:
        '200':
          description: ok
  /loans:
    post:
      responses:
        '201':
          description: created
        '404':
          description: unknown isbn
  /books/{isbn}:
    get:
      responses:
        '200':
          description: ok
"""


def test_entrypoint_found_under_any_name():
    """The entrypoint is discovered, not assumed to be ``main``."""
    assert find_app_object({"app.py": LIBRARY_APP}) == ("app", "app")
    assert find_app_object({"server.py": LIBRARY_APP}) == ("server", "app")
    # A module in a subdirectory keeps its dotted path, which is importable
    # because the runner makes directories containing modules into packages.
    assert find_app_object({"nested/service.py": LIBRARY_APP}) == ("nested.service", "app")


def test_entrypoint_prefers_real_fastapi_instance():
    """A bare ``api = get_api()`` must not outrank ``app = FastAPI()``."""
    files = {
        "db.py": "engine = get_engine()\n",
        "app.py": "app = FastAPI()\napi = get_api()\n",
    }
    assert find_app_object(files) == ("app", "app")


def test_entrypoint_ignores_commented_out_app():
    files = {"a.py": "# app = FastAPI()\n'note: app = FastAPI()'\n"}
    assert find_app_object(files) is None


def test_entrypoint_absent_returns_none():
    assert find_app_object({"helpers.py": "x = 1\n"}) is None
    assert find_app_object({"broken.py": "def f(:\n"}) is None


def test_routes_extracted_from_decorators():
    routes = all_routes({"app.py": LIBRARY_APP})
    assert ("/health", "GET") in routes
    assert ("/loans", "POST") in routes
    assert ("/books/{isbn}", "GET") in routes
    assert len(routes) == 3


def test_api_route_with_methods_list():
    src = (
        "from fastapi import FastAPI\napp=FastAPI()\n"
        "@app.api_route('/x', methods=['GET','POST'])\ndef x():\n    return {}\n"
    )
    assert all_routes({"a.py": src}) == {("/x", "GET"), ("/x", "POST")}


def test_doc_paths_excluded_from_routes():
    src = (
        "from fastapi import FastAPI\napp=FastAPI()\n@app.get('/health')\ndef h():\n    return {}\n"
    )
    assert ("/docs", "GET") not in all_routes({"a.py": src})


def test_path_params_normalise_across_naming():
    assert normalise_path("/books/{isbn}") == normalise_path("/books/{book_id}")
    assert normalise_path("/tasks/") == "/tasks"


def test_spec_paths_parsed():
    pairs, paths = spec_paths(LIBRARY_SPEC)
    assert ("/loans", "POST") in pairs
    assert paths == {"/health", "/loans", "/books/{isbn}"}


def test_matching_spec_reports_full_conformance():
    assert uncovered_spec_endpoints(LIBRARY_SPEC, {"app.py": LIBRARY_APP}) == []


def test_missing_endpoint_is_detected():
    """A spec promise with no route is a real gap, not a naming quibble."""
    spec = (
        LIBRARY_SPEC
        + "  /returns:\n    post:\n      responses:\n        '201':\n          description: ok\n"
    )
    missing = uncovered_spec_endpoints(spec, {"app.py": LIBRARY_APP})
    assert missing == [("/returns", "POST")]


def test_param_name_difference_is_not_a_mismatch():
    spec = LIBRARY_SPEC.replace("{isbn}", "{book_isbn}")
    assert uncovered_spec_endpoints(spec, {"app.py": LIBRARY_APP}) == []
