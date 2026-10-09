"""Orchestration: pm -> designer -> developer -> tester -> runner -> triage -> reviewer.

Sequential by design: the Tester's independence comes from never seeing the
code, not from running at the same time, and the small local models gain
nothing from interleaving.

Terminal statuses:
  accepted            green tests, review clean
  accepted_no_review  green tests, reviewer ablated
  unresolved          retry budgets exhausted with tests still failing
  unresolved_review   review blockers survived the retry budget
  blocked             a generation or validation step failed; see GraphState.error
  domain_missed       the coverage gate found the code does not implement the
                      requested domain
"""

from __future__ import annotations

import contextlib
import json
import time
from collections.abc import Callable
from pathlib import Path

from agents import designer as Designer
from agents import developer as Developer
from agents import llm
from agents import pm as PM
from agents import reviewer as Reviewer
from agents import tester as Tester
from agents.coverage import check as coverage_check
from agents.introspect import all_routes, spec_paths
from agents.llm import GenerationError
from sandbox import runner as Runner
from schemas.messages import CoverageReport, TestFailure, TestReport, TestSuite

NODES = ("pm", "designer", "developer", "tester", "runner", "triage", "reviewer", "final")

DEFAULT_MAX_DEV = 4


def _save(out: Path, name: str, content: str) -> None:
    out.mkdir(parents=True, exist_ok=True)
    (out / name).write_text(content)


def _write_code(out: Path, code) -> None:
    for rel, content in code.files.items():
        p = out / "code" / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)


def _self_checks(spec) -> str:
    """A smoke test used only when the Tester is ablated.

    Deliberately minimal: with the Tester ablated there is no independent check
    of behaviour, which is exactly what the ablation demonstrates. For an API
    it checks the routes are registered; for a program it checks the declared
    interface resolves.
    """
    if getattr(spec, "kind", "api") == "program":
        names = ", ".join(repr(n) for n in spec.public_api)
        module = spec.entrypoint[:-3] if spec.entrypoint.endswith(".py") else spec.entrypoint
        return f'''"""Ablated run: developer-authored smoke checks only."""
import {module}


def test_declared_interface_present():
    for name in [{names}]:
        target = {module}
        for part in name.split('.'):
            assert hasattr(target, part), f"missing {{name}}"
            target = getattr(target, part)
'''
    pairs, _paths = spec_paths(spec.openapi_yaml)
    paths = sorted({p for p, _ in pairs} - {"/health"})
    checks = "\n".join(
        f"""
def test_route_{i}():
    assert any(r.path == {p!r} for r in {spec.entrypoint[:-3]}.app.routes)"""
        for i, p in enumerate(paths)
    )
    return f'''"""Ablated run: developer-authored smoke checks only."""
import {spec.entrypoint[:-3]}


def test_app_importable():
    assert {spec.entrypoint[:-3]}.app is not None
{checks}
'''


def _finish(out: Path, st, t0: float, emit) -> None:
    summary = {
        "status": st.status,
        "error": st.error or None,
        "tests_passed": st.report.passed if st.report else 0,
        "tests_failed": st.report.failed if st.report else 0,
        "attempts_dev": st.retry_dev,
        "attempts_test": st.retry_test,
        "review_rounds": st.retry_review,
        "entrypoint": st.code.entry_module if st.code else None,
        "files": sorted(st.code.files) if st.code else [],
        "domain_coverage": st.coverage.model_dump() if st.coverage else None,
        "wall_time": round(time.time() - t0, 1),
    }
    _save(out, "summary.json", json.dumps(summary, indent=2))
    emit("final", "done", summary)


