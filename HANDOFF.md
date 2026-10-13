# Handoff Prompt — Multi-Agent Software Team

Copy everything below the line into a fresh agent session to continue work on this repo.

---

You are taking over the repo at `/home/shubham/projects/Multi-agent` (git repo, branch: check `git status`).

## 1. What this app does

5 LLM agents turn plain English into a tested, reviewed FastAPI REST API. No template fallback — failed generation ends `blocked`, never a silent substitution.

Pipeline (`backend/graph/workflow.py:run_team`):

```
Requirement
 -> PM (stories + acceptance criteria + 1 clarifying Q)
 -> Reasoner (advisory Plan{approach,decisions,risks,edge_cases,open_questions}, ablatable via skip_reasoner)
 -> Designer (OpenAPI YAML + file plan {entrypoint, files, kind, public_api})
 -> Developer (FastAPI code bundle)
 -> lint repair (ruff) -> domain coverage gate (agents/coverage.py, <ratio => domain_missed)
 -> boot check (agents/introspect.py AST discovery + sandbox/runner.py:boot_check)
 -> Tester (spec+stories ONLY, never sees code) OR developer self-checks if skip_tester
 -> Sandbox Runner (pytest, local subprocess by default, --docker for Docker no-net)
 -> triage (PM: code_bug vs test_bug) + reflection (Developer.reflect -> memory[])
 -> Developer patch loop (max_dev=4, max_test=2)
 -> Reviewer (code+spec+tests + ruff + bandit, blocker=>retry max_review=2)
 -> final (summary.json)
```

Terminal statuses: `accepted | accepted_no_review | unresolved | unresolved_review | blocked | domain_missed | cancelled | failed | restored`.

Key invariants:
- Tester never sees code (AgentCoder independence).
- Reasoner plan is advisory; Tester/Reviewer never see it.
- Entrypoint discovered by AST (`find_app_object`, `all_routes`, `spec_paths`), never assumed `main.py`.
- Every LLM call logs to `$RUNS_DIR/llm_log.jsonl` with model/latency/failures/reasoning.
- Artifacts per run in `outputs/<run_id>/`: `stories.json, reasoning.json, spec.yaml, plan.json, code/, tests/, coverage.json, boot.txt, report_try*.json, reflections.json, ruff.txt, bandit.txt, review.json, summary.json`.

## 2. Layout

```
backend/  FastAPI app (app/main.py, app/routers/runs.py,evals.py,auth.py,health.py, app/services/runs.py RunStore)
          agents/ (pm,reasoner,designer,developer,tester,reviewer,llm,coverage,introspect,kind,blocks)
          graph/workflow.py  schemas/messages.py  sandbox/runner.py  evals/  tests/ (11 files)
frontend/ React+Vite+TS SPA (src/App.tsx, api.ts, components/) — Pipeline/Stories/Plan/Spec/Code/Tests/Report/Review tabs, SSE+polling, Stop/Restart/Resume
cli.py    --stub --run --out --eval --live --no-tester --no-reviewer --no-reasoner --docker
run_all.sh  gates: pytest -> stub -> reviewer recall -> coverage -> [if --llm] connectivity + 3 novel domains + baseline + ablations + eval suite
deploy/   prod compose + Caddyfile(s) + GCP bootstrap
docs/     DEPLOY.md, ARCHITECTURE.md, original PDF brief
docker-compose.yml  dev stack (backend :8000 + Vite :5173, project name multi-agent-team-dev)
```

Backend entry: `backend/app/main.py:create_app()` — auth-gate middleware (default-deny), CORS, `/api/*` routers. `RunStore` = ThreadPoolExecutor (`MAX_CONCURRENT_RUNS=2`), cooperative cancel via `threading.Event`, SSE fan-out (`MAX_EVENTS=500`), rehydrate on boot.

Frontend data flow (`frontend/src/App.tsx`): `api.session() -> api.listRuns() -> api.createRun({requirement,skip_*}) -> subscribeToRun(SSE) + poll every 1500ms while queued/running/stopping -> api.getRun(id) detail -> tabs`.

LLM router (`backend/agents/llm.py`, ~1000 lines, urllib-based): `LLM_PROVIDER=groq|gemini|github|openrouter|huggingface|openai|local|auto|groq,gemini`. Per-role Ollama models (`OLLAMA_MODEL_DESIGNER=7b`, rest 3b), `SKIP_OLLAMA`, `PREFER_LOCAL_ONLY`, `LLM_REASONING_ROLES`, `LLM_TIMEOUT`. `generate(required=True)` / `require()` raise `GenerationError` on empty. Scoped cache (`_RUN_SCOPE` + sha) `CACHE_MAX=512`.

