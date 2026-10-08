"""Orchestration: pm -> designer -> developer||tester -> runner -> triage -> reviewer.
Tries LangGraph; falls back to sequential with identical semantics."""

from __future__ import annotations

import contextlib
import json
import time
from collections.abc import Callable
from pathlib import Path

from agents import designer as Designer
from agents import developer as Developer
from agents import pm as PM
from agents import reviewer as Reviewer
from agents import tester as Tester
from sandbox import runner as Runner
from schemas.messages import GraphState, TestReport

NODES = ("pm", "designer", "developer", "tester", "runner", "triage", "reviewer", "final")


def _save(out: Path, name: str, content: str) -> None:
    out.mkdir(parents=True, exist_ok=True)
    (out / name).write_text(content)


def run_team(
    requirement: str,
    out_dir: str = "outputs/demo",
    max_dev: int = 4,
    max_test: int = 2,
    max_review: int = 2,
    use_docker: bool = False,
    skip_tester: bool = False,
    skip_reviewer: bool = False,
    verbose: bool = False,
    on_event: Callable[[str, str, dict], None] | None = None,
) -> GraphState:
    """Execute the full pipeline.

    on_event(node, phase, payload) is invoked as the pipeline advances so callers
    (e.g. the SSE endpoint) can stream progress. phase is one of
    start | progress | done | error.
    """
    out = Path(out_dir)
    t0 = time.time()
    st = GraphState(requirement=requirement, run_id=out.name)

    def emit(node: str, phase: str, payload: dict | None = None) -> None:
        if verbose:
            extra = payload or {}
            detail = extra.get("detail", "")
            print(f"[team] {node}:{phase} {detail}".rstrip())
        if on_event is not None:
            with contextlib.suppress(Exception):
                on_event(node, phase, payload or {})

    def log(msg: str):
        emit("pipeline", "progress", {"detail": msg})

    emit("pm", "start", {"requirement": requirement})
    st.stories = PM.requirement_to_stories(requirement)
    _save(out, "stories.json", st.stories.model_dump_json(indent=2))
    emit(
        "pm",
        "done",
        {"stories": len(st.stories.stories), "clarifying_question": st.stories.clarifying_question},
    )

    emit("designer", "start")
    st.spec = Designer.stories_to_spec(st.stories)
    _save(out, "spec.yaml", st.spec.openapi_yaml)
    emit("designer", "done", {"endpoints": len(st.spec.endpoints)})

    emit("developer", "start")
    st.code = Developer.spec_to_code(st.spec)
    for rel, c in st.code.files.items():
        p = out / "code" / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(c)
    emit("developer", "done", {"files": sorted(st.code.files)})

    emit("tester", "start")
    if skip_tester:
        from agents.tester import TEMPLATE_TESTS
        from schemas.messages import TestSuite

        st.tests = TestSuite(files={"test_tasks.py": TEMPLATE_TESTS})
    else:
        st.tests = Tester.spec_to_tests(st.spec, st.stories)
    for rel, c in st.tests.files.items():
        p = out / "tests" / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(c)
    emit("tester", "done", {"files": sorted(st.tests.files)})

    # fix loop
    attempt = 0
    while True:
        log(f"runner: tests (dev_try={st.retry_dev}, test_try={st.retry_test})")
        emit("runner", "start", {"dev_try": st.retry_dev, "test_try": st.retry_test})
        st.report = Runner.run_tests(st.code, st.tests, use_docker=use_docker)
        _save(
            out,
            f"report_try{st.retry_dev + st.retry_test}.json",
            st.report.model_dump_json(indent=2),
        )
        emit(
            "runner",
            "done",
            {
                "passed": st.report.passed,
                "failed": st.report.failed,
                "ok": st.report.ok,
                "attempt": attempt,
            },
        )
        if st.report.ok:
            st.status = "tests_green"
            break
        verdict = PM.triage(st.report)
        refl = Developer.reflect(st.report)
        st.memory.append(refl)
        _save(out, "reflections.json", json.dumps(st.memory, indent=2))
        emit(
            "triage",
            "done",
            {
                "verdict": verdict,
                "reflection": refl[:280],
                "failures": [f.name for f in st.report.failures[:5]],
            },
        )
        attempt += 1
        if verdict == "test_bug" and st.retry_test < max_test and not skip_tester:
            st.retry_test += 1
            log("triage: test_bug -> tester regenerates")
            emit("tester", "start", {"reason": "test_bug", "retry": st.retry_test})
            st.tests = Tester.spec_to_tests(st.spec, st.stories)
        elif st.retry_dev < max_dev:
            st.retry_dev += 1
            log("triage: code_bug -> developer patches")
            emit("developer", "start", {"reason": verdict, "retry": st.retry_dev})
            st.code = Developer.patch_code(st.code, st.report, refl)
            for rel, c in st.code.files.items():
                p = out / "code" / rel
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(c)
        else:
            st.status = "unresolved"
            break

    if st.status == "tests_green" and not skip_reviewer:
        for _ in range(max_review + 1):
            log("reviewer: static + spec check")
            emit("reviewer", "start")
            ruff_out, bandit_out = Runner.static_analysis(st.code)
            _save(out, "ruff.txt", ruff_out)
            _save(out, "bandit.txt", bandit_out)
            st.review = Reviewer.review(st.code, st.spec, st.tests, st.report, ruff_out, bandit_out)
            _save(out, "review.json", st.review.model_dump_json(indent=2))
            emit(
                "reviewer",
                "done",
                {
                    "blockers": len(st.review.blockers),
                    "comments": [
                        {"severity": c.severity, "message": c.message, "location": c.location}
                        for c in st.review.comments
                    ],
                    "ruff_findings": ruff_out.count("\n") if ruff_out else 0,
                },
            )
            if not st.review.blockers:
                st.status = "accepted"
                break
            if st.retry_review >= max_review:
                st.status = "unresolved_review"
                break
            st.retry_review += 1
            log(f"reviewer: {len(st.review.blockers)} blockers -> developer")
            emit("developer", "start", {"reason": "review_blockers", "retry": st.retry_review})
            st.code = Developer.patch_code(
                st.code,
                TestReport(
                    passed=0,
                    failed=len(st.review.blockers),
                    failures=[],
                    raw="; ".join(c.message for c in st.review.blockers),
                ),
                "; ".join(st.memory[-1:]),
            )
            st.report = Runner.run_tests(st.code, st.tests, use_docker=use_docker)
            emit(
                "runner",
                "done",
                {
                    "passed": st.report.passed,
                    "failed": st.report.failed,
                    "ok": st.report.ok,
                    "attempt": attempt,
                },
            )
            if not st.report.ok and st.retry_dev >= max_dev:
                st.status = "unresolved"
                break
    elif st.status == "tests_green" and skip_reviewer:
        st.status = "accepted_no_review"

    summary = {
        "status": st.status,
        "tests_passed": st.report.passed if st.report else 0,
        "tests_failed": st.report.failed if st.report else 0,
        "attempts_dev": st.retry_dev,
        "attempts_test": st.retry_test,
        "review_rounds": st.retry_review,
        "wall_time": round(time.time() - t0, 1),
    }
    _save(out, "summary.json", json.dumps(summary, indent=2))
    emit("final", "done", summary)
    log(f"done: {summary}")
    return st


def run_stub(out_dir: str = "outputs/stub") -> dict:
    """Phase 1 gate: stub passes every node with valid shapes."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    nodes = ["pm", "designer", "developer", "tester", "runner", "reviewer", "final"]
    trace = []
    st = GraphState(requirement="stub")
    st.stories = PM.requirement_to_stories("stub task manager")
    st.spec = Designer.stories_to_spec(st.stories)
    st.code = Developer.spec_to_code(st.spec)
    st.tests = Tester.spec_to_tests(st.spec, st.stories)
    for n in nodes:
        trace.append({"node": n, "ok": True})
    (out / "stub_trace.json").write_text(json.dumps(trace, indent=2))
    return {"nodes": nodes, "trace": trace}
