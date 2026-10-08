# Multi-Agent Software Team

5 LLM agents turn plain English into a tested, reviewed FastAPI REST API.
Local-first: Ollama primary, free cloud fallback, template mode offline.

Ships with a web UI (React + Vite behind nginx), a FastAPI backend that streams
agent progress over SSE, Docker images for both, and a CI/CD pipeline that
deploys to a GCP VM.

## Architecture

```
Requirement -> PM (stories) -> Designer (OpenAPI) -> Developer || Tester
  -> Sandbox Runner -> triage+reflection -> Developer -> Reviewer (ruff+bandit) -> done
```

```
browser ──> Caddy ──> nginx (SPA + /api proxy) ──> FastAPI ──> agents
                                                           └──> pytest sandbox
```

See `ARCHITECTURE.md`, `EXECUTION_PLAN.md`, `docs/DEPLOY.md`. Original brief: `docs/`.

## Quickstart

Everything at once (creates a venv, runs every phase gate):

```bash
./run_all.sh            # template mode, offline, ~20s
./run_all.sh --llm      # use local Ollama (slow on CPU)
./run_all.sh --docker   # sandbox tests in Docker
```

Manually:

```bash
python3 -m venv .venv && . .venv/bin/activate   # `python` alone is not on PATH
pip install -r backend/requirements.txt
pytest -q
python cli.py --run "task manager" --out outputs/demo
python cli.py --eval
```

## Web UI

One command. It generates credentials if `.env` is missing, prints the
password once, and starts the stack:

```bash
./deploy/bootstrap-env.sh
docker compose --env-file .env -f deploy/docker-compose.prod.yml up -d --build
open http://localhost          # sign in with the password it printed
```

`bootstrap-env.sh` is idempotent — it keeps any credentials you already have
and only fills in what is missing.

For hot-reloading development instead (Vite dev server, API on `:8000`):

```bash
docker compose up            # UI on :5173
```

Dev leaves auth **off**, even if `deploy/bootstrap-env.sh` has written a
production `.env` in the repo root (Compose loads that file automatically). To
exercise the login flow locally, opt in explicitly:

```bash
DEV_AUTH_USERNAME=admin \
DEV_AUTH_SECRET=$(python3 -c 'import secrets;print(secrets.token_urlsafe(48))') \
DEV_AUTH_PASSWORD_HASH=$(python3 backend/scripts/gen_auth.py 'dev-pass' | sed -n 's/^AUTH_PASSWORD_HASH=//p') \
  docker compose up
```

Change a lost password with `./deploy/bootstrap-env.sh --reset-password`, then
recreate the backend container.

### If nothing shows up

```bash
docker compose --env-file .env -f deploy/docker-compose.prod.yml ps   # all three must be Up
docker compose --env-file .env -f deploy/docker-compose.prod.yml logs --tail=50 frontend
curl -s localhost/health
```

| Symptom | Cause | Fix |
|---|---|---|
| `error while interpolating ... AUTH_SECRET is missing` | no credentials in `.env` | run `./deploy/bootstrap-env.sh` |
| Containers never created | compose refused to start | read the error above; it names the missing key |
| `frontend` restarting, `host not found in upstream "backend"` | services on different networks | both must be on the `edge` network |
| SPA loads but every API call 401s | not signed in, or expired session | sign in again; check `AUTH_SESSION_HOURS` |
| `bind: address already in use` | port 80 or 5173 taken | `./deploy/stop.sh`, or change `SITE_ADDRESS` |
| Works locally, blank from another machine | firewall | open the port, see `docs/DEPLOY.md` §1 |

## Authentication

Session-cookie login with a scrypt-hashed password, plus an API key for
headless access. Every `/api/*` route and the UI are gated; only `/health` and
the auth endpoints are public.

`deploy/bootstrap-env.sh` handles this for you. To manage credentials by hand:

```bash
python backend/scripts/gen_auth.py     # prints hash + secret + API key
```

Paste the output into `.env` (or the VM's `/opt/multi-agent-team/.env`).
Production compose refuses to start without `AUTH_SECRET` and
`AUTH_PASSWORD_HASH`. Details, rotation and CSRF behaviour: `docs/DEPLOY.md` §4a.

## LLM backends

Local Ollama first, then free hosted tiers, then deterministic templates.
Template mode is always the fallback and needs nothing.

```bash
ollama pull qwen2.5-coder:7b-instruct-q4_K_M   # developer/tester/reviewer
ollama pull qwen2.5-coder:3b                    # pm/triage/reflection
cp .env.example .env                            # add GEMINI_API_KEY etc.
```

`SKIP_OLLAMA=1` skips local models entirely — useful on machines where Ollama is
not installed, otherwise every call burns the probe timeout.

On CPU-only hardware the 7B q4 model measures **5.6 tok/s** (4 vCPU), so the
90-second per-call timeout in `agents/llm.py` caps it near 500 tokens and long
generations silently fall back to templates. Prefer the 3B on CPU, or a hosted
tier for the heavy roles. See `docs/DEPLOY.md` §7 for measured numbers.

## Results

| Run | Status | Pass rate | Spec coverage | Attempts |
|---|---|---|---|---|
| eval_task-manager | accepted | 1.0 | 1.0 | 0 |
| eval_task-manager-filter | accepted | 1.0 | 1.0 | 0 |
| eval_task-manager-notes | accepted | 1.0 | 1.0 | 0 |
| baseline (single-agent) | 7/7 pass | 1.0 | — | — |
| abl_no_tester | accepted | 1.0 | — | 0 |
| abl_no_reviewer | accepted_no_review | 1.0 | — | 0 |

**Read this honestly:** `attempts = 0` across the board means the fix loop,
triage and reflexion never executed. In template mode the Developer emits a
known-good bundle and the Tester emits a matching suite, so the first `pytest`
is always green and the Reviewer sees only clean static checks. These numbers
are a regression harness, not evidence that the multi-agent loop beats a single
agent. `docs/DEPLOY.md` §9 lists the other limits, including that the sandbox
runs generated code without isolation when Docker is unavailable.

## Deployment

Push an `v*` tag: CI builds both images, pushes to GHCR, and rolls the VM over
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

* Template mode covers task-manager CRUD only; novel domains need an LLM backend.
* SWE-bench Lite is a stretch goal, not yet run.
* One shared operator account, not per-user accounts.
* Sessions are stateless, so logout clears the cookie but a copied cookie stays
  valid until it expires.
* Production runs the sandbox as a local subprocess, so generated code executes
  with the API's network access and no isolation. Auth is not a substitute for
  sandboxing.