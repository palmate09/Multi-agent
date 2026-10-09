"""Tests for the HTTP API layer (routes, validation, streaming, security)."""

import contextlib
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


def test_create_run_executes_and_settles(client, fake_pipeline):
    """HTTP lifecycle, with generation stubbed.

    Generation is exercised by the ``live_llm`` tests; the API suite must not
    depend on a model being reachable.
    """
    resp = client.post("/api/runs", json={"requirement": REQUIREMENT})
    assert resp.status_code == 202
    run_id = resp.json()["run_id"]

    detail = _wait_for(client, run_id)
    assert detail["status"] == "accepted", detail
    assert detail["tests_passed"] > 0
    assert detail["tests_failed"] == 0
    assert "app.py" in detail["code"]
    assert detail["tests"]
    assert detail["entrypoint"] or detail["code"]


def test_run_reports_blocked_when_generation_fails(client, no_llm):
    """No backend must surface as ``blocked``, never as success."""
    resp = client.post("/api/runs", json={"requirement": REQUIREMENT})
    detail = _wait_for(client, resp.json()["run_id"])
    assert detail["status"] == "blocked", detail
    assert detail["error"]


def test_run_rejects_short_requirement(client):
    resp = client.post("/api/runs", json={"requirement": "api"})
    assert resp.status_code == 422


def test_run_rejects_empty_requirement(client):
    assert client.post("/api/runs", json={"requirement": ""}).status_code == 422


def test_missing_run_returns_404(client):
    assert client.get("/api/runs/does-not-exist").status_code == 404


def test_list_runs_includes_created_run(client, fake_pipeline):
    created = client.post("/api/runs", json={"requirement": REQUIREMENT}).json()
    ids = [r["run_id"] for r in client.get("/api/runs").json()]
    assert created["run_id"] in ids


def test_events_endpoint_replays_history(client, fake_pipeline):
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


def test_artifact_download_and_traversal_blocked(client, fake_pipeline):
    run_id = client.post("/api/runs", json={"requirement": REQUIREMENT}).json()["run_id"]
    _wait_for(client, run_id)

    ok = client.get(f"/api/runs/{run_id}/artifacts/code/app.py")
    assert ok.status_code == 200
    assert "FastAPI" in ok.text

    listing = client.get(f"/api/runs/{run_id}/artifacts").json()
    assert any(f.endswith("summary.json") for f in listing["files"])

    for attempt in ("../../../../etc/passwd", "..%2f..%2fetc%2fpasswd", ".env"):
        assert client.get(f"/api/runs/{run_id}/artifacts/{attempt}").status_code == 404


def test_delete_run_removes_it(client, fake_pipeline):
    run_id = client.post("/api/runs", json={"requirement": REQUIREMENT}).json()["run_id"]
    _wait_for(client, run_id)
    assert client.delete(f"/api/runs/{run_id}").status_code == 204
    assert client.get(f"/api/runs/{run_id}").status_code == 404


def test_ablation_flags_are_honoured(client, fake_pipeline):
    run_id = client.post(
        "/api/runs",
        json={"requirement": REQUIREMENT, "skip_reviewer": True},
    ).json()["run_id"]
    detail = _wait_for(client, run_id)
    assert detail["status"] == "accepted_no_review"


def test_skip_reasoner_reaches_the_pipeline_and_the_plan_key_exists(
    client, fake_pipeline, monkeypatch
):
    """The UI renders the plan, so the key must always be present (null-safe)."""
    from app.services import runs as runs_mod

    seen: dict = {}
    inner = runs_mod.run_team

    def spy(*a, **kw):
        seen.update(kw)
        return inner(*a, **kw)

    monkeypatch.setattr(runs_mod, "run_team", spy)
    run_id = client.post(
        "/api/runs",
        json={"requirement": REQUIREMENT, "skip_reasoner": True},
    ).json()["run_id"]
    detail = _wait_for(client, run_id)
    assert seen.get("skip_reasoner") is True
    assert "plan" in detail
    assert detail["plan"] is None


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


