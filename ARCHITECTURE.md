# Multi-Agent Software Team — Architecture

> Plain-English requirement → tested, reviewed REST API via 5 cooperating LLM agents.
> Source: `docs/Multi-Agent Software Team Project Plan.pdf` + 8 grounding papers.

## 1. Goal and success claim

Build a team that **beats a single-agent baseline** on the same requirements,
measured by test pass rate, spec coverage, attempts-to-green, reviewer recall, cost.

Target output: **FastAPI + SQLite + pytest** app running in Docker, driven by an
**OpenAPI spec** derived from user stories.

## 2. System overview

```
User Requirement
  │
  ▼
Project Manager ──► UserStories + AcceptanceCriteria + TaskBoard
  │
  ▼
Designer ──► OpenAPI spec (YAML) + data model + folder structure
  │               │ validates with openapi-spec-validator, max 3 retries
  ├───────────────┴────────────────┐
  ▼                                ▼
Developer (spec → code)      Tester (spec+stories → tests, NEVER sees code)
  │                                │
  └────────► Sandbox Runner (container, sandbox-net, internal) ──► TestReport
                     │ fail
                     ▼
               Reflection (2-3 sentences) + Triage (PM: code_bug vs test_bug)
                     │ → back to Developer (max 4) or Tester (max 2)
                     │ pass
                     ▼
               Reviewer (code+spec+tests + ruff + bandit)
                     │ blocker → Developer (max 2)
                     │ clean
                     ▼
               Project Manager final summary
```

Two loops back to Developer, one test gate, one review gate.

Sequential, not parallel: the Tester's independence comes from never seeing the
code (AgentCoder), not from running concurrently, and the small local models
gain nothing from interleaving.

**Domain coverage gate** (`agents/coverage.py`) runs between generation and
testing. It extracts the requirement's subject matter and requires the code to
reflect it, so a generation that answers a different question cannot be graded
on its own tests. Below 60% match the run ends `domain_missed`.

## 3. Agent contracts

Each agent: one job, fixed in/out schema, measurable done condition, file in `agents/`.

| Agent | Takes in | Produces | Tools | Done when |
|---|---|---|---|---|
| Project Manager (`agents/pm.py`) | Requirement, status reports | UserStories, AC, task board, final summary, triage verdict | Task board JSON, message router | Every AC maps to ≥1 test |
| Reasoner (`agents/reasoner.py`) | Requirement + UserStories | `Plan{approach, decisions, risks, edge_cases, open_questions}` | JSON block in its reply | Plan parses, or degrades to prose (advisory: never blocks) |
| Designer (`agents/designer.py`) | UserStories + requirement + Plan | OpenAPI YAML + **file plan** (`entrypoint`, `files`) | OpenAPI validator | Spec validates (shape only) + covers every story |
| Developer (`agents/developer.py`) | ApiSpec + file plan + failure reports + reflections + Plan | FastAPI code + DB models | writes the planned files | App imports, lints clean, Tester suite passes |
| Tester (`agents/tester.py`) | ApiSpec + stories (NOT code) | pytest suite, coverage, bug reports | pytest, coverage, Docker shell | Each endpoint: success + validation + error tests |
| UI Designer (`agents/ui.py:design_ui`) | ApiSpec + stories | `UiSpec{pages, components, data_bindings, files}` | JSON block in its reply | Contract parses, plans `index.html`, and ≥1 binding resolves against the spec's paths |
| UI Developer (`agents/ui.py:build_ui`) | ApiSpec + UiSpec + entrypoint | `CodeBundle` of `index.html`/`app.js`/`styles.css` | writes the planned files | All planned files non-empty; a reply that lost its `###` headers is rejected |
| Reviewer (`agents/reviewer.py`) | Code + tests + TestReport + ruff/bandit output + UiReport | Ranked comments blocker/major/minor | ruff, bandit, diff reader | 0 blockers + spec conformance confirmed |