Coverage gate (`backend/agents/coverage.py`): `GENERIC` stoplist, `term_groups()` with singular variants, `PASS_RATIO=0.35, MIN_COVERED=2, MIN_GROUPS=2`. Inconclusive (<2 groups) never blocks.

Sandbox (`backend/sandbox/runner.py`): `write_bundle()` + `run_tests_local()` / `run_tests(use_docker)` + `boot_check(public_api?)` + `static_analysis(ruff,bandit)`. Docker image `python:3.11-slim`, `--network none --memory 512m --cpus 1`, 60s pytest timeout.

Evals: `backend/evals/requirements_suite.json` (only 3 task-manager variants currently), `metrics.py:summarize_run`, `results.json`, `baseline.py`.

## 3. How to run / verify

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r backend/requirements.txt
pytest -q                          # offline, no model
python backend/evals/check_llm.py  # validates GROQ/GEMINI key, prints serving model
python cli.py --run "a library API that lends books" --out outputs/demo --live
python cli.py --eval
./run_all.sh                       # offline gates
./run_all.sh --llm --quick         # + generation gates (needs $LLM_PROVIDER key)
./run_all.sh --llm --docker        # sandbox in Docker
docker compose up                  # dev UI :5173, API :8000
```

Env: `cp .env.example .env` — set `LLM_PROVIDER=groq + GROQ_API_KEY` (recommended) or `LLM_PROVIDER=local + SKIP_OLLAMA=0` with `ollama pull qwen2.5-coder:7b-instruct-q4_K_M` + `qwen2.5-coder:3b`. Never commit `.env` (root `.env` exists locally, gitignored).

Measured: Groq ~45-100s, Gemini ~100-160s, Ollama-3b CPU ~350s for 3-4 endpoint API.

## 4. Known gaps / improvement backlog (from last review)

Proposed priorities — pick one, do not mix:
1. **Generation quality:** coverage gate is lexical (0.35) — add embedding check + enforce `spec_coverage`; expand `requirements_suite.json` beyond task-manager; add schemathesis/OpenAPI conformance; fix double `retry_dev+=1` in boot-repair loop (`workflow.py:510,520`).
2. **Security+reliability:** prod sandbox runs as local subprocess (no isolation) — make Docker mandatory + `--network none`; single shared operator auth, stateless cookie survives logout — add per-user + rotation/invalidation; add requirement length/rate-limit, run GC, OTel.
3. **Cost+speed:** `llm.py` urllib, no token/cost accounting — add litellm/SDK + structured output + jitter/circuit-breaker + per-run cost in `summary.json`; model IDs churn — registry health check.
4. **UX+ops:** polling+SSE redundant (`App.tsx:132,163`) — SSE-only; FileViewer read-only — add edit/diff/download-zip; fix docs drift (`README:226-230` duplicate table, `ARCHITECTURE:93-94` LangGraph contradiction, `EXECUTION_PLAN` still mentions template mode); don't commit `frontend/dist/`.

## 5. Working agreements

- Read-only recon first: `Read/Grep/Glob` before `Edit`; keep edits minimal, preserve style (ruff, temp 0.1 prompts).
- Verify every change: `pytest -q`, `python cli.py --stub`, relevant `backend/tests/test_*.py`.
- Never add template fallbacks; `blocked/domain_missed` must stay reachable.
- Never commit secrets; check `git status/diff/log --oneline -10` before any commit; only commit when explicitly asked.
- Use `/tmp/opencode` for scratch, not `outputs/` (reserved for runs).
- Frontend: `npm run dev` via compose; backend reload via uvicorn `--reload`.
- If LLM quota 429/403/404: report `blocked` with backend errors, do not retry silently; see README “Choosing a backend” for 403-vs-404 vs 429 triage.

## 6. Suggested first tasks for next session

- [ ] `git status && pytest -q` baseline
- [ ] Reproduce one `--run` on current `LLM_PROVIDER`
- [ ] Pick ONE backlog item above, create todo list, implement + test
- [ ] Update `README Results` + `backend/evals/results.json` if behavior changes

Context cutoff: 2026-01-04. Current date (UTC): 2026-10-13. Docs: `README.md`, `ARCHITECTURE.md`, `EXECUTION_PLAN.md`, `docs/DEPLOY.md`.
