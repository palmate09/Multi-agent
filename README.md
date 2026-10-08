# Multi-Agent Software Team

5 LLM agents turn plain English into a tested, reviewed FastAPI REST API.

Bring your own model: a Gemini API key (a run takes 1-2 minutes) or local
Ollama (free and private, but 3-6 minutes on CPU). There is **no template
fallback** — if generation fails the run reports `blocked` and says why, rather
than quietly substituting a built-in answer.

Ships with a web UI (React + Vite behind nginx), a FastAPI backend that streams
agent progress over SSE, Docker images for both, and a CI/CD pipeline that
deploys to a GCP VM.

## Architecture

```
Requirement -> PM (stories) -> Designer (OpenAPI + file plan) -> Developer
  -> domain coverage gate -> boot check -> Tester || (developer self-checks)
  -> Sandbox Runner -> triage+reflection -> Developer -> Reviewer (ruff+bandit) -> done
```

```
browser ──> Caddy ──> nginx (SPA + /api proxy) ──> FastAPI ──> agents
                                                           └──> pytest sandbox
```

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
pip install -r backend/requirements.txt
pytest -q                                       # offline suite, no model needed
python cli.py --run "a library API that lends books" --out outputs/demo --live
python cli.py --eval
```

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
* One shared operator account, not per-user accounts.
* Sessions are stateless, so logout clears the cookie but a copied cookie stays
  valid until it expires.
* Production runs the sandbox as a local subprocess, so generated code executes
  with the API's network access and no isolation. Auth is not a substitute for
  sandboxing.