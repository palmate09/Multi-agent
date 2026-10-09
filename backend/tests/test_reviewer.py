"""Reviewer: spec-driven conformance instead of task-manager literals.

The old reviewer demanded ``/tasks``, ``min_length`` and 422/404 literals, so a
correct library API was rejected with "Missing endpoint /tasks". These tests pin
the domain-agnostic behaviour.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agents import reviewer as R
from agents.designer import _valid
from schemas.messages import ApiSpec, CodeBundle, TestSuite

LIBRARY_SPEC = """openapi: 3.1.0
info: {title: Library API, version: 1.0.0}
paths:
  /health:
    get:
      responses: {'200': {description: ok}}
  /loans:
    post:
      responses:
        '201': {description: created}
        '404': {description: unknown isbn}
        '422': {description: missing isbn}
"""

GOOD_CODE = """from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import Column, Integer, String, create_engine
from sqlalchemy.orm import declarative_base

Base = declarative_base()
app = FastAPI()


class LoanIn(BaseModel):
    isbn: str = Field(min_length=1)


class Loan(Base):
    __tablename__ = "loans"
    id = Column(Integer, primary_key=True)
    isbn = Column(String)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/loans", status_code=201)
def borrow(payload: LoanIn):
    return {"isbn": payload.isbn}
"""

GOOD_TESTS = """import app
from fastapi.testclient import TestClient

def test_health():
    assert TestClient(app.app).get("/health").status_code == 200

def test_borrow_unknown_isbn():
    assert TestClient(app.app).post("/loans", json={"isbn": "x"}).status_code == 404

def test_borrow_validation():
    assert TestClient(app.app).post("/loans", json={}).status_code == 422
"""


@pytest.fixture(autouse=True)
def _no_llm(monkeypatch):
    """The LLM must not inject findings into these assertions."""
    monkeypatch.setattr(R, "generate", lambda *a, **k: ("", {"ok": False}))


def _bundle(body: str = GOOD_CODE) -> CodeBundle:
    from agents.introspect import find_app_object

    module, attr = find_app_object({"app.py": body}) or ("app", "app")
    return CodeBundle(files={"app.py": body}, entry_module=module, entry_attr=attr)


def _suite(body: str = GOOD_TESTS) -> TestSuite:
    return TestSuite(files={"test_app.py": body})


def test_correct_library_app_has_no_blockers():
    spec = ApiSpec(openapi_yaml=LIBRARY_SPEC, endpoints=["/health", "/loans"])
    comments = R._static_checks(_bundle(), spec, _suite(), report=None)
    blockers = [c for c in comments if c.severity == "blocker"]
    assert blockers == [], [c.message for c in blockers]


def test_library_app_is_not_flagged_for_missing_tasks():
    """The exact defect: a correct app must not be told /tasks is missing."""
    spec = ApiSpec(openapi_yaml=LIBRARY_SPEC)
    messages = [c.message for c in R._static_checks(_bundle(), spec, _suite(), None)]
    assert not any("/tasks" in m for m in messages)


def test_no_literal_min_length_requirement():
    """Validation is detected structurally, not by grepping one library's idiom."""
    no_field = GOOD_CODE.replace("isbn: str = Field(min_length=1)", "isbn: str")
    spec = ApiSpec(openapi_yaml=LIBRARY_SPEC)
    comments = R._static_checks(_bundle(no_field), spec, _suite(), None)
    assert not any("validation" in c.message for c in comments)


def test_missing_spec_endpoint_is_a_blocker():
    body = GOOD_CODE.replace('@app.post("/loans", status_code=201)', "")
    body = body.replace("def borrow(payload: LoanIn):", "def _unused(payload: LoanIn):")
    spec = ApiSpec(openapi_yaml=LIBRARY_SPEC)
    blockers = [
        c for c in R._static_checks(_bundle(body), spec, _suite(), None) if c.severity == "blocker"
    ]
    assert any("/loans" in c.message for c in blockers), [c.message for c in blockers]


def test_unsafe_sql_is_a_blocker_in_any_domain():
    body = GOOD_CODE + '\nquery = f"SELECT * FROM loans WHERE id={id}"\n'
    spec = ApiSpec(openapi_yaml=LIBRARY_SPEC)
    blockers = [
        c for c in R._static_checks(_bundle(body), spec, _suite(), None) if c.severity == "blocker"
    ]
    assert any("Unsafe" in c.message for c in blockers)


def test_missing_app_object_is_a_blocker():
    spec = ApiSpec(openapi_yaml=LIBRARY_SPEC)
    code = CodeBundle(files={"helpers.py": "x = 1\n"})
    comments = R._static_checks(code, spec, _suite(), None)
    assert any("FastAPI application object" in c.message for c in comments)


def test_tests_that_never_import_the_app_are_a_blocker():
    spec = ApiSpec(openapi_yaml=LIBRARY_SPEC)
    suite = TestSuite(files={"test_app.py": "def test_x():\n    assert 1 == 1\n404\n422\n"})
    comments = R._static_checks(_bundle(), spec, suite, None)
    assert any("never import" in c.message for c in comments)


def test_absent_error_codes_are_reported():
    """Spec promises 404/422; code that never returns them is a gap."""
    body = GOOD_CODE.replace("status_code=201", "status_code=201")
    body = body.replace("isbn: str = Field(min_length=1)", "isbn: str")
    spec = ApiSpec(openapi_yaml=LIBRARY_SPEC)
    messages = [c.message for c in R._static_checks(_bundle(body), spec, _suite(), None)]
    assert any("404" in m for m in messages)


def test_planted_bug_is_found_recall():
    """Plant a real defect and require the reviewer to name it."""
    broken = GOOD_CODE.replace(
        '@app.post("/loans", status_code=201)\ndef borrow(payload: LoanIn):\n    return {"isbn": payload.isbn}',
        'query = f"SELECT * FROM loans WHERE id={loan_id}"',
    )
    spec = ApiSpec(openapi_yaml=LIBRARY_SPEC)
    from schemas.messages import TestReport

    rep = R.review(_bundle(broken), spec, _suite(), TestReport(passed=1, failed=0))
    messages = [c.message for c in rep.comments]
    assert rep.blockers, messages
    assert any("Unsafe" in m or "/loans" in m for m in messages), messages


def test_malformed_spec_does_not_crash_reviewer():
    """An unparseable spec must degrade, not raise."""
    spec = ApiSpec(openapi_yaml="not a spec at all")
    assert R._static_checks(_bundle(), spec, _suite(), None) is not None


def test_spec_validator_accepts_the_library_spec():
    ok, why = _valid(LIBRARY_SPEC)
    assert ok, why