Schemas (`schemas/messages.py`, Pydantic v2):
`Requirement, UserStory, UserStories, Plan{approach, decisions, risks, edge_cases, open_questions, structured}, ApiSpec{openapi_yaml, entrypoint, files, domain_terms, kind, has_ui}, CodeBundle{files, entry_module, entry_attr}, TestSuite, TestFailure, TestReport, ReviewComment, ReviewReport, CoverageReport, UiCheck, UiSpec, UiReport, GraphState{retry_*, memory[], plan, ui_spec, ui_code, ui_report, skip_ui, error}`.

**Entrypoint discovery** (`agents/introspect.py`) parses generated modules with
`ast` to find the module-level `FastAPI()` instance and to enumerate route
decorators. Nothing assumes `main.py`, and a commented-out `app = FastAPI()`
cannot be mistaken for the real one.

## 3a. Frontend generation and the UI gate

Opt-in per run (`ui: true`, or `--ui`): `ApiSpec.has_ui`, not a third `kind`,
because only api-kind projects have endpoints to bind a page to.

Three nodes run **after the backend tests are green**, never in parallel — a UI
written against endpoints that are still being repaired binds to nothing:

* `ui_designer` → `UiSpec`. Every `data_bindings` entry is checked against
  `spec_paths(spec.openapi_yaml)`, so the contract cannot name an endpoint the
  API does not serve.
* `ui_developer` → static HTML/JS/CSS. No build step, no npm, no bundler.
* `ui_gate` → seven pure static checks (`missing_files`, `rooted_url`,
  `external_asset`, `xss_innerhtml`, `undeclared_endpoint`, `unbound_endpoint`,
  `no_mount`). No LLM, milliseconds.

Two constraints are enforced rather than merely requested in the prompt:

* **Relative URLs.** The UI is served at `/preview/<run_id>/`, so
  `fetch('/books')` resolves against the *platform's* host and hits this app's
  own API instead of the sandboxed one. `rooted_url` fails the check.
* **No external origins.** The preview sandbox has no network, so a CDN link is
  a page that renders half-styled.

**The UI verdict is advisory and never changes the terminal status.** A 3b
model producing a mediocre page must not turn a green, tested, independently
reviewed backend run into a failure. `UiReport.ok` is reported to the UI tab
and to `evals/metrics.py`, and nothing else reads it. The Reviewer restates
each failed check as `minor`, never `blocker`.

UI artifacts: `ui_contract.json`, `ui/` (the files, kept out of `code/` so the
test runner never sees an HTML tree as a Python package), `ui_report.json` —
all three read back by `reconstruct_state`, so a resume adopts the UI rather
than regenerating it.

## 4. Orchestration (`graph/workflow.py`)

Nodes: `pm → reasoner → designer → developer → coverage → boot → tester → runner → triage? → ui_designer? → ui_developer? → ui_gate? → reviewer → final`.
`reasoner` is advisory and ablatable (`skip_reasoner`): it writes
`reasoning.json` and hands `GraphState.plan` to the Designer and Developer.
Neither the Tester nor the Reviewer is given the plan, so a run is still graded
on its contract and its tests rather than on what the plan promised.
The three UI nodes are declared unconditionally but only walked when
`want_ui` is set *and* the project is an api with declared endpoints.
Conditional edges:

* `coverage -- ratio<0.6 --> final(status=domain_missed)`
* `boot -- import failed --> final(status=unresolved)`
* `runner -- all pass --> reviewer` (via the UI nodes when `want_ui`)
* `runner -- fail + retry_dev<4 --> developer (with TestReport + reflection)`
* `triage -- test_bug + retry_test<2 --> tester`
* `reviewer -- blocker + retry_review<2 --> developer`
* `ui_gate -- failed --> reviewer` — **no edge back**, and never a terminal status
* any generation failure → `final(status=blocked)` with the per-backend errors
* else `-- cap --> final(status=unresolved)` — never hangs.

**No LangGraph.** The orchestration is a plain sequential function; the earlier
docstring claiming a LangGraph path with a sequential fallback was wrong and
has been removed.

State carries `memory: str[]` (Reflexion reflections), counters, last reports.
Every LLM call logs `{prompt, response, tokens, latency, model}` to JSONL.
All hand-offs saved under `outputs/<run_id>/` (MetaGPT inspectable-docs rule).

