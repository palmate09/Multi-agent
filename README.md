# Multi-Agent Software Team

5 LLM agents turn plain English into a tested, reviewed FastAPI REST API — and,
if you ask, a static web UI for it that you can click on and use.

Bring your own model: a Gemini API key (a run takes 1-2 minutes) or local
Ollama (free and private, but 3-6 minutes on CPU). There is **no template
fallback** — if generation fails the run reports `blocked` and says why, rather
than quietly substituting a built-in answer.

Ships with a web UI (React + Vite behind nginx), a FastAPI backend that streams
agent progress over SSE, a live playground that boots the generated app in a
hardened container and serves it at `/preview/<run_id>/`, Docker images for
both, and a CI/CD pipeline that deploys to a GCP VM.

## Architecture

```
Requirement -> PM (stories) -> Reasoner (plan: approach/risks/edge cases)
  -> Designer (OpenAPI + file plan) -> Developer
  -> domain coverage gate -> boot check -> Tester || (developer self-checks)
  -> Sandbox Runner (container, internal net) -> triage+reflection -> Developer
  -> UI designer -> UI developer -> UI gate (advisory)      [opt-in]
  -> Reviewer (ruff+bandit) -> done
```

```
browser ──> Caddy ──> nginx (SPA + /api + /preview proxy) ──> FastAPI ──> agents
                                                                 ├──> sandbox sidecar  (owns docker.sock; the ONLY service
                                                                 │      on sandbox-net, so it can both start and reach
                                                                 │      the containers)
                                                                 │      ├─> pytest container   (sandbox-net, internal, no egress)
                                                                 │      └─> preview container  (sandbox-net, internal, no egress)
                                                                 ├──> Postgres (inner net; runs index, sessions, rate limits)
                                                                 └──> Ollama (optional, inner net)

The backend is deliberately NOT on `sandbox-net`: that keeps the platform's own
API out of reach of model-generated code. The sidecar spans both networks, so
preview traffic is relayed through it (`/sandbox/proxy/<id>/…`) rather than
dialled directly.

## Running it

```bash
./start.sh            # build if needed, start, wait until healthy, print URLs
./start.sh --db       # also start Postgres (revocable sessions, durable limits)
./start.sh --rebuild  # force a rebuild of every image
./start.sh status     # what is running and whether it is healthy
./start.sh logs       # tail every service
./start.sh check      # preflight only, changes nothing
./start.sh down       # stop and remove the containers
```

`start.sh` does what `docker compose up` cannot: it detects the Docker socket's
group and passes it to the sidecar (which is refused the socket without it),
builds the sandbox image up front, waits for real HTTP responses rather than
"Up", and verifies the sidecar can actually start a container.

See `ARCHITECTURE.md`, `EXECUTION_PLAN.md`, `docs/DEPLOY.md`. Original brief: `docs/`.

## Requirements

One model backend. There is no offline fallback any more, by design: the
previous template mode graded a task-manager template with a task-manager test
suite, so a library requirement still reported `accepted`.

**Groq (recommended)** — paste a key from <https://console.groq.com/keys>:

```bash
cp .env.example .env          # add GROQ_API_KEY, keep LLM_PROVIDER=groq
python backend/evals/check_llm.py     # validates the key and prints the model serving
```

**Ollama (local, no key)**

```bash
ollama pull qwen2.5-coder:7b-instruct-q4_K_M   # Designer
ollama pull qwen2.5-coder:3b                    # developer/tester/reviewer/pm
# then set LLM_PROVIDER=local and SKIP_OLLAMA=0 in .env
```

## Quickstart

```bash
python3 -m venv .venv && . .venv/bin/activate   # `python` alone is not on PATH
pip install -r backend/requirements-dev.txt       # dev file: adds pytest/ruff/bandit
pytest -q                                       # offline suite, no model needed
python cli.py --run "a library API that lends books" --out outputs/demo --live
python cli.py --eval
```

`backend/requirements.txt` is the runtime set the backend image installs;
`requirements-dev.txt` is that plus pytest/ruff/bandit for the suite and for
`SANDBOX_MODE=subprocess`. In the default container mode the generated code's
pytest/ruff/bandit live in the sandbox image instead, so the backend image
carries none of them.

`./run_all.sh` runs every gate in order and prints a pass/fail table. Pass
`--llm` to include the generation gates, which use whichever provider
`$LLM_PROVIDER` names.

Measured wall time for a 3-endpoint API:

| Provider | Wall time (4-endpoint API) |
|---|---|
| Groq (`qwen/qwen3.8-27b`) | ~45-100 s |
| Gemini (`gemini-3-flash-preview`) | ~100-160 s |
| Ollama 3b on 4 CPU cores | ~350 s |

The gap is throughput: the local models manage ~12 tok/s (3b) and ~6.5 tok/s
(7b) without a GPU, and a full app is 800-2000 tokens.

## Web UI

```bash
./deploy/bootstrap-env.sh
docker compose --env-file .env -f deploy/docker-compose.prod.yml up -d --build
open http://localhost          # sign in with the password it printed
```

For hot-reloading development (Vite dev server, API on `:8000`):

```bash
docker compose up            # UI on :5173
```

Change a lost password with `./deploy/bootstrap-env.sh --reset-password`, then
recreate the backend container.

## How a run can end

| Status | Meaning |
|---|---|
| `accepted` | tests green, review found no blockers |
| `accepted_no_review` | tests green, reviewer ablated for this run |
| `unresolved` | retry budget exhausted with tests still failing, or the app would not import |
| `unresolved_review` | review blockers survived the retry budget |
| `blocked` | an agent could not produce a valid result; `error` names the cause |
| `domain_missed` | the code does not implement the requested domain |

`blocked` and `domain_missed` are the two statuses the old design could not
produce. They exist so that "accepted" means the run actually did what you
asked.

## The domain coverage gate

The defect this project is built to avoid: a library requirement produced a task
manager, its self-written tests passed, and the run reported `accepted`.

Before any test is graded, `agents/coverage.py` extracts the subject matter from
your requirement and checks the generated code actually reflects it:

```
requirement: library that lends books, POST /loans, GET /books, ISBN
code contains: Book, Loan, /loans, /books, isbn   -> covered
```

Below a 60% match the run ends `domain_missed`. A requirement too vague to
extract terms from (fewer than 2) is treated as inconclusive and never blocks.

## Generated file layout

The Designer chooses the module layout from the requirement's complexity and the
Developer emits exactly those files. Nothing is fixed: a 3-endpoint app may be a
single module, while a larger one gets `models.py`, `schemas_pyd.py` and routers.

The entrypoint is **discovered from the code** by AST inspection
(`agents/introspect.py`) rather than assumed to be `main.py`, so `app.py`,
`server.py` and nested packages all work.

## Full-stack runs and the playground

Tick **Generate a web UI** (or pass `--ui`) to add three nodes after the
backend tests are green:

```
ui_designer -> ui_developer -> ui_gate
```

They emit a **static, no-build** UI — `index.html`, `app.js`, `styles.css` —
bound to the endpoints the generated API actually declares. There is no React,
no npm and no bundler in the loop.

Two things are checked rather than merely requested:

* **Relative URLs.** The UI is served at `/preview/<run_id>/`, so
  `fetch('/books')` would hit the *platform's* API instead of the sandboxed one.
* **No external origins.** The preview sandbox has no network, so a CDN link
  renders a half-styled page.

The gate's verdict is **advisory**: a broken UI is reported on the UI tab and
folded into the review as `minor` findings, but it never changes the run's
terminal status. A green, tested, reviewed backend run stays green.

Then the **Playground** tab boots the generated app in a **hardened sandbox
container** on the internal `sandbox-net` network and reverse-proxies it at
`/preview/<run_id>/`:

```
GET  /api/runs/{rid}/playground         -> {state, url, host, mode, error, log_tail}
POST /api/runs/{rid}/playground/start   -> 202, idempotent
POST /api/runs/{rid}/playground/stop
ANY  /preview/{rid}/{path:path}         -> reverse proxy to container IP
```

* **Container isolation**: the preview runs on `sandbox-net` (`internal: true`),
  with `--read-only`, `--cap-drop ALL`, `--security-opt no-new-privileges`,
  `--pids-limit 256`, `--memory 512m`, `--cpus 1`, `--user 10001`, and a
  dedicated `/data` tmpfs for writable state. The generated code cannot reach
  the internet and has no host filesystem access.
* **No shared Docker socket**: the backend never sees `docker.sock`. A dedicated
  **sandbox sidecar** owns the socket and exposes a minimal 2-endpoint API
  (`POST /sandbox/runs`, `DELETE /sandbox/runs/{id}`) with image allowlist,
  mount-root confinement, and credential stripping.
* **Database-backed sessions**: PostgreSQL stores revocable sessions, durable
  login attempt counts, run events, and file indexes. Logout actually revokes;
  a fresh limiter blocks after 3 failures.
* Health probe: exponential backoff from 250 ms polls the spec's declared paths
  for HTTP 200 within `PREVIEW_BOOT_TIMEOUT_SECONDS` → `running`, else
  `crashed` with log tail. `ever_booted` is sticky across stops/restarts.
* TTL reap (default 1800s idle) and concurrency cap (`SANDBOX_MAX_CONCURRENT=4`)
  bound resource use. Crash-restart recovers on the next proxy request.

> The playground **is isolation** — a hardened container on an internal network
> with no egress, no host filesystem access, and a dedicated sidecar mediating
> Docker. This replaces the previous subprocess model.

## Choosing a backend

`LLM_PROVIDER` selects the strategy:

| Value | Behaviour |
|---|---|
| `groq` | Groq only (recommended) |
| `gemini` | Gemini only |
| `github` | GitHub Models only — free with a GitHub token |
| `openrouter` / `huggingface` / `openai` | that provider only |
| `local` | Ollama only, never the network |
| `auto` | every hosted tier in turn |

Naming one provider matters on a free tier: `auto` would spend a request on
each provider you have no key for before reaching the one you configured.

Check what is actually serving requests:

```bash
$ python backend/evals/check_llm.py
tiers to try: ['groq/free']
smoke call: model=groq/qwen/qwen3.8-27b ok=True latency=0.08s
```

Three failure modes that all look alike, and need opposite fixes:

* **HTTP 403 from Groq** means either a decommissioned model id *or* a rejected
  key — Groq does not use 404. The current ids, verified against
  `GET /openai/v1/models`, are `qwen/qwen3.8-27b`, `openai/gpt-oss-20b` and
  `openai/gpt-oss-120b`.
* **HTTP 404 from Gemini** means the model id is retired; `gemini-2.5-flash` and
  `gemini-2.0-flash` both now fail this way for new accounts. Hence
  `GEMINI_MODELS` is an ordered list rather than one pinned id.
* **HTTP 429** means the free quota is spent and resets daily.

`llm.py` sends an explicit `User-Agent` because Cloudflare — which fronts Groq —
rejects urllib's default agent with `403 error code: 1010`, which is
indistinguishable from a bad key. If you ever see that exact code, it is the
user agent, not your key.

Per-role model routing applies to the local path, because the roles have very
different appetites:

```bash
OLLAMA_MODEL_DESIGNER=qwen2.5-coder:7b-instruct-q4_K_M   # structured YAML quality
OLLAMA_MODEL_DEVELOPER=qwen2.5-coder:3b                 # ~2x faster on CPU
```

If no backend answers, the run is `blocked` with the per-backend errors in
`GraphState.error`. Every call is logged to `$RUNS_DIR/llm_log.jsonl` with its
latency and the reason each fallback declined.

### Reasoning

Some roles are allowed to think before they answer. The thinking is returned
separately — in `meta["reasoning"]` and in the call log — so a `<thinking>` block
can never end up in front of the YAML the Designer parses.

```bash
LLM_REASONING_ROLES=pm,reasoner,designer,developer,tester,triage,reflection  # reviewer is off
LLM_REASONING_EFFORT=medium        # clamped to what the serving model accepts
LLM_REASONING=0                    # kill switch for the whole pipeline
LLM_REASONING_ROLE_REVIEWER=1      # add one role back
```

`GROQ_REASONING_MODEL` / `OPENROUTER_REASONING_MODEL` lead the roster with a
reasoning-capable id when such a role asks (`gpt-oss`, `qwen3` otherwise).
Gemini gets `thinkingConfig` with `LLM_REASONING_BUDGET` (or
`GEMINI_THINKING_LEVEL` on 3.x). Every call's ceiling is doubled while
reasoning, because thinking is billed against the answer's budget — a cap sized
for the answer alone comes back as empty content.

### The plan node

Between the PM and the Designer, the Reasoner writes a plan — approach,
decisions, risks, edge cases, open questions — to `outputs/<run>/reasoning.json`
and hands it to the Designer and Developer as context. It is advisory: the
contract and the tests are still what a run is graded on, and neither the Tester
nor the Reviewer sees it.

```bash
python cli.py --run "..." --no-reasoner     # ablate it
curl -X POST /api/runs -d '{"requirement":"...","skip_reasoner":true}'
```

In the web UI it is the **Plan** tab beside *Stories* (approach, decisions,
risks, edge cases, open questions), an **Ablate reasoner** checkbox on the form
alongside the other ablations, and a labelled step in the pipeline timeline
showing its risk and edge-case counts.

## Token ceilings (Phase 1 fix)

The old design hardcoded `GEMINI_MAX_OUTPUT_TOKENS=8192` (and the same flat
number for Groq/OpenRouter/HF/OpenAI), which routed **every** reasoning call
through a 16,384-token ceiling — including the one-word `triage` verdict and
two-sentence `reflection`. This was silently expensive.

Now:

* Per-role ceilings from `_NUM_PREDICT` (`triage=500`, `reflection=800`,
  `designer=4000`, `developer=4000`, `tester=4000`, `pm=2000`, `reviewer=2000`,
  `reasoner=2000`).
* **Hosted floor** `_HOSTED_FLOOR = {designer: 8000, developer: 8000, tester: 8000}`
  raises only the three genuinely long-output roles on hosted paths.
* Overrides, most specific first: `LLM_MAX_TOKENS_<ROLE>`, `LLM_MAX_TOKENS`,
  then provider-specific (`GEMINI_MAX_OUTPUT_TOKENS`, `GROQ_MAX_TOKENS`, etc.).
* Truncation is **loud and counted**: `finishReason=MAX_TOKENS` (Gemini),
  `finish_reason=length` (OpenAI-compatible), `done_reason=length` (Ollama)
  are recorded as failures naming the role and ceiling, and counted as
  `truncations` in the perf harness.

```bash
# Example: widen only the Developer if its multi-file answers are truncated
LLM_MAX_TOKENS_DEVELOPER=12000

