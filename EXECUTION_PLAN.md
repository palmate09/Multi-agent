# Multi-Agent Software Team — Execution Plan

> 6 phases, local-first (10GB RAM + 4GB RTX 2050), simple task-manager, free-tier fallback.
> Each phase ends with a **gate** — do not advance on red.

## Hardware / cost policy (applies to all phases)

* Local: Ollama `qwen2.5-coder:7b-instruct-q4_K_M`, smaller `3b` for PM/triage.
  If `ollama` absent → template mode (deterministic, offline) so gates still pass.
* Cloud (only Developer/Tester/Reviewer, only on failure or explicit flag):
  `GEMINI_API_KEY` (Flash free) → `GROQ_API_KEY` → `OPENROUTER_API_KEY` → `OPENAI_API_KEY` (mini last).
* All LLM calls via `agents/llm.py:generate()` with JSONL log (`prompt/response/tokens/latency/model`), input-hash cache, trace truncation (20 lines), temp 0–0.2.

## Phase 0 — Foundations

Steps:
1. Scaffold `agents/ graph/ schemas/ sandbox/ evals/ outputs/ tests/` + `__init__.py`, `requirements.txt`, `.gitignore`, `cli.py`.
2. `sandbox/Dockerfile` (python:3.11-slim + fastapi uvicorn sqlalchemy pytest ruff bandit) + `run.sh` + `runner.py` (no-net, 512m, 60s timeout; local-subprocess fallback if Docker absent).
3. `agents/llm.py` (Ollama → free APIs → template; logging + cache).
4. `evals/task-manager.md` + `requirements_suite.json` (3 requirements rising difficulty).
5. `evals/baseline.py` single-prompt codegen + `outputs/baseline/` result.

Gate: `python evals/baseline.py` writes `outputs/baseline/result.json` with pass/fail recorded.
Verify: `ls outputs/baseline` + `cat result.json`.

## Phase 1 — Contracts and orchestration

Steps:
1. `schemas/messages.py` Pydantic v2 models: Requirement, UserStory(ies), ApiSpec, CodeBundle, TestSuite, TestFailure/Report, ReviewComment/Report, GraphState + counters + memory.
2. `graph/workflow.py`: one node per agent (stub OK), edges `pm→designer→developer∥tester→runner→triage→reviewer→final`, conditional fail→developer/tester, cap→unresolved. Try LangGraph, fallback sequential.
3. `tests/test_schemas.py` + `test_workflow.py` stub E2E asserting message shapes + log lines.
4. Run `pytest -q` + `python cli.py --stub` and inspect `outputs/<run>/log.jsonl`.

Gate: stub run traverses every node, all messages validate, log complete.

## Phase 2 — Core agents (PM, Designer, Developer)

Steps:
1. `agents/pm.py`: requirement → stories with testable AC (ChatDev clarifying-Q if vague: "assumed X, confirm?"). Template covers task-manager CRUD.
2. `agents/designer.py`: stories → OpenAPI 3.1 YAML + models; validate with `openapi-spec-validator` (or structural check fallback); regenerate ≤3 on error.
3. `agents/developer.py`: spec → `CodeBundle` via ACI tools `read_file/write_file/run` only; template emits working FastAPI+SQLite app (`main.py models.py schemas_pyd.py database.py`).
4. Boot check: `runner.py::boot_check` imports app / hits `/health` within 30s.
5. Persist every hand-off to `outputs/<run_id>/` (stories.json, spec.yaml, code/).

Gate: requirement → valid spec → app boots in sandbox (or local fallback).

## Phase 3 — Tester and fix loop

Steps:
1. `agents/tester.py`: spec+stories (NO code) → pytest suite (success/validation/error per endpoint: create/get/update/delete/404/422).
2. `sandbox/runner.py::run_tests`: copy code+tests to temp dir, `pytest -q`, parse to `TestReport{name,error,trace:20}`.
3. Reflexion: `agents/developer.py::reflect(failures)` → 2–3 sentence cause → `state.memory[]`.
4. Patch: Developer regenerates only failing files (diff-limited prompt).
5. `agents/pm.py::triage(report)`: `code_bug` → Developer, `test_bug` (e.g. wrong expected status) → Tester. Heuristic + LLM override.
6. Loop to green or `retry_dev≥4` / `retry_test≥2` → `unresolved`.

Gate: 3 consecutive runs terminate (green or unresolved), never hang; `attempts_to_green` logged.

## Phase 4 — Reviewer and quality gates

Steps:
1. `agents/reviewer.py` in: code+spec+tests+`ruff check`+`bandit -q` outputs.
2. Checks: every spec endpoint exists; every AC has ≥1 test; no `exec/eval`, string-SQL, missing validation.
3. Rank `blocker/major/minor`; blocker→Developer (≤2), rest→final report.
4. `tests/test_reviewer.py`: plant 3 bugs (missing title validation, missing 404, f-string SQL) → assert 3/3 flagged.
5. PM accepts only if 0 blockers.

Gate: reviewer recall 3/3 on planted sample.

## Phase 5 — Evaluation and portfolio

Steps:
1. `evals/metrics.py`: `test_pass_rate, spec_coverage, attempts_to_green, reviewer_recall, tokens, wall_time` from logs.
2. `evals/requirements_suite.json`: task-manager, +filter/pagination variant, +notes variant.
3. Run `python cli.py --eval` (baseline vs team, 1–3 reps) → `evals/results.json` + table.
4. Ablations: `--no-tester`, `--no-reviewer` flags showing delta.
5. `cli.py --live` streams steps; `README.md` (arch diagram ASCII, results, limits, demo); optional screen recording.

Gate: `results.json` + README table exist.

## Verification commands (run after each phase)

```
pip install -r requirements.txt
pytest -q
python cli.py --stub
python cli.py --run "task manager" --out outputs/demo
python evals/baseline.py
python -m tests.test_reviewer  # phase 4
python cli.py --eval            # phase 5
```

## Risks → fallbacks

| Risk | Fallback |
|---|---|
| Loop no converge | Caps + `unresolved` to PM |
| Wrong test breaks good code | Triage + spec-only tester + spot-check |
| Unsafe code | Docker no-net +Limits; local tmp fallback |
| Cost | Token log + small/local for easy roles + free tiers |
| Lucky run | Repeat 3×, report spread not best |
| Scope creep | REST-only; UI later |

## Done definition

`outputs/demo` has `spec.yaml + code boots + tests green + review 0 blockers`, `evals/results.json` shows team ≥ baseline, README complete.
