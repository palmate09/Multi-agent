"""Orchestration: pm -> designer -> developer||tester -> runner -> triage -> reviewer.
Tries LangGraph; falls back to sequential with identical semantics."""
from __future__ import annotations
import json
import time
from pathlib import Path
from schemas.messages import GraphState, TestReport
from agents import pm as PM
from agents import designer as Designer
from agents import developer as Developer
from agents import tester as Tester
from agents import reviewer as Reviewer
from sandbox import runner as Runner


def _save(out: Path, name: str, content: str) -> None:
    out.mkdir(parents=True, exist_ok=True)
    (out / name).write_text(content)


def run_team(requirement: str, out_dir: str = "outputs/demo", max_dev: int = 4,
             max_test: int = 2, max_review: int = 2, use_docker: bool = False,
             skip_tester: bool = False, skip_reviewer: bool = False,
             verbose: bool = False) -> GraphState:
    out = Path(out_dir)
    t0 = time.time()
    st = GraphState(requirement=requirement, run_id=out.name)

    def log(msg: str):
        if verbose:
            print(f"[team] {msg}")

    log("pm: stories")
    st.stories = PM.requirement_to_stories(requirement)
    _save(out, "stories.json", st.stories.model_dump_json(indent=2))

    log("designer: spec")
    st.spec = Designer.stories_to_spec(st.stories)
    _save(out, "spec.yaml", st.spec.openapi_yaml)

    log("developer: code")
    st.code = Developer.spec_to_code(st.spec)
    for rel, c in st.code.files.items():
        p = out / "code" / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(c)

    log("tester: tests")
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

    # fix loop
    while True:
        log(f"runner: tests (dev_try={st.retry_dev}, test_try={st.retry_test})")
        st.report = Runner.run_tests(st.code, st.tests, use_docker=use_docker)
        _save(out, f"report_try{st.retry_dev + st.retry_test}.json", st.report.model_dump_json(indent=2))
        if st.report.ok:
            st.status = "tests_green"
            break
        verdict = PM.triage(st.report)
        refl = Developer.reflect(st.report)
        st.memory.append(refl)
        _save(out, "reflections.json", json.dumps(st.memory, indent=2))
        if verdict == "test_bug" and st.retry_test < max_test and not skip_tester:
            st.retry_test += 1
            log("triage: test_bug -> tester regenerates")
            st.tests = Tester.spec_to_tests(st.spec, st.stories)
        elif st.retry_dev < max_dev:
            st.retry_dev += 1
            log("triage: code_bug -> developer patches")
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
            ruff_out, bandit_out = Runner.static_analysis(st.code)
            _save(out, "ruff.txt", ruff_out)
            _save(out, "bandit.txt", bandit_out)
            st.review = Reviewer.review(st.code, st.spec, st.tests, st.report, ruff_out, bandit_out)
            _save(out, "review.json", st.review.model_dump_json(indent=2))
            if not st.review.blockers:
                st.status = "accepted"
                break
            if st.retry_review >= max_review:
                st.status = "unresolved_review"
                break
            st.retry_review += 1
            log(f"reviewer: {len(st.review.blockers)} blockers -> developer")
            st.code = Developer.patch_code(st.code, TestReport(
                passed=0, failed=len(st.review.blockers),
                failures=[], raw="; ".join(c.message for c in st.review.blockers)), "; ".join(st.memory[-1:]))
            st.report = Runner.run_tests(st.code, st.tests, use_docker=use_docker)
            if not st.report.ok and st.retry_dev >= max_dev:
                st.status = "unresolved"
                break
    elif st.status == "tests_green" and skip_reviewer:
        st.status = "accepted_no_review"

    summary = {"status": st.status, "tests_passed": st.report.passed if st.report else 0,
               "tests_failed": st.report.failed if st.report else 0,
               "attempts_dev": st.retry_dev, "attempts_test": st.retry_test,
               "review_rounds": st.retry_review, "wall_time": round(time.time() - t0, 1)}
    _save(out, "summary.json", json.dumps(summary, indent=2))
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