# Or shrink reasoning spend for a specific role
LLM_REASONING_BUDGET_DEVELOPER=1000
```

## Results

Two unrelated domains, both generated from a plain-English prompt with no
task-manager template anywhere in the codebase:

Four unrelated domains, each generated from a plain-English prompt with no
task-manager template anywhere in the codebase:

| Run | Requirement | Status | Tests | Spec coverage | Retries | Wall |
|---|---|---|---|---|---|---|
| verify_recipes | `/recipes` CRUD, 404 + blank-title 422 | accepted | 9 passed | 4/4 | 0 | 46 s |
| verify_library | `/loans`, `/books`, ISBN 404 | accepted | 5 passed | 3/3 | 1 | 73 s |
| verify_inventory | warehouse stock, `/products`, SKU 404/422 | accepted | 10 passed | 4/4 | 0 | 99 s |
| verify_final | book club, `/members`, `/meetings` | accepted | 9 passed | 4/4 | 1 | 127 s |

Zero occurrences of `task` in any generated file. Two of the four runs needed a
repair cycle: one failed to import (the boot-repair loop fixed it), one had no
`create_all` so every request hit `no such table` (the Tester caught it and the
Developer patched it). So the repair loops do real work rather than reporting
zeros. `spec_coverage` compares the endpoints the spec declares against the
routes the code exposes — it is not a substring count.

The generated file layout varies with the requirement: the recipe and library
APIs came back as `main.py` + `requirements.txt`, while the inventory and book
club APIs were split into `database.py`, `models.py`, `schemas.py`, `main.py`.
Nothing about that split is hardcoded.

Regenerate with `python cli.py --eval`. Per-run detail is in
`outputs/<run>/summary.json`.

**Caveats:**

* A run needs a working backend and quota. When the quota is spent the run ends
  `blocked` with the reason, rather than reporting a green result it did not earn.
* Test counts vary with how much the Tester can exercise: a spec with no
  `POST /books` leaves the loan-creation test without a fixture, and such a test
  is skipped, i.e. unverified.

## Deployment

Push a `v*` tag: CI builds both images, pushes to GHCR, and rolls the VM over
SSH. Full walkthrough in **`docs/DEPLOY.md`** (GCP VM creation, firewall,
bootstrap, secrets, TLS without a domain, operating and backing up).

## Layout

```
backend/     FastAPI app, agents, graph, schemas, sandbox, evals, tests
frontend/    React + Vite + TypeScript SPA, nginx runtime image
deploy/      production compose, Caddyfile(s), GCP VM bootstrap script
docs/        original brief + DEPLOY.md
cli.py       headless CLI
run_all.sh   run every phase gate in order
```

## Limits

* **Requires a local model.** With no backend the run is `blocked`; that is
  deliberate, but it means there is no offline demo.
* A 3b model on CPU makes real mistakes on complex specs. The fix loop is
  genuine but bounded at 4 developer retries; failures end `unresolved` with
  the pytest output attached.
* The coverage gate is lexical. It proves the domain vocabulary is present, not
  that the behaviour is correct — the tests and reviewer cover that.
* SWE-bench Lite is a stretch goal, not yet run.
* The UI is plain static HTML/JS/CSS by decision — no React, Vite or npm build
  in v1, and no user-triggered code editing from the browser.
* WebSockets are not proxied to a preview (501 by design).
* One shared operator account, not per-user accounts.
* Production runs the sandbox as a **hardened container on an internal network**,
  not a subprocess. The sandbox sidecar owns `docker.sock` and enforces image
  allowlist, mount confinement, and credential stripping.
* **Database-backed sessions** (PostgreSQL optional; degrades to in-memory when
  `DATABASE_URL` is unset). Logout revokes; rate limits persist across restarts.
* **Per-run schemas** in Postgres: each run's tables live in `run_<sha256>` with
  `search_path` pinned in the connection URL, so generated code never sees
  another run's data.