**Concurrency (Phase 1):** `triage` and `reflection` run concurrently via
`ThreadPoolExecutor(max_workers=2, thread_name_prefix="fixloop")` using
`llm.thread_call()` which copies `_RUN_SCOPE` and `_CANCEL_SCOPE` ContextVars
into the worker thread. Per-call LLM state is thread-local (`_CallState`/`_CALL`)
fixing a real race across `RunStore`'s `ThreadPoolExecutor`.

## 5. Stack

* **Orchestration:** Python 3.12 + plain functions (LangGraph removed).
* **Generated target:** FastAPI + SQLAlchemy + SQLite + pytest.
* **Sandbox image:** `backend/sandbox/Dockerfile` — Python 3.12-slim, multi-stage,
  non-root uid 10001, OCI labels, wheelhouse (fastapi, uvicorn, sqlalchemy,
  pydantic, httpx, psycopg, aiosqlite, pytest, ruff, bandit, openapi-spec-validator,
  precompiled `.pyc`).
* **Sandbox runner** (`backend/sandbox/runner.py`): `SandboxLimits` from env
  (`SANDBOX_MEMORY=512m`, `SANDBOX_CPUS=1`, `SANDBOX_PIDS_LIMIT=256`,
  `SANDBOX_CPUSET=0`, `SANDBOX_MAX_CONCURRENT=4`, `SANDBOX_TMPFS=/tmp:rw,noexec,nosuid,size=64m`),
  CIS `isolation_flags()` (`--network sandbox-net --read-only --cap-drop ALL
  --security-opt no-new-privileges --pids-limit 256 --memory 512m --cpus 1
  --cpuset-cpus 0 --user 10001:10001 --tmpfs /tmp:rw,noexec,nosuid,size=64m`),
  image cache by Dockerfile hash, `:ro` bind mount + `/data` tmpfs, wheelhouse
  miss diagnostics, concurrency semaphore.
* **Sandbox transport** (`backend/sandbox/docker.py`): CLI-based (no daemon
  dependency in backend), `ensure_image`, `ensure_network`, `reachable_address`
  (container IP on internal net, host port on bridge), `start_detached`,
  `stop_container`, `logs`.
* **Sandbox sidecar** (`backend/sandbox/sidecar.py`): FastAPI app owning
  `docker.sock`; `POST /sandbox/runs` (argv, mount, env), `DELETE /sandbox/runs/{id}`,
  `GET /sandbox/runs/{id}/logs`; image allowlist, mount root confinement,
  credential stripping, `extra="forbid"` on request model.
* **Preview** (`app/services/playground.py`): `PreviewSession` with `container_id`,
  `host`, `base_url`, `mode`; `_spawn_container` uses `Docker.start_detached` +
  `reachable_address`; gateway proxies to `session.base_url`; child env points
  `PREVIEW_DIR`/`SQLITE_PATH`/`DATABASE_URL` at `/data` tmpfs; TTL reap + crash
  restart.
* **Database** (`app/db/`): PostgreSQL 16 (optional; degrades to in-memory when
  `DATABASE_URL` unset). Schema: users, sessions, api_keys, login_attempts,
  runs, run_events, run_files, preview_sessions, conversations, messages,
  audit_log. `SessionStore` (server-side sessions, revocable logout),
  `AttemptLimiter` (durable rate limiting via `login_attempts`),
  `RunIndex` (upsert_run, append_event, index_files, reconcile),
  per-run schema via `schema_name(run_id)` + `build_url()` with `search_path`.
