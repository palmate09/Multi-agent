"""Tester: spec+stories -> pytest suite. NEVER reads code (AgentCoder anti-bias)."""

from __future__ import annotations

from agents.llm import generate
from schemas.messages import ApiSpec, TestSuite, UserStories

TEMPLATE_TESTS = '''"""Auto-generated Tester suite: success + validation + error per endpoint."""
import os
os.environ["TASKS_DB"] = "./test_tasks.db"
import pathlib
for p in ("./test_tasks.db", "./tasks.db"):
    try:
        pathlib.Path(p).unlink()
    except FileNotFoundError:
        pass
from fastapi.testclient import TestClient
import main
client = TestClient(main.app)

def test_health():
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}

def test_create_task_success():
    r = client.post("/tasks", json={"title": "Buy milk"})
    assert r.status_code == 201
    body = r.json()
    assert body["title"] == "Buy milk"
    assert "id" in body

def test_create_task_validation():
    r = client.post("/tasks", json={"title": ""})
    assert r.status_code == 422
    r = client.post("/tasks", json={})
    assert r.status_code == 422

def test_list_tasks():
    client.post("/tasks", json={"title": "A"})
    r = client.get("/tasks")
    assert r.status_code == 200
    assert isinstance(r.json(), list)
    assert len(r.json()) >= 1

def test_get_task_and_404():
    c = client.post("/tasks", json={"title": "G"})
    tid = c.json()["id"]
    r = client.get(f"/tasks/{tid}")
    assert r.status_code == 200
    assert r.json()["id"] == tid
    r = client.get("/tasks/999999")
    assert r.status_code == 404

def test_update_task_and_404():
    c = client.post("/tasks", json={"title": "U"})
    tid = c.json()["id"]
    r = client.put(f"/tasks/{tid}", json={"title": "U2", "done": True})
    assert r.status_code == 200
    assert r.json()["title"] == "U2"
    r = client.put("/tasks/999999", json={"title": "x"})
    assert r.status_code == 404

def test_delete_task_and_404():
    c = client.post("/tasks", json={"title": "D"})
    tid = c.json()["id"]
    r = client.delete(f"/tasks/{tid}")
    assert r.status_code == 200
    assert client.get(f"/tasks/{tid}").status_code == 404
    assert client.delete("/tasks/999999").status_code == 404
'''

SOP = (
    "You are the Tester. Given ONLY the OpenAPI spec + stories, output ONE pytest file "
    "using fastapi.testclient covering success/validation/error for every endpoint. "
    "No prose, code only."
)


def spec_to_tests(spec: ApiSpec, stories: UserStories) -> TestSuite:
    listing = "\n".join(f"- {s.title}: {'; '.join(s.acceptance)}" for s in stories.stories)
    text, _ = generate(
        f"Spec:\n{spec.openapi_yaml[:2000]}\nStories:\n{listing}\nEmit pytest only.",
        SOP,
        role="tester",
        max_tokens=4000,
    )
    if text and "def test_" in text and "TestClient" in text:
        return TestSuite(files={"test_tasks.py": text})
    return TestSuite(files={"test_tasks.py": TEMPLATE_TESTS})
