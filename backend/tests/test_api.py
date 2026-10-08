"""Tests for the HTTP API layer (routes, validation, streaming, security)."""

import json
import time

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import create_app
from app.services.runs import RunStore


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNS_DIR", str(tmp_path / "runs"))
    get_settings.cache_clear()
    app = create_app()
    with TestClient(app) as c:
        yield c
    get_settings.cache_clear()


REQUIREMENT = (
    "Build a REST API for managing tasks with SQLite persistence, "
    "CRUD /tasks plus GET /health, 422 on empty title, 404 unknown id."
)


def _wait_for(client, run_id, timeout=90):
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = client.get(f"/api/runs/{run_id}")
        if r.status_code == 200 and r.json()["status"] not in {"queued", "running"}:
            return r.json()
        time.sleep(0.2)
    raise AssertionError(f"run {run_id} did not settle within {timeout}s")


def test_health_reports_backend_state(client):
    body = client.get("/health").json()
    assert body["status"] in {"ok", "degraded"}
    assert "llm" in body
    assert body["max_concurrent_runs"] >= 1


def test_root_advertises_docs(client):
    body = client.get("/").json()
    assert body["docs"] == "/api/docs"


def test_create_run_executes_and_settles(client):
    resp = client.post("/api/runs", json={"requirement": REQUIREMENT})
    assert resp.status_code == 202
    run_id = resp.json()["run_id"]

    detail = _wait_for(client, run_id)
    assert detail["status"].startswith("accepted"), detail
    assert detail["tests_passed"] > 0
    assert detail["tests_failed"] == 0
    assert "main.py" in detail["code"]
    assert detail["tests"]


def test_run_rejects_short_requirement(client):
    resp = client.post("/api/runs", json={"requirement": "api"})
    assert resp.status_code == 422


def test_run_rejects_empty_requirement(client):
    assert client.post("/api/runs", json={"requirement": ""}).status_code == 422


def test_missing_run_returns_404(client):
    assert client.get("/api/runs/does-not-exist").status_code == 404


def test_list_runs_includes_created_run(client):
    created = client.post("/api/runs", json={"requirement": REQUIREMENT}).json()
    ids = [r["run_id"] for r in client.get("/api/runs").json()]
    assert created["run_id"] in ids


def test_events_endpoint_replays_history(client):
    run_id = client.post("/api/runs", json={"requirement": REQUIREMENT}).json()["run_id"]
    _wait_for(client, run_id)
    with client.stream("GET", f"/api/runs/{run_id}/events") as resp:
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        body = "".join(resp.iter_text())
    events = [json.loads(line[6:]) for line in body.splitlines() if line.startswith("data: ")]
    assert any(e.get("phase") == "start" for e in events)
    assert any(e.get("node") == "final" for e in events)
    seqs = [e["seq"] for e in events if "seq" in e]
    assert seqs == sorted(seqs), "events must be ordered"


def test_artifact_download_and_traversal_blocked(client):
    run_id = client.post("/api/runs", json={"requirement": REQUIREMENT}).json()["run_id"]
    _wait_for(client, run_id)

    ok = client.get(f"/api/runs/{run_id}/artifacts/code/main.py")
    assert ok.status_code == 200
    assert "FastAPI" in ok.text

    listing = client.get(f"/api/runs/{run_id}/artifacts").json()
    assert any(f.endswith("summary.json") for f in listing["files"])

    for attempt in ("../../../../etc/passwd", "..%2f..%2fetc%2fpasswd", ".env"):
        assert client.get(f"/api/runs/{run_id}/artifacts/{attempt}").status_code == 404


def test_delete_run_removes_it(client):
    run_id = client.post("/api/runs", json={"requirement": REQUIREMENT}).json()["run_id"]
    _wait_for(client, run_id)
    assert client.delete(f"/api/runs/{run_id}").status_code == 204
    assert client.get(f"/api/runs/{run_id}").status_code == 404


def test_ablation_flags_are_honoured(client):
    run_id = client.post(
        "/api/runs",
        json={"requirement": REQUIREMENT, "skip_reviewer": True},
    ).json()["run_id"]
    detail = _wait_for(client, run_id)
    assert detail["status"] == "accepted_no_review"


def test_evals_endpoints(client):
    results = client.get("/api/evals/results").json()
    assert "runs" in results and "total" in results
    assert client.get("/api/evals/suite").json()["requirements"]
    assert "markdown" in client.get("/api/evals/reference").json()


def test_store_rehydrates_existing_directories(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNS_DIR", str(tmp_path / "runs"))
    get_settings.cache_clear()
    root = tmp_path / "runs" / "prior-run"
    root.mkdir(parents=True)
    (root / "summary.json").write_text(
        json.dumps(
            {
                "status": "accepted",
                "tests_passed": 7,
                "tests_failed": 0,
                "attempts_dev": 0,
                "attempts_test": 0,
                "review_rounds": 0,
                "wall_time": 1.2,
            }
        )
    )
    store = RunStore(get_settings())
    try:
        assert store.rehydrate() == 1
        assert store.get("prior-run").status == "accepted"
    finally:
        store.shutdown()
    get_settings.cache_clear()


def test_store_generates_unique_ids(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNS_DIR", str(tmp_path / "runs"))
    get_settings.cache_clear()
    store = RunStore(get_settings())
    try:
        a = store.new_id("my/run")
        assert "/" not in a, "unsafe characters must be stripped"
        store.create("x" * 20, run_id="same")
        second = store.new_id("same")
        assert second != "same"
    finally:
        store.shutdown()
    get_settings.cache_clear()