* **Static analysis:** ruff + bandit (Reviewer input, not just LLM opinion).
* **LLM (`agents/llm.py`):**
  * Primary local: Ollama `qwen2.5-coder:7b-instruct-q4_K_M` (fits 4GB VRAM partial
    offload), fallback `qwen2.5-coder:3b` / `llama3.2:3b` for PM/triage/reflection.
    Works when `ollama` binary present.
  * Cloud fallback router per role (Developer/Tester/Reviewer heavy): Gemini 2.0
    Flash free → Groq `llama-3.3-70b` free tier → OpenRouter free → `gpt-4o-mini`
    paid last. Keys via env: `GEMINI_API_KEY, GROQ_API_KEY, OPENROUTER_API_KEY,
    OPENAI_API_KEY`.
    * **No template fallback.** An earlier design substituted built-in
      task-manager generators whenever a call failed. Because the Tester's suite
      was also built in, a library requirement produced a task manager, passed its
      own tests and reported `accepted`. `generate()` now returns `ok: False` and
      `require()` raises `GenerationError`; the run ends `blocked`.
  * **Per-role routing:** the Designer uses 7b for structured YAML, the rest 3b
    (~12.6 vs ~6.5 tok/s on 4 CPU cores). `PREFER_LOCAL_ONLY=1` by default so a
    rate-limited cloud key cannot silently change the output.
  * **Token ceilings (Phase 1):** per-role `_NUM_PREDICT` table + `_HOSTED_FLOOR`
    for designer/developer/tester on hosted paths; overrides
    `LLM_MAX_TOKENS_<ROLE>` > `LLM_MAX_TOKENS` > provider-specific. Truncation
    detection (`finishReason=MAX_TOKENS`, `finish_reason=length`,
    `done_reason=length`) is loud and counted.
  * Cost savers: cache by input hash + run scope, truncate traces, temp 0.1,
    per-role token caps.

## 6. Research grounding

| # | Paper (arXiv) | Borrowed mechanism in this repo |
|---|---|---|
| 1 | ChatDev `2307.07924` Qian 2023 | Phased chain + communicative dehallucination: PM asks 1 clarifying Q when ambiguous |
| 2 | MetaGPT `2308.00352` Hong 2023 | SOPs embedded in prompts + structured docs between roles + shared pool |
| 3 | AgentCoder `2312.13010` Huang 2023 | Independent tester (Basic/Edge/Large), executor feeds failures to programmer, iteration budget |
| 4 | Self-collab `2304.07590` Dong 2023 | Analyst/Coder/Tester role-instruction baseline the team must beat |
| 5 | AutoGen `2308.08155` Wu 2023 | Conversable/auto-reply + group-chat routing patterns; alternative to LangGraph |
| 6 | SWE-agent `2405.15793` Yang 2024 | ACI: tiny `read/write/run/search` toolset with ≤50-line summaries, not raw shell |
| 7 | Reflexion `2303.11366` Shinn 2023 | Actor/Evaluator/Self-Reflection; 2–3 sentence reflection appended to next attempt memory |
| 8 | SWE-bench `2310.06770` Jimenez 2023 | Fail-to-Pass Docker eval framing; stretch goal beyond task-manager |

## 7. Repository layout

```
ARCHITECTURE.md  EXECUTION_PLAN.md  README.md  requirements.txt  cli.py
agents/    # pm.py reasoner.py designer.py developer.py tester.py reviewer.py ui.py llm.py __init__.py
schemas/   # messages.py __init__.py
graph/     # workflow.py __init__.py
sandbox/   # Dockerfile runner.py flags.py docker.py sidecar.py run.sh __init__.py requirements.txt
app/       # main.py config.py auth.py schemas.py
            # routers/    runs.py auth.py evals.py health.py playground.py
            # services/   runs.py playground.py
            # db/         engine.py sessions.py runs.py runschema.py schema.sql
evals/     # task-manager.md requirements_suite.json baseline.py metrics.py perf.py
outputs/   # <run_id>/spec.yaml code/... tests/... ui/... reports/ (generated)
tests/     # test_schemas.py test_agents.py test_workflow.py test_ui_gate.py
            # test_ui_workflow.py test_playground.py test_sandbox_runner.py
            # test_sandbox_sidecar.py test_sessions.py test_preview_container.py
            # test_perf.py
docs/      # original project plan PDF
deploy/    # docker-compose.prod.yml, Caddyfile, vm-setup.sh, bootstrap-env.sh
```

## 7a. Playground (`app/services/playground.py`, `app/routers/playground.py`)