def _slow_cooperative_run_team(requirement, out_dir, on_event=None, run_id=None, **kw):
    """Stand-in worker: blocks until cancelled, then reports cancelled."""
    from schemas.messages import GraphState

    cancel = kw.get("cancel")
    assert cancel is not None, "worker must receive the cancel event"
    cancel.wait(20)
    return GraphState(
        requirement=requirement,
        run_id=run_id or "slow",
        status="cancelled" if cancel.is_set() else "failed",
        error="stopped by user" if cancel.is_set() else "slow stub was never stopped",
    )


def _wait_for_status(client, run_id, want, timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        got = client.get(f"/api/runs/{run_id}").json()["status"]
        if got == want:
            return got
        time.sleep(0.2)
    raise AssertionError(f"run {run_id} never reached {want}")


def test_stop_unknown_run_returns_404(client):
    assert client.post("/api/runs/no-such-run/stop").status_code == 404


def test_stop_finished_run_reports_not_stopped(client, fake_pipeline):
    run_id = client.post("/api/runs", json={"requirement": REQUIREMENT}).json()["run_id"]
    _wait_for(client, run_id)
    r = client.post(f"/api/runs/{run_id}/stop")
    assert r.status_code == 200
    assert r.json()["stopped"] is False


def test_stop_running_run_cancels_it(client, monkeypatch):
    from app.services import runs as runs_mod

    monkeypatch.setattr(runs_mod, "run_team", _slow_cooperative_run_team)
    run_id = client.post("/api/runs", json={"requirement": REQUIREMENT}).json()["run_id"]
    _wait_for_status(client, run_id, "running")
    r = client.post(f"/api/runs/{run_id}/stop")
    assert r.status_code == 200
    assert r.json()["stopped"] is True
    assert _wait_for_status(client, run_id, "cancelled") == "cancelled"


def test_delete_running_run_conflicts_then_stop_allows_delete(client, monkeypatch):
    from app.services import runs as runs_mod

    monkeypatch.setattr(runs_mod, "run_team", _slow_cooperative_run_team)
    run_id = client.post("/api/runs", json={"requirement": REQUIREMENT}).json()["run_id"]
    _wait_for_status(client, run_id, "running")
    try:
        r = client.delete(f"/api/runs/{run_id}")
        assert r.status_code == 409, "deleting under a live worker must be refused"
    finally:
        client.post(f"/api/runs/{run_id}/stop")
        _wait_for_status(client, run_id, "cancelled")
    assert client.delete(f"/api/runs/{run_id}").status_code == 204


def test_restart_unknown_run_returns_404(client):
    assert client.post("/api/runs/no-such-run/restart").status_code == 404


def test_restart_finished_run_starts_fresh_with_same_requirement(client, fake_pipeline):
    old_id = client.post("/api/runs", json={"requirement": REQUIREMENT}).json()["run_id"]
    _wait_for(client, old_id)
    r = client.post(f"/api/runs/{old_id}/restart")
    assert r.status_code == 202
    new_id = r.json()["run_id"]
    assert new_id != old_id, "restart must mint a fresh id, never resurrect the old row"
    assert r.json()["requirement"] == REQUIREMENT
    assert client.get(f"/api/runs/{old_id}").status_code == 404, "old data must be gone"
    settled = _wait_for(client, new_id)
    assert settled["status"].startswith("accepted"), settled


def test_restart_running_run_cancels_and_replaces_it(client, monkeypatch):
    from app.services import runs as runs_mod

    monkeypatch.setattr(runs_mod, "run_team", _slow_cooperative_run_team)
    old_id = client.post("/api/runs", json={"requirement": REQUIREMENT}).json()["run_id"]
    _wait_for_status(client, old_id, "running")
    try:
        r = client.post(f"/api/runs/{old_id}/restart")
        assert r.status_code == 202
        new_id = r.json()["run_id"]
        assert new_id != old_id
        assert client.get(f"/api/runs/{old_id}").status_code == 404
    finally:
        # The replacement run uses the same cooperative stub: stop it outright.
        with contextlib.suppress(NameError):
            client.post(f"/api/runs/{new_id}/stop")
    assert _wait_for_status(client, new_id, "cancelled") == "cancelled"


def test_restart_refuses_stuck_worker_without_deleting(tmp_path, monkeypatch):
    import time as _time

    from app.services.runs import RunningError, RunStore

    monkeypatch.setenv("RUNS_DIR", str(tmp_path / "runs3"))
    get_settings.cache_clear()
    store = RunStore(get_settings())
    try:
        def stuck(requirement, out_dir, **kw):
            _time.sleep(5)
            from schemas.messages import GraphState

            return GraphState(requirement=requirement, run_id="s", status="failed")

        monkeypatch.setattr("app.services.runs.run_team", stuck)
        run = store.create("Build a REST API for a library that lends books.")
        store.submit(run)
        _time.sleep(0.5)
        with pytest.raises(RunningError):
            store.restart(run.run_id, grace=1)
        assert store.get(run.run_id) is not None, "nothing may be deleted on timeout"
    finally:
        store.shutdown()
        get_settings.cache_clear()


def _partial_run_team(requirement, out_dir, on_event=None, run_id=None, **kw):
    """Stand-in worker: writes mid-flight artifacts, then reports cancelled."""
    import json as _json
    from pathlib import Path as _Path

    from schemas.messages import GraphState, UserStories

    out = _Path(out_dir)
    (out / "code").mkdir(parents=True, exist_ok=True)
    (out / "tests").mkdir(parents=True, exist_ok=True)
    (out / "stories.json").write_text(
        UserStories(
            stories=[{"id": "US1", "title": "List books", "acceptance": ["GET /books 200"]}]
        ).model_dump_json()
    )
    (out / "spec.yaml").write_text("openapi: 3.1.0\ninfo: {title: Library}\npaths: {}\n")
    (out / "plan.json").write_text(
        _json.dumps(
            {"kind": "api", "entrypoint": "app.py", "files": ["app.py"],
             "public_api": [], "contract_text": ""}
        )
    )
    (out / "code" / "app.py").write_text("x = 1\n")
    (out / "tests" / "test_app.py").write_text("import app\n\ndef test_x():\n    assert True\n")
    _partial_run_team.seen.append(kw.get("resume"))
    return GraphState(
        requirement=requirement, run_id=run_id or "partial",
        status="cancelled", error="stopped by user",
    )


_partial_run_team.seen = []


def test_resume_unknown_run_returns_404(client):
    assert client.post("/api/runs/no-such-run/resume").status_code == 404


def test_resume_accepted_run_conflicts(client, fake_pipeline):
    run_id = client.post("/api/runs", json={"requirement": REQUIREMENT}).json()["run_id"]
    _wait_for(client, run_id)
    r = client.post(f"/api/runs/{run_id}/resume")
    assert r.status_code == 409


def test_resume_running_run_conflicts(client, monkeypatch):
    from app.services import runs as runs_mod

    monkeypatch.setattr(runs_mod, "run_team", _slow_cooperative_run_team)
    run_id = client.post("/api/runs", json={"requirement": REQUIREMENT}).json()["run_id"]
    _wait_for_status(client, run_id, "running")
    try:
        assert client.post(f"/api/runs/{run_id}/resume").status_code == 409
    finally:
        client.post(f"/api/runs/{run_id}/stop")
        _wait_for_status(client, run_id, "cancelled")


def test_resume_cancelled_run_continues_from_artifacts(client, monkeypatch):
    from app.services import runs as runs_mod

    _partial_run_team.seen.clear()
    monkeypatch.setattr(runs_mod, "run_team", _partial_run_team)
    old_id = client.post("/api/runs", json={"requirement": REQUIREMENT}).json()["run_id"]
    assert _wait_for_status(client, old_id, "cancelled") == "cancelled"
    r = client.post(f"/api/runs/{old_id}/resume")
    assert r.status_code == 202, r.text
    body = r.json()
    new_id = body["run_id"]
    assert new_id != old_id
    assert body["resumed_from"] == old_id
    assert body["requirement"] == REQUIREMENT
    assert _wait_for_status(client, new_id, "cancelled") == "cancelled"
    # The old run is preserved as history; the worker got the restored state.
    assert client.get(f"/api/runs/{old_id}").status_code == 200
    resumed_states = [s for s in _partial_run_team.seen if s is not None]
    assert len(resumed_states) == 1, "exactly the resumed run carries a state"
    assert resumed_states[0].stories is not None
    assert resumed_states[0].spec is not None
    assert resumed_states[0].code is not None
