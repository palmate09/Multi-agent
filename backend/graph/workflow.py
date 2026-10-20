"""Orchestration: pm -> reasoner -> designer -> developer -> tester -> runner -> triage -> reviewer.

Sequential by design: the Tester's independence comes from never seeing the
code, not from running at the same time, and the small local models gain
nothing from interleaving.

The UI half (``want_ui=True``) runs *after* the backend tests are green and
before the reviewer:

    ... -> runner -> triage -> developer -> ui_designer -> ui_developer
        -> ui_gate -> reviewer

Sequential rather than parallel on purpose: the UI binds to a known-good API,
so a backend that is still being repaired cannot produce a UI written against
endpoints that do not exist yet. The UI verdict is advisory — it is reported,
and it never changes the terminal status.

Terminal statuses:
  accepted            green tests, review clean
  accepted_no_review  green tests, reviewer ablated
  unresolved          retry budgets exhausted with tests still failing
  unresolved_review   review blockers survived the retry budget
  blocked             a generation or validation step failed; see GraphState.error
  domain_missed       the coverage gate found the code does not implement the
                      requested domain
  cancelled           stopped by the user via the Stop control; partial
                      artifacts remain on disk for inspection or resume
"""

from __future__ import annotations

import contextlib
import json
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from agents import designer as Designer
from agents import developer as Developer
from agents import llm
from agents import pm as PM
from agents import reasoner as Reasoner
from agents import reviewer as Reviewer
from agents import tester as Tester
from agents import ui as UI
from agents.coverage import check as coverage_check
from agents.introspect import all_routes, find_app_object, spec_paths
from agents.llm import GenerationError
from sandbox import runner as Runner
from schemas.messages import (
    ApiSpec,
    CodeBundle,
    CoverageReport,
    GraphState,
    Plan,
    TestFailure,
    TestReport,
    TestSuite,
    UiCheck,
    UiReport,
    UiSpec,
    UserStories,
)

NODES = (
    "pm",
    "reasoner",
    "designer",
    "developer",
    "coverage",
    "tester",
    "runner",
    "triage",
    "ui_designer",
    "ui_developer",
    "ui_gate",
    "reviewer",
    "final",
)

DEFAULT_MAX_DEV = 4


def _save(out: Path, name: str, content: str) -> None:
    out.mkdir(parents=True, exist_ok=True)
    (out / name).write_text(content)


def _write_code(out: Path, code) -> None:
    for rel, content in code.files.items():
        p = out / "code" / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)


def _write_ui_code(out: Path, code, into_static: bool = False) -> None:
    """Write the interface files.

    For a full-stack application they go into ``code/static/``, because the
    generated application serves them itself: the sandbox runs one process,
    and that process is the product. For a service with a separate preview page
    they stay under ``ui/`` so the preview can copy them verbatim — kept out
    of ``code/`` on purpose, since the runner only ever imports ``.py`` from
    there.
    """
    root = out / "code" / "static" if into_static else out / "ui"
    for rel, content in code.files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)