The accepted app is booted in a **hardened sandbox container** on the internal
`sandbox-net` network and reverse-proxied to the browser at
`/preview/<run_id>/`.

```
GET  /api/runs/{rid}/playground            -> {state, url, host, mode, error, log_tail}
POST /api/runs/{rid}/playground/start      -> 202, idempotent
POST /api/runs/{rid}/playground/stop       -> 200
ANY  /preview/{rid}/{path:path}            -> reverse proxy to container IP:8000
```

* `PreviewSession{run_id, workdir, log_path, state, port, host, base_url,
  container_id, mode, terminating, ever_booted}` lives in a run-id → session map.
  The browser never learns the container address.
* **The gateway serves the UI itself** from the run's `ui/` directory; only
  everything else is proxied to the app. The Developer node runs before the UI
  exists and never sees it, so a real generation's `main.py` mounts nothing —
  proxying `/preview/<rid>/` to it 404s with the page unused on disk. Serving
  the files in the gateway makes the page work regardless. Only text assets
  (`.html/.js/.css/.json/.svg/.txt`) are served, and the resolved path is
  re-checked for containment so `ui/../../…` cannot escape the run.
* The code is **copied** to `$RUNS_DIR/.preview/<run_id>/` before booting: the
  run directory is the pipeline's durable record, which a user may delete or
  resume from at any moment.
* Env hardening: every platform credential (`*_API_KEY`, `AUTH_*`, `GITHUB_TOKEN`,
  `HF_TOKEN`, …) is stripped from the child; `PREVIEW=1` and any SQLite path is
  pointed inside the `/data` tmpfs (container writable layer).
* Health probe: poll the spec's first declared path (else `/health`, else `/`)
  for HTTP 200 within `PREVIEW_BOOT_TIMEOUT_SECONDS` → `running`, else
  `crashed` with the log tail. `ever_booted` is sticky across stops and
  restarts, so `evals/metrics.py:preview_booted` answers "did this app ever
  boot" rather than "is it up right now".
* `/preview` is **authenticated** (it must never become an open relay) and
  **exempt from the double-submit CSRF check**: the caller is the iframe
  loading sandboxed generated code, which cannot read the platform's CSRF
  cookie, and the session cookie is SameSite=strict.
* Lifecycle: `PREVIEW_TTL_SECONDS` (1800s idle) reaper, `SANDBOX_MAX_CONCURRENT`
  (4), stopped on run delete / restart / backend shutdown.
* The gateway auto-restarts once if the process died, which is what survives a
  review patch rewriting the files under a live preview.
* **This IS isolation:** a hardened container on `sandbox-net` (`internal: true`),
  `--read-only`, `--cap-drop ALL`, `--security-opt no-new-privileges`,
  `--pids-limit 256`, `--memory 512m`, `--cpus 1`, `--user 10001:10001`,
  `--tmpfs /tmp:rw,noexec,nosuid,size=64m`, with a dedicated `/data` tmpfs for
  writable state. The generated code cannot reach the internet and has no host
  filesystem access.
* `PREVIEW_ALLOW_START` defaults on outside production and off in it.

## 8. Quality gates and risks

* Phase gates: baseline recorded → stub graph valid → app boots → loop terminates → planted bugs found (3/3) → results table published.
* Risks: loops→caps+`unresolved`; wrong test→triage+spec-only+hand spot-check;
  unsafe code→Docker no-net + cap-drop + no-new-privileges; cost→logging+small models; luck→3× runs report
  spread; scope creep→REST-only.
* **Wrong-domain generation** → domain coverage gate + `domain_missed`.
* **Failed generation silently substituted** → no templates; `blocked` carries
  the per-backend errors.
* **A bad UI failing a good run** → the UI verdict is advisory; no edge from
  `ui_gate` to a terminal status; the Reviewer pins UI findings to `minor`.
* **Generated code running with platform credentials** → keys stripped from the
  child env, container isolation, no egress, TTL + concurrency caps,
  `/preview` auth-required.
* **Truncation silent** → truncation is loud, recorded, and counted as
  `truncations`; per-role ceilings prevent over-spend.