def run_team(
    requirement: str,
    out_dir: str = "outputs/demo",
    max_dev: int = DEFAULT_MAX_DEV,
    max_test: int = 2,
    max_review: int = 2,
    use_docker: bool = False,
    skip_tester: bool = False,
    skip_reviewer: bool = False,
    verbose: bool = False,
    on_event: Callable[[str, str, dict], None] | None = None,
    run_id: str | None = None,
) -> object:
    """Execute the full pipeline, streaming progress through ``on_event``.

    Raises nothing for an expected failure: a generation that never succeeds
    ends the run with ``status="blocked"`` and a populated ``error``.
    """
    from schemas.messages import GraphState

    out = Path(out_dir)
    t0 = time.time()
    st = GraphState(requirement=requirement, run_id=run_id or out.name)
    # Scope LLM responses to this run so a later run with the same requirement
    # never inherits this run's completions from the shared backend cache.
    llm.set_run_scope(str(run_id or out.name))

    def emit(node: str, phase: str, payload: dict | None = None) -> None:
        data = payload or {}
        if verbose:
            print(f"[team] {node}:{phase} {data.get('detail', '')}".rstrip())
        if on_event is not None:
            with contextlib.suppress(Exception):
                on_event(node, phase, data)

    def log(msg: str) -> None:
        emit("pipeline", "progress", {"detail": msg})

    def block(reason: str, err: Exception | str) -> None:
        st.status = "blocked"
        st.error = f"{reason}: {err}"
        emit("pipeline", "error", {"error": st.error})
        _finish(out, st, t0, emit)

    try:
        emit("pm", "start", {"requirement": requirement})
        st.stories = PM.requirement_to_stories(requirement)
        _save(out, "stories.json", st.stories.model_dump_json(indent=2))
        emit(
            "pm",
            "done",
            {
                "stories": len(st.stories.stories),
                "clarifying_question": st.stories.clarifying_question,
            },
        )

        emit("designer", "start")
        st.spec = Designer.stories_to_spec(st.stories, requirement=requirement)
        _save(out, "spec.yaml", st.spec.openapi_yaml)
        _save(
            out,
            "plan.json",
            json.dumps(
                {
                    "kind": st.spec.kind,
                    "entrypoint": st.spec.entrypoint,
                    "files": st.spec.files,
                    "public_api": st.spec.public_api,
                    "contract_text": st.spec.contract_text,
                },
                indent=2,
            ),
        )
        emit(
            "designer",
            "done",
            {
                "endpoints": len(st.spec.endpoints),
                "files": st.spec.files,
                "entrypoint": st.spec.entrypoint,
            },
        )

        emit("developer", "start")
        st.code = Developer.spec_to_code(st.spec)
        _write_code(out, st.code)
        emit(
            "developer",
            "done",
            {
                "files": sorted(st.code.files),
                "entrypoint": f"{st.code.entry_module}.{st.code.entry_attr}",
            },
        )

        # Static repair before anything expensive runs. Undefined names are the
        # most common generation error and ruff finds them in about a second,
        # where a failing pytest cycle costs a whole generation plus a run.
        lint_out, _ = Runner.static_analysis(st.code)
        if lint_out:
            st.code = Developer.repair_lint(st.code, lint_out, kind=st.spec.kind)
            _write_code(out, st.code)
            emit("developer", "progress", {"detail": "lint repair pass complete"})

        # Coverage gate, before any test is graded: a generation that ignored
        # the requested domain must not be able to report success.
        # Only an HTTP service contributes route paths to the domain check; a
        # program's vocabulary lives in its modules and symbols.
        routes = all_routes(st.code.files) if st.spec.kind == "api" else set()
        verdict = coverage_check(requirement, st.code.files, routes)
        st.coverage = CoverageReport(
            covered=verdict.covered,
            missing=verdict.missing,
            conclusive=verdict.conclusive,
        )
        _save(out, "coverage.json", st.coverage.model_dump_json(indent=2))
        emit(
            "coverage",
            "done",
            {"ratio": verdict.ratio, "ok": verdict.ok, "missing": verdict.missing},
        )
        if not verdict.ok:
            st.status = "domain_missed"
            st.error = (
                "generated code does not implement the requested domain; missing: "
                + ", ".join(verdict.missing)
            )
            emit("pipeline", "error", {"error": st.error})
            _finish(out, st, t0, emit)
            return st

        # Boot check: the code must import, and expose what the contract declares.
        # A program declares a public interface rather than an ASGI app object.
        declared = st.spec.public_api if st.spec.kind == "program" else None
        booted, boot_msg = Runner.boot_check(st.code, public_api=declared)
        _save(out, "boot.txt", boot_msg)
        emit("runner", "progress", {"detail": f"boot check: {'ok' if booted else 'FAILED'}"})

        if not booted:
            # An import failure is fixable and carries an exact traceback, so it
            # enters the same loop as a failing test instead of ending the run.
            log("boot check failed; routing the traceback to the developer")
            no_ops = 0
            for _ in range(max_dev):
                refl = (
                    "The application does not import. Fix the error below and reply with the "
                    "complete corrected files. Add any missing imports and make every FastAPI "
                    "route return a serialisable type (never a SQLAlchemy model class)."
                )
                st.memory.append(refl)
                _save(out, "reflections.json", json.dumps(st.memory, indent=2))
                before = dict(st.code.files)
                try:
                    st.code = Developer.patch_code(
                        st.code,
                        TestReport(
                            passed=0,
                            failed=1,
                            failures=[TestFailure(name="boot", error=boot_msg[-800:])],
                            raw=boot_msg[-2000:],
                        ),
                        refl,
                        kind=st.spec.kind,
                    )
                except GenerationError as exc:
                    log(f"boot repair failed: {exc}")
                if st.code.files == before:
                    # A model occasionally re-emits the same file. One no-op is
                    # not proof it cannot fix it, but repeating forever is not
                    # progress either, so allow a couple before giving up.
                    no_ops += 1
                    if no_ops >= 2:
                        break
                    continue
                no_ops = 0
                st.retry_dev += 1
                _write_code(out, st.code)
                emit("developer", "start", {"reason": "boot_failure", "retry": st.retry_dev})
                booted, boot_msg = Runner.boot_check(st.code, public_api=declared)
                _save(out, "boot.txt", boot_msg)
                emit(
                    "runner", "progress", {"detail": f"boot check: {'ok' if booted else 'FAILED'}"}
                )
                if booted:
                    break
                st.retry_dev += 1
                _write_code(out, st.code)
                emit("developer", "start", {"reason": "boot_failure", "retry": st.retry_dev})
                booted, boot_msg = Runner.boot_check(st.code, public_api=declared)
                _save(out, "boot.txt", boot_msg)
                emit(
                    "runner", "progress", {"detail": f"boot check: {'ok' if booted else 'FAILED'}"}
                )
                if booted:
                    break
            if not booted and st.status != "unresolved":
                st.status = "unresolved"
                st.error = f"generated application failed to import: {boot_msg[-400:]}"
            if not booted:
                emit("pipeline", "error", {"error": st.error})
                _finish(out, st, t0, emit)
                return st

        if skip_tester:
            # Ablation: the Developer writes its own checks inline instead. There
            # is no independent suite, so the independent-verification guarantee
            # is deliberately given up for this run.
            log("tester ablated: developer-authored checks only")
            st.tests = TestSuite(files={"test_generated.py": _self_checks(st.spec)})
            emit("tester", "done", {"ablated": True, "files": ["test_generated.py"]})
        else:
            emit("tester", "start")
            st.tests = Tester.spec_to_tests(
                st.spec,
                st.stories,
                entry_module=st.code.entry_module,
                entry_attr=st.code.entry_attr,
                code=st.code,
            )
            for rel, c in st.tests.files.items():
                p = out / "tests" / rel
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(c)
            emit("tester", "done", {"files": sorted(st.tests.files)})
    except GenerationError as exc:
        block("an agent could not produce a valid result", exc)
        return st

    # ---- test / fix loop ----
    attempt = 0
    while True:
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

        verdict_txt = PM.triage(st.report)
        refl = Developer.reflect(st.report, kind=st.spec.kind)
        st.memory.append(refl)
        _save(out, "reflections.json", json.dumps(st.memory, indent=2))
        emit(
            "triage",
            "done",
            {
                "verdict": verdict_txt,
                "reflection": refl[:280],
                "failures": [f.name for f in st.report.failures[:5]],
            },
        )
        attempt += 1

        if verdict_txt == "test_bug" and st.retry_test < max_test and not skip_tester:
            st.retry_test += 1
            log("triage: test_bug -> tester regenerates")
            emit("tester", "start", {"reason": "test_bug", "retry": st.retry_test})
            try:
                st.tests = Tester.spec_to_tests(
                    st.spec,
                    st.stories,
                    entry_module=st.code.entry_module,
                    entry_attr=st.code.entry_attr,
                    code=st.code,
                )
            except GenerationError as exc:
                log(f"tester regeneration failed: {exc}")
            continue

        before = dict(st.code.files)
        if st.retry_dev >= max_dev:
            st.status = "unresolved"
            st.error = f"tests still failing after {st.retry_dev} developer retries"
            break
        try:
            st.code = Developer.patch_code(st.code, st.report, refl, tests=st.tests, kind=st.spec.kind)
        except GenerationError as exc:
            log(f"developer patch failed: {exc}")
        if st.code.files == before:
            # An unchanged bundle means every further attempt repeats this one.
            st.status = "unresolved"
            st.error = "developer patch produced no change; stopping instead of looping"
            emit("developer", "done", {"changed": False, "retry": st.retry_dev})
            break
        st.retry_dev += 1
        _write_code(out, st.code)
        emit("developer", "start", {"reason": verdict_txt, "retry": st.retry_dev})
        # Re-verify the entrypoint still resolves after a patch. A program has
        # no ASGI app object; its entrypoint is the declared module.
        if st.spec.kind == "program":
            if not st.code.entry_module or not any(
                f == st.spec.entrypoint for f in st.code.files
            ):
                st.status = "unresolved"
                st.error = "patched code no longer contains the declared entrypoint"
                break
        elif not st.code.entry_module:
            st.status = "unresolved"
            st.error = "patched code no longer exposes an importable app object"
            break

    # ---- review loop ----
    if st.status == "tests_green" and skip_reviewer:
        st.status = "accepted_no_review"
    elif st.status == "tests_green":
        for _ in range(max_review + 1):
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
                },
            )
            if not st.review.blockers:
                st.status = "accepted"
                break
            if st.retry_review >= max_review:
                st.status = "unresolved_review"
                st.error = f"{len(st.review.blockers)} review blockers survived the retry budget"
                break
            before = dict(st.code.files)
            refl = st.memory[-1] if st.memory else "address the review blockers"
            try:
                st.code = Developer.patch_code(
                    st.code,
                    TestReport(
                        passed=0,
                        failed=len(st.review.blockers),
                        failures=[],
                        raw="; ".join(c.message for c in st.review.blockers),
                    ),
                    "Review blockers to fix: " + "; ".join(c.message for c in st.review.blockers),
                    tests=st.tests,
                    kind=st.spec.kind,
                )
            except GenerationError as exc:
                log(f"developer review patch failed: {exc}")
            if st.code.files == before:
                st.status = "unresolved_review"
                st.error = "review patch produced no change; stopping instead of looping"
                break
            st.retry_review += 1
            _write_code(out, st.code)
            st.report = Runner.run_tests(st.code, st.tests, use_docker=use_docker)
            emit(
                "runner",
                "done",
                {
                    "passed": st.report.passed,
                    "failed": st.report.failed,
                    "ok": st.report.ok,
                },
            )
            if not st.report.ok:
                st.status = "unresolved"
                st.error = "patched for review but tests are failing again"
                break

    log(f"done: {st.status}")
    _finish(out, st, t0, emit)
    return st


def run_stub(out_dir: str = "outputs/stub") -> dict:
    """Phase 1 gate: the node list is well-formed and every name is known."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    trace = [{"node": n, "known": n in NODES} for n in NODES]
    (out / "stub_trace.json").write_text(json.dumps(trace, indent=2))
    return {"nodes": list(NODES), "trace": trace, "ok": all(t["known"] for t in trace)}