def reconstruct_state(run_dir: str | Path) -> GraphState:
    """Rebuild pipeline state from a previous run's saved artifacts.

    Every artifact is optional: a cancelled run may have stopped anywhere, so
    a missing file simply leaves that phase (and everything after it) to be
    re-executed. Never raises on malformed files — they are treated as absent.
    Always returns a state, possibly empty (resume then behaves as restart).
    """
    root = Path(run_dir)
    st = GraphState()

    def _json(name: str):
        try:
            return json.loads((root / name).read_text())
        except (OSError, json.JSONDecodeError):
            return None

    stories = _json("stories.json")
    if isinstance(stories, dict):
        with contextlib.suppress(Exception):
            st.stories = UserStories.model_validate(stories)

    reasoning = _json("reasoning.json")
    if isinstance(reasoning, dict):
        with contextlib.suppress(Exception):
            st.plan = Plan.model_validate(reasoning)

    spec_text = None
    try:
        spec_text = (root / "spec.yaml").read_text()
    except OSError:
        spec_text = None
    plan_meta = _json("plan.json") or {}
    if spec_text:
        with contextlib.suppress(Exception):
            st.spec = ApiSpec(
                openapi_yaml=spec_text,
                kind=plan_meta.get("kind", "api"),
                entrypoint=plan_meta.get("entrypoint", "main.py"),
                files=plan_meta.get("files", []),
                public_api=plan_meta.get("public_api", []),
                contract_text=plan_meta.get("contract_text", ""),
                has_ui=bool(plan_meta.get("has_ui", False)),
            )

    code_files: dict[str, str] = {}
    code_root = root / "code"
    if code_root.is_dir():
        for p in sorted(code_root.rglob("*")):
            if p.is_file():
                try:
                    code_files[str(p.relative_to(code_root))] = p.read_text()
                except OSError:
                    continue
    if code_files:
        module = (plan_meta.get("entrypoint", "main.py") or "main.py").replace(".py", "")
        entry_module, entry_attr = module, ""
        try:
            found = find_app_object(code_files)
        except Exception:
            found = None
        if found:
            entry_module, entry_attr = found
        elif (st.spec.kind if st.spec else "api") == "api":
            entry_attr = "app"
        with contextlib.suppress(Exception):
            st.code = CodeBundle(files=code_files, entry_module=entry_module, entry_attr=entry_attr)

    test_files: dict[str, str] = {}
    tests_root = root / "tests"
    if tests_root.is_dir():
        for p in sorted(tests_root.rglob("*")):
            if p.is_file() and p.suffix == ".py":
                try:
                    test_files[str(p.relative_to(tests_root))] = p.read_text()
                except OSError:
                    continue
    if test_files:
        st.tests = TestSuite(files=test_files)

    reports = sorted(root.glob("report_try*.json"))
    if reports:
        with contextlib.suppress(Exception):
            st.report = TestReport.model_validate(json.loads(reports[-1].read_text()))

    coverage = _json("coverage.json")
    if isinstance(coverage, dict):
        with contextlib.suppress(Exception):
            st.coverage = CoverageReport.model_validate(coverage)

    memory = _json("reflections.json")
    if isinstance(memory, list):
        st.memory = [str(m) for m in memory]

    # UI half. Without these three reads a resume silently regenerates (or
    # drops) a UI the previous run already produced, which is the exact data
    # loss reconstruct_state exists to prevent.
    ui_contract = _json("ui_contract.json")
    if isinstance(ui_contract, dict):
        with contextlib.suppress(Exception):
            st.ui_spec = UiSpec.model_validate(ui_contract)

    ui_files: dict[str, str] = {}
    # A full-stack application keeps its interface inside the app package
    # (``code/static/``), because the app serves it; a service with a separate
    # preview page keeps it in ``ui/``. Read whichever exists.
    for ui_root in (root / "ui", root / "code" / "static"):
        if not ui_root.is_dir():
            continue
        for p in sorted(ui_root.rglob("*")):
            if p.is_file():
                try:
                    ui_files[str(p.relative_to(ui_root))] = p.read_text()
                except OSError:
                    continue
    if ui_files:
        with contextlib.suppress(Exception):
            st.ui_code = CodeBundle(files=ui_files)

    ui_report = _json("ui_report.json")
    if isinstance(ui_report, dict):
        with contextlib.suppress(Exception):
            st.ui_report = UiReport.model_validate(ui_report)

    return st


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
    llm.set_cancel(None)
    summary = {
        "status": st.status,
        "error": st.error or None,
        # What the run produced: a full-stack app, a service or a program.
        "kind": getattr(st.spec, "kind", None),
        "tests_passed": st.report.passed if st.report else 0,
        "tests_failed": st.report.failed if st.report else 0,
        "attempts_dev": st.retry_dev,
        "attempts_test": st.retry_test,
        "review_rounds": st.retry_review,
        "entrypoint": st.code.entry_module if st.code else None,
        "files": sorted(st.code.files) if st.code else [],
        "domain_coverage": st.coverage.model_dump() if st.coverage else None,
        # Advisory only: the UI half never contributes to ``status``.
        "ui_requested": bool(getattr(st.spec, "has_ui", False)),
        "ui_files": sorted(st.ui_code.files) if st.ui_code else [],
        "ui_report": st.ui_report.model_dump() if st.ui_report else None,
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
    skip_reasoner: bool = False,
    want_ui: bool = False,
    verbose: bool = False,
    on_event: Callable[[str, str, dict], None] | None = None,
    run_id: str | None = None,
    cancel: threading.Event | None = None,
    resume: GraphState | None = None,
    followup: str = "",
    followup_of: str = "",
) -> object:
    """Execute the full pipeline, streaming progress through ``on_event``.

    Raises nothing for an expected failure: a generation that never succeeds
    ends the run with ``status="blocked"`` and a populated ``error``.

    ``want_ui`` adds the three UI nodes after the tests are green. It is off by
    default, and a UI failure never changes the terminal status: ``ui_gate``
    reports what it found and the run carries on to review.

    ``cancel`` is a cooperative stop signal (see ``RunStore.stop``): it is
    honoured at node boundaries, so stopping latency is bounded by one agent
    call or one sandbox run, never instant mid-call. A cancelled run ends with
    ``status="cancelled"``.

    ``resume`` continues a previous run from its saved artifacts (see
    ``reconstruct_state``): phases with artifacts are skipped, the coverage
    gate and boot check always re-run, retry budgets start fresh.

    ``followup`` makes this run a CHANGE to the app in ``resume``: the PM, the
    Designer and the Developer all take the previous artifacts as their base
    and apply the request, so the result is the same product with one thing
    different. The tests and the interface are regenerated from the updated
    contract, so nothing is left describing the old app.
    """
    out = Path(out_dir)
    t0 = time.time()
    st = GraphState(requirement=requirement, run_id=run_id or out.name)
    if resume is not None:
        # Adopt the reconstructed state under the new run's identity. Budgets
        # restart: the resumed run gets full dev/test/review retries, since it
        # may be continuing from a budget the old run exhausted.
        st = resume
        st.run_id = run_id or out.name
        st.status = "pending"
        st.error = ""
        st.retry_dev = 0
        st.retry_test = 0
        st.retry_review = 0
        if skip_reasoner:
            # Ablation wins over restored artifacts: a resumed ablated run
            # must behave like a fresh ablated run (designer gets no plan).
            st.plan = None
    # A follow-up evolves an app: the previous artifacts are the base it is
    # edited from, and the phases that would otherwise be skipped (the PM, the
    # Designer, the Developer, the Tester, the interface) run again against
    # that base.
    prev_stories = prev_spec = prev_code = prev_tests = None
    if followup.strip() and resume is not None:
        prev_stories, prev_spec, prev_code, prev_tests = (
            st.stories,
            st.spec,
            st.code,
            st.tests,
        )
        st.stories = None
        st.spec = None
        st.code = None
        st.tests = None
        st.ui_spec = None
        st.ui_code = None
        st.ui_report = None
        st.coverage = None
        st.review = None
    st.followup_prompt = followup
    st.followup_of = followup_of
    # Scope LLM responses to this run so a later run with the same requirement
    # never inherits this run's completions from the shared backend cache.
    llm.set_run_scope(str(run_id or out.name))
    # Arm the cooperative stop signal for this worker thread (None for
    # ad-hoc/CLI runs). Cleared in _finish so the scope never leaks into the
    # next run that reuses the thread.
    llm.set_cancel(cancel)

    def emit(node: str, phase: str, payload: dict | None = None) -> None:
        data = payload or {}
        if verbose:
            print(f"[team] {node}:{phase} {data.get('detail', '')}".rstrip())
        if on_event is not None:
            with contextlib.suppress(Exception):
                on_event(node, phase, data)

    def log(msg: str) -> None:
        emit("pipeline", "progress", {"detail": msg})

    def cancelled_now() -> bool:
        return cancel is not None and cancel.is_set()

    def finish_cancelled():
        """Terminal state for a user stop: partial artifacts stay on disk."""
        st.status = "cancelled"
        st.error = "stopped by user"
        emit("pipeline", "cancelled", {"detail": "stopped by user"})
        _finish(out, st, t0, emit)
        return st

    def block(reason: str, err: Exception | str) -> None:
        st.status = "blocked"
        st.error = f"{reason}: {err}"
        emit("pipeline", "error", {"error": st.error})
        _finish(out, st, t0, emit)

    try:
        if resume is not None:
            skipped = sorted(
                name
                for name, present in (
                    ("pm", st.stories is not None),
                    ("reasoner", st.plan is not None),
                    ("designer", st.spec is not None),
                    ("developer", st.code is not None),
                    ("tester", st.tests is not None),
                    ("ui_designer", st.ui_spec is not None),
                    ("ui_developer", st.ui_code is not None),
                )
                if present
            )
            emit(
                "pipeline",
                "resumed",
                {"detail": f"resuming with {len(skipped)} phases skipped", "skipped": skipped},
            )
        if st.stories is None:
            emit("pm", "start", {"requirement": requirement})
            if prev_stories is not None:
                st.stories = PM.update_stories(requirement, followup, prev_stories)
            else:
                st.stories = PM.requirement_to_stories(requirement)
            _save(out, "stories.json", st.stories.model_dump_json(indent=2))
            emit(
                "pm",
                "done",
                {
                    "stories": len(st.stories.stories),
                    "clarifying_question": st.stories.clarifying_question,
                    "followup": bool(followup.strip()),
                },
            )
        if cancelled_now():
            return finish_cancelled()

        # Plan-then-act: the Reasoner works the requirement out before a spec
        # exists, so a misread costs one advisory node instead of a contract, a
        # codebase and a test suite. Ablating it (skip_reasoner) restores the
        # old straight-to-design behaviour for comparison. A resumed run with a
        # restored plan skips straight to design.
        if st.plan is None:
            emit("reasoner", "start")
            if skip_reasoner:
                log("reasoner ablated: the designer gets no plan")
                emit("reasoner", "done", {"ablated": True})
            else:
                st.plan = Reasoner.plan(requirement, stories=st.stories)
                _save(out, "reasoning.json", st.plan.model_dump_json(indent=2))
                emit(
                    "reasoner",
                    "done",
                    {
                        "approach": (st.plan.approach or "")[:240],
                        "risks": len(st.plan.risks),
                        "edge_cases": len(st.plan.edge_cases),
                        "open_questions": len(st.plan.open_questions),
                        "structured": st.plan.structured,
                    },
                )
        if cancelled_now():
            return finish_cancelled()

        if st.spec is None:
            emit("designer", "start")
            if prev_spec is not None and st.stories is not None:
                st.spec = Designer.update_spec(
                    prev_spec, followup, st.stories, requirement=requirement
                )
            else:
                st.spec = Designer.stories_to_spec(
                    st.stories, requirement=requirement, plan=st.plan, want_ui=want_ui
                )
            _save(out, "spec.yaml", st.spec.openapi_yaml)
            emit(
                "designer",
                "done",
                {
                    "endpoints": len(st.spec.endpoints),
                    "files": st.spec.files,
                    "entrypoint": st.spec.entrypoint,
                },
            )
        # Recorded on the spec, not just in memory, so a resume reads it back
        # from plan.json and knows the UI is part of this run.
        if st.spec.kind == "fullstack":
            # A full-stack application always ships an interface: it is the
            # application's own landing page, served by the same process.
            st.spec.has_ui = True
        else:
            st.spec.has_ui = bool(want_ui and UI.is_ui_eligible(st.spec))
            if want_ui and not st.spec.has_ui:
                log("ui skipped: only an api-kind project with declared endpoints gets a UI")
                st.skip_ui = True
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
                    "has_ui": st.spec.has_ui,
                },
                indent=2,
            ),
        )
        if cancelled_now():
            return finish_cancelled()

        if st.code is None:
            emit("developer", "start")
            if prev_code is not None and st.spec is not None:
                st.code = Developer.apply_change(
                    prev_code, st.spec, followup, tests=prev_tests
                )
            else:
                st.code = Developer.spec_to_code(st.spec, plan=st.plan)
            _write_code(out, st.code)
            emit(
                "developer",
                "done",
                {
                    "files": sorted(st.code.files),
                    "entrypoint": f"{st.code.entry_module}.{st.code.entry_attr}",
                },
            )
        if cancelled_now():
            return finish_cancelled()

        # Static repair before anything expensive runs. Undefined names are the
        # most common generation error and ruff finds them in about a second,
        # where a failing pytest cycle costs a whole generation plus a run.
        lint_out, _ = Runner.static_analysis(st.code)
        if lint_out:
            st.code = Developer.repair_lint(st.code, lint_out, kind=st.spec.kind)
            _write_code(out, st.code)
            emit("developer", "progress", {"detail": "lint repair pass complete"})
            # A terminal event as well: the rail decides a stage is done from
            # its LAST event, and a lingering "progress" made a finished
            # developer look stopped (or unfinished) once the run ended.
            emit(
                "developer",
                "done",
                {"files": sorted(st.code.files), "lint_repaired": True},
            )
        if cancelled_now():
            return finish_cancelled()

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
        if cancelled_now():
            return finish_cancelled()
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
        # A full-stack application must also serve a page at "/". Importing
        # cleanly is not enough: without the mount, the person opening the
        # preview gets a 404 and the "full-stack app" is a bare API.
        web_root = st.spec.kind == "fullstack"
        booted, boot_msg = Runner.boot_check(st.code, public_api=declared, web_root=web_root)
        _save(out, "boot.txt", boot_msg)
        emit("runner", "progress", {"detail": f"boot check: {'ok' if booted else 'FAILED'}"})
        if cancelled_now():
            return finish_cancelled()

        if not booted:
            # An import failure is fixable and carries an exact traceback, so it
            # enters the same loop as a failing test instead of ending the run.
            log("boot check failed; routing the traceback to the developer")
            no_ops = 0
            # What the next repair request says about the last attempt.
            boot_note = ""
            for _ in range(max_dev):
                if cancelled_now():
                    return finish_cancelled()
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
                        spec=st.spec,
                        note=boot_note,
                    )
                except GenerationError as exc:
                    log(f"boot repair failed: {exc}")
                if st.code.files == before:
                    # A model occasionally re-emits the same file. One no-op is
                    # not proof it cannot fix it, but repeating forever is not
                    # progress either, so allow a couple before giving up --
                    # and ask differently the second time.
                    no_ops += 1
                    if no_ops >= 2:
                        break
                    boot_note = (
                        "Your previous reply reproduced the current code exactly, so the "
                        "import still fails. Make one concrete, minimal edit that fixes "
                        "the traceback above, and reply with the complete corrected file.\n"
                    )
                    continue
                no_ops = 0
                boot_note = ""
                st.retry_dev += 1
                _write_code(out, st.code)
                emit("developer", "start", {"reason": "boot_failure", "retry": st.retry_dev})
                booted, boot_msg = Runner.boot_check(
                    st.code, public_api=declared, web_root=web_root
                )
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
            # Boot repair succeeded. Without a terminal event the node's last
            # event stayed "start (retry #N boot_failure)", which the rail
            # renders as stopped — a repaired job looked abandoned.
            emit(
                "developer",
                "done",
                {
                    "files": sorted(st.code.files),
                    "repaired": "boot_failure",
                    "retries": st.retry_dev,
                },
            )

        if skip_tester:
            # Ablation: the Developer writes its own checks inline instead. There
            # is no independent suite, so the independent-verification guarantee
            # is deliberately given up for this run.
            log("tester ablated: developer-authored checks only")
            st.tests = TestSuite(files={"test_generated.py": _self_checks(st.spec)})
            emit("tester", "done", {"ablated": True, "files": ["test_generated.py"]})
        elif st.tests is None:
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
        if cancelled_now():
            return finish_cancelled()
    except GenerationError as exc:
        block("an agent could not produce a valid result", exc)
        return st

    # ---- test / fix loop ----
    dev_repaired = False
    attempt = 0
    # A single empty patch is not evidence that the model cannot fix the
    # failures: a truncated or rate-limited reply is indistinguishable from a
    # deliberate no-op at this layer. Give it one more chance before stopping.
    patch_no_ops = 0
    # What the next patch request says about the last attempt. It starts
    # empty and is filled in when an attempt changes nothing.
    patch_note = ""
    while True:
        if cancelled_now():
            return finish_cancelled()
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
        if cancelled_now():
            return finish_cancelled()
        if st.report.ok:
            st.status = "tests_green"
            if dev_repaired:
                # Same terminal-event rule as above: the last developer event
                # was the retry that fixed the code, so close the stage.
                emit(
                    "developer",
                    "done",
                    {
                        "files": sorted(st.code.files),
                        "repaired": "code_bug",
                        "retries": st.retry_dev,
                    },
                )
            break

        # triage and reflection read the same TestReport and never touch each
        # other's output, so they are the one genuinely independent pair in the
        # pipeline and the only place concurrency buys anything: together they
        # are two full LLM round trips on every failed fix-loop iteration.
        # Both swallow GenerationError and fall back to a deterministic verdict,
        # so neither can strand the other.
        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="fixloop") as pool:
            verdict_call = pool.submit(llm.thread_call(PM.triage, st.report))
            reflection_call = pool.submit(
                llm.thread_call(Developer.reflect, st.report, kind=st.spec.kind)
            )
            verdict_txt = verdict_call.result()
            refl = reflection_call.result()
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
            regenerated = True
            try:
                st.tests = Tester.spec_to_tests(
                    st.spec,
                    st.stories,
                    entry_module=st.code.entry_module,
                    entry_attr=st.code.entry_attr,
                    code=st.code,
                )
            except GenerationError as exc:
                regenerated = False
                log(f"tester regeneration failed: {exc}")
            # Terminal event for the same reason as the developer's: a retry
            # "start" left the tester stage looking stopped after the run.
            emit(
                "tester",
                "done",
                {
                    "files": sorted(st.tests.files),
                    "regenerated": regenerated,
                    "retry": st.retry_test,
                },
            )
            continue

        before = dict(st.code.files)
        if st.retry_dev >= max_dev:
            st.status = "unresolved"
            st.error = f"tests still failing after {st.retry_dev} developer retries"
            break
        try:
            st.code = Developer.patch_code(
                st.code,
                st.report,
                refl,
                tests=st.tests,
                kind=st.spec.kind,
                spec=st.spec,
                note=patch_note,
            )
        except GenerationError as exc:
            # The model was never reached (rate limit, outage): the bundle is
            # unchanged but NOT because the model tried and gave up. Say so —
            # "produced no change" would blame the model and hide the outage.
            st.status = "unresolved"
            st.error = f"developer patch LLM call failed ({exc}); re-running later may succeed"
            emit("developer", "done", {"changed": False, "retry": st.retry_dev, "llm_error": True})
            break
        if st.code.files == before:
            patch_no_ops += 1
            if patch_no_ops < 2:
                # The same prompt produced the same answer. Say so in the next
                # one: a retry that repeats the request verbatim gets the same
                # reply back.
                patch_note = (
                    "Your previous reply reproduced the current code exactly, so nothing "
                    "changed. Make one concrete, minimal edit that fixes the failure, and "
                    "reply with the complete corrected file.\n"
                )
                log("developer patch produced no change; retrying with a different ask")
                continue
            # Two unchanged bundles mean every further attempt repeats this one.
            st.status = "unresolved"
            st.error = "developer patch produced no change; stopping instead of looping"
            emit("developer", "done", {"changed": False, "retry": st.retry_dev})
            break
        patch_no_ops = 0
        patch_note = ""
        st.retry_dev += 1
        dev_repaired = True
        _write_code(out, st.code)
        emit("developer", "start", {"reason": verdict_txt, "retry": st.retry_dev})
        # Re-verify the entrypoint still resolves after a patch. A program has
        # no ASGI app object; its entrypoint is the declared module.
        if st.spec.kind == "program":
            if not st.code.entry_module or not any(f == st.spec.entrypoint for f in st.code.files):
                st.status = "unresolved"
                st.error = "patched code no longer contains the declared entrypoint"
                break
        elif not st.code.entry_module:
            st.status = "unresolved"
            st.error = "patched code no longer exposes an importable app object"
            break

    # ---- UI nodes ----
    # Runs only on a green backend: the UI binds to endpoints that now exist and
    # are exercised by a passing suite. Every failure here is swallowed — the
    # UI verdict is reported to the user and to the eval metrics, and the
    # terminal status below is decided by the backend alone.
    if st.status == "tests_green" and st.spec.has_ui and not st.skip_ui:
        # A full-stack application serves its own interface from the app
        # package, so the files land in code/static/ and the running process
        # serves them. A service with a separate preview page keeps them in
        # ui/, where the preview gateway can serve them.
        into_static = st.spec.kind == "fullstack"
        try:
            if st.ui_spec is None:
                emit("ui_designer", "start")
                st.ui_spec = UI.design_ui(st.spec, st.stories, followup=followup)
                _save(out, "ui_contract.json", st.ui_spec.model_dump_json(indent=2))
                emit(
                    "ui_designer",
                    "done",
                    {
                        "pages": len(st.ui_spec.pages),
                        "components": len(st.ui_spec.components),
                        "bindings": len(st.ui_spec.data_bindings),
                        "files": st.ui_spec.files,
                    },
                )
            if cancelled_now():
                return finish_cancelled()

            if st.ui_code is None:
                emit("ui_developer", "start")
                st.ui_code = UI.build_ui(
                    st.spec,
                    st.stories,
                    st.ui_spec,
                    entry_module=st.code.entry_module,
                    entry_attr=st.code.entry_attr,
                    followup=followup,
                )
                _write_ui_code(out, st.ui_code, into_static=into_static)
                emit("ui_developer", "done", {"files": sorted(st.ui_code.files)})
            if cancelled_now():
                return finish_cancelled()

            emit("ui_gate", "start")
            st.ui_report = UI.gate(st.spec, st.ui_spec, st.ui_code.files)
            if st.ui_report.failures:
                # One targeted repair pass. The first generation is the cheap
                # path; when the gate finds a bare shell or missing states, the
                # exact findings are more useful than a blind second attempt.
                # Keep the repaired UI only when it removes failures.
                emit("ui_developer", "start", {"reason": "ui_gate", "retry": 1})
                repaired_code = None
                repaired_report = None
                try:
                    repaired_code = UI.build_ui(
                        st.spec,
                        st.stories,
                        st.ui_spec,
                        entry_module=st.code.entry_module,
                        entry_attr=st.code.entry_attr,
                        feedback=UI.gate_feedback(st.ui_report),
                        followup=followup,
                    )
                    repaired_report = UI.gate(st.spec, st.ui_spec, repaired_code.files)
                except GenerationError as exc:
                    log(f"ui repair pass failed: {exc}")
                improved = (
                    repaired_code is not None
                    and repaired_report is not None
                    and len(repaired_report.failures) < len(st.ui_report.failures)
                )
                if improved:
                    st.ui_code = repaired_code
                    st.ui_report = repaired_report
                    _write_ui_code(out, st.ui_code, into_static=into_static)
                emit(
                    "ui_developer",
                    "done",
                    {
                        "files": sorted(st.ui_code.files),
                        "repaired": bool(improved),
                        "remaining": len(st.ui_report.failures),
                    },
                )
            _save(out, "ui_report.json", st.ui_report.model_dump_json(indent=2))
            emit(
                "ui_gate",
                "done",
                {
                    "ok": st.ui_report.ok,
                    "failed": [c.name for c in st.ui_report.failures],
                    "advisory": True,
                },
            )
            if not st.ui_report.ok:
                log(
                    "ui gate: "
                    + "; ".join(f"{c.name}: {c.detail}" for c in st.ui_report.failures)
                    + " (advisory; terminal status unaffected)"
                )
            # The domain check ran before the UI existed and could only read
            # the backend. Now score the whole deliverable: a full-stack run
            # legitimately names its subject in the page ("Tic Tac Toe" in the
            # title) even when the Python identifiers do not repeat it. This
            # can only add evidence — the gate already passed above, and the
            # blob is a superset of the code the verdict was based on.
            if st.coverage is not None and st.ui_code is not None and st.coverage.missing:
                routes = all_routes(st.code.files) if st.spec.kind == "api" else set()
                verdict = coverage_check(
                    requirement, {**st.code.files, **st.ui_code.files}, routes
                )
                st.coverage = CoverageReport(
                    covered=verdict.covered,
                    missing=verdict.missing,
                    conclusive=verdict.conclusive,
                )
                _save(out, "coverage.json", st.coverage.model_dump_json(indent=2))
                log(
                    "domain check over the full deliverable (code + UI): "
                    f"missing {verdict.missing or 'nothing'}"
                )
        except GenerationError as exc:
            st.ui_report = UiReport(
                ok=False,
                checks=[
                    UiCheck(name="generation", ok=False, detail=str(exc)[:400]),
                ],
            )
            _save(out, "ui_report.json", st.ui_report.model_dump_json(indent=2))
            log(f"ui generation failed: {exc} (advisory; terminal status unaffected)")
            emit("ui_gate", "done", {"ok": False, "failed": ["generation"], "advisory": True})
        if cancelled_now():
            return finish_cancelled()

    # ---- review loop ----
    if st.status == "tests_green" and skip_reviewer:
        st.status = "accepted_no_review"
    elif st.status == "tests_green":
        # A review round that changes nothing is the same dead end as a test
        # round that changes nothing; the next ask says so.
        review_no_ops = 0
        for _ in range(max_review + 1):
            if cancelled_now():
                return finish_cancelled()
            ruff_out, bandit_out = Runner.static_analysis(st.code)
            _save(out, "ruff.txt", ruff_out)
            _save(out, "bandit.txt", bandit_out)
            st.review = Reviewer.review(
                st.code,
                st.spec,
                st.tests,
                st.report,
                ruff_out,
                bandit_out,
                ui_spec=st.ui_spec,
                ui_code=st.ui_code,
                ui_report=st.ui_report,
            )
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
                    spec=st.spec,
                    note=(
                        "Your previous reply reproduced the current code exactly, so the "
                        "blockers survived. Make one concrete, minimal edit per blocker and "
                        "reply with the complete corrected file.\n"
                        if review_no_ops
                        else ""
                    ),
                )
            except GenerationError as exc:
                st.status = "unresolved_review"
                st.error = f"review patch LLM call failed ({exc}); re-running later may succeed"
                break
            if st.code.files == before:
                st.status = "unresolved_review"
                st.error = "review patch produced no change; stopping instead of looping"
                break
            review_no_ops = 0
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
