"""Write a finished `accepted` run into E2E_RUNS_DIR.

The console's run-detail tabs (stories, plan, OpenAPI, code, tests, report,
review) only render from a run that has artifacts on disk. Without a live model
there is no such run, so these tests would only ever be able to assert the empty
and error states.

This writes exactly the artifact shape a finished run has -- the same shape
`backend/tests/conftest.py`'s `fake_pipeline` produces -- so the whole run-detail
console is exercisable with no LLM and no backend code change.

    python e2e/fixtures/seed_run.py <runs_dir>

Idempotent: re-running overwrites.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

RUN_ID = "verify_library_e2e"

REQUIREMENT = (
    "Build a library REST API that lends books: CRUD /books with ISBN "
    "validation and /loans, 404 on unknown ISBN."
)

APP_PY = """from fastapi import FastAPI
from sqlalchemy import Column, Integer, String
from sqlalchemy.orm import declarative_base

Base = declarative_base()
app = FastAPI()


class Book(Base):
    __tablename__ = "books"
    id = Column(Integer, primary_key=True)
    isbn = Column(String)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/books")
def books() -> list:
    return []
"""

TEST_PY = """import app
from fastapi.testclient import TestClient


def test_health() -> None:
    assert TestClient(app.app).get("/health").status_code == 200
"""

SPEC_YAML = """openapi: 3.1.0
info:
  title: Library API
  version: 1.0.0
paths:
  /health:
    get:
      responses:
        '200':
          description: ok
  /books:
    get:
      responses:
        '200':
          description: ok
"""

STORIES = {
    # rehydrate() reads this to label the restored run.
    "requirement": REQUIREMENT,
    "stories": [
        {"id": "US1", "title": "List books", "acceptance": ["GET /books returns 200"]},
        {
            "id": "US2",
            "title": "Look a book up by ISBN",
            "acceptance": ["GET /books/{isbn} 404 on unknown ISBN"],
        },
    ],
    "clarifying_question": "Should loans be a separate resource, or fields on a book?",
}

PLAN = {
    "approach": "One SQLModel table for books and one for loans, joined by isbn. "
    "SQLite in a file under the run directory so the sandbox can import the app.",
    "decisions": ["SQLite over in-memory so the boot check survives a re-import"],
    "risks": ["ISBN validation has no offline authority to check against"],
    "edge_cases": ["Duplicate ISBN on insert", "Loan of an already-loaned book"],
    "open_questions": ["Should returns be modelled or a loan state change?"],
    "structured": True,
}

REPORT = {"passed": 5, "failed": 0, "raw": "===== 5 passed in 0.42s ====="}

COVERAGE = {"covered": ["book", "isbn", "loan"], "missing": [], "conclusive": True}

REVIEW = {
    "comments": [
        {
            "severity": "major",
            "message": "Loans endpoint does not validate that the book exists.",
            "location": "app.py:88",
        },
        {
            "severity": "minor",
            "message": "Inconsistent quoting in the generated OpenAPI block.",
            "location": "spec.yaml:12",
        },
    ]
}

SUMMARY = {
    "status": "accepted",
    "created_at": "2026-10-13T10:00:00Z",
    "error": None,
    "tests_passed": 5,
    "tests_failed": 0,
    "attempts_dev": 1,
    "attempts_test": 0,
    "review_rounds": 1,
    "entrypoint": "app",
    "files": ["app.py"],
    "wall_time": 73.0,
}

# NOTE: there is deliberately no events file. `Run.events` is an in-memory
# deque mirrored into the database index; `rehydrate()` does not replay
# events from disk, so a seeded run legitimately has an empty timeline.
# The tests assert on artifacts, and on the empty-state copy for the log.


def seed(runs_dir: Path) -> Path:
    out = runs_dir / RUN_ID
    if out.exists():
        shutil.rmtree(out)
    (out / "code").mkdir(parents=True)
    (out / "tests").mkdir(parents=True)

    (out / "code" / "app.py").write_text(APP_PY)
    (out / "tests" / "test_app.py").write_text(TEST_PY)
    (out / "spec.yaml").write_text(SPEC_YAML)
    (out / "stories.json").write_text(json.dumps(STORIES, indent=2))
    (out / "reasoning.json").write_text(json.dumps(PLAN, indent=2))
    (out / "coverage.json").write_text(json.dumps(COVERAGE, indent=2))
    (out / "review.json").write_text(json.dumps(REVIEW, indent=2))
    (out / "report_try1.json").write_text(json.dumps(REPORT, indent=2))
    (out / "summary.json").write_text(json.dumps(SUMMARY, indent=2))
    return out


if __name__ == "__main__":
    target = Path(sys.argv[1] if len(sys.argv) > 1 else "e2e/fixtures/runs")
    written = seed(target)
    print(f"seeded {written}")