# Improvement Plan — Containerised Sandbox, Platform Database, and Latency

> Status: Phases 1-5 implemented. 439 tests pass, ruff clean, container preview
> and Postgres-backed sessions verified against a live daemon. Docs updated.
> **Outstanding: `evals/perf_baseline.json` is not captured** — see §10.1. The
> harness is verified; only the measured numbers are missing.

> Decisions locked: `internal: true` compose network for the sandbox · database is an index, the filesystem stays the artifact store · the Docker socket lives in a sidecar, never in the backend · phased, each phase independently verifiable.

Supersedes nothing. Builds on `FULLSTACK_PLAYGROUND_PLAN.md` (already
implemented) and fixes what it left running as a plain subprocess.

## 0. Background: where things stand

The pipeline generates a tested, reviewed FastAPI backend, and — opt-in — a
static web UI that a live playground boots and serves at `/preview/<run_id>/`.
That playground runs the generated app as an **ordinary subprocess on the host**
with the platform's filesystem, network and CPU. The documented caveat is that
it is "not isolation", mitigated by credential stripping, a loopback bind, and
a TTL plus concurrency cap.

This plan closes that gap properly and fixes the latency and image-weight
problems found while verifying the playground.

```
today                                  after
──────────────────────────────────     ──────────────────────────────────────────
backend container                      caddy -> frontend -> backend -> postgres
  runs pytest as a subprocess            |
  SANDBOX_USE_DOCKER=false (dead)      sandbox sidecar (owns docker.sock)
                                         └─ spawns pytest AND preview containers
preview = Popen(uvicorn) on the host
langgraph + 47MB of unused deps
467MB image, 175KB single JS bundle
```

### 0.1 Why the sandbox work is the priority

The preview is the largest untrusted-code exposure in the system. It executes
model-generated code for the lifetime of a user's session, with the platform's
filesystem and network reachable. Credential stripping and a loopback bind
reduce the blast radius; they do not remove it. The pytest path already has a
Docker branch with `--network none` — it is simply disabled by default.

The gap is therefore not "there is no sandbox image". There is one. It is
unused, unhardened, and does not cover the case that matters most.

## 1. Verified findings

Everything in this section was measured on this machine, not inferred. The
latency conclusions that shape the phasing depend on §1.1–§1.3 being true.

### 1.1 The system is LLM-bound, not CPU-bound

Per iteration of the fix loop, on the local (non-Docker) sandbox:

| Operation | Cost |
|---|---|
| `run_tests_local` (pytest) | 1.58 s |
| `boot_check` (import the app) | 0.71 s |
| `static_analysis` (ruff + bandit) | 0.23 s |
| **Total** | **~2.5 s** |

Against a full run: `verify_recipes` 45.7 s, `verify_library` 72.6 s,
`verify_inventory` 99.2 s, and the full-stack Gemini run 384 s.

**Consequence:** optimising local CPU, imports, or Docker startup buys at most
a couple of seconds. Latency work has to target *LLM call count and token
budget*. Anyone proposing "make pytest faster" as the latency fix has not read
§1.1.

Breakdown of the 1.58 s pytest run: interpreter start 0.01 s, `--collect-only`
alone 1.52 s, importing the app 0.69 s. So roughly 0.8 s is pytest's own
plugin and conftest overhead, and 0.7 s is SQLAlchemy import. Neither is worth
attacking.

### 1.2 Per-role token caps are ignored on every hosted call

`_NUM_PREDICT` (`llm.py:90`) defines per-role ceilings, and the correct ones
already exist:

| role | cap |
|---|---|
| `pm`, `reasoner`, `reviewer` | 2000 |
| `designer`, `developer`, `tester` | 4000 |
| `reflection` | 800 |
| `triage` | 500 |

They are used **only** by the Ollama path, via `role_num_predict(role)`. The
hosted paths hardcode their own value and ignore the table entirely:

* `_gemini` — `GEMINI_MAX_OUTPUT_TOKENS`, default **8192** (`llm.py:329`)
* `_groq` — `max_tokens=8192` (`llm.py:497`)

Worse, `_openai_compat` passes the budget through `_reasoning_max_tokens`,
which **doubles** it. Both `triage` and `reflection` are members of
`_DEFAULT_REASONING_ROLES`. So the two cheapest calls in the pipeline — the
ones that emit a one-word triage verdict and a 2–3 sentence reflection — are
requested with a **16,384-token ceiling**.

This is the largest single latency defect, it fires on every failed fix-loop
iteration, and it is a one-line fix. It is the first item in Phase 1.

### 1.3 `langgraph` is a dead dependency carrying 47 MB

`ARCHITECTURE.md` states plainly: *"No LangGraph. The orchestration is a plain
sequential function."* A grep for `langgraph` and `langchain` across
`backend/` and `cli.py` returns **zero imports**. It is nonetheless installed,
and its transitive closure is the single largest removable block in the image:

| package | size | needed by anything? |
|---|---|---|
| `zstandard` | 23 MB | no |
| `langsmith` | 9 MB | no |
| `langchain_core` | 6 MB | no |
| `langgraph` | 4 MB | no |
| `xxhash`, `requests`, `requests_toolbelt`, `orjson` | 5 MB | no |
| **total** | **47 MB** | — |

Site-packages is 137 MB total, so this is ~34% of installed Python and ~10% of
the 467 MB image.

### 1.4 The existing Docker sandbox is unused and unhardened

`docker-compose.yml` sets `SANDBOX_USE_DOCKER: 'false'`, so `run_tests` always
takes the local-subprocess branch. When the Docker branch *is* taken it:

* runs `docker build` **on every test cycle**, not once
* builds to a fixed shared tag (`agent-team-sandbox`), so concurrent runs race
* passes `--network none --memory 512m --cpus 1` (good) but **no** `--read-only`,
  `--cap-drop`, `--security-opt`, `--pids-limit`, or non-root `--user`
* runs as **root** inside the container

Against CIS Docker Benchmark v1.6 §4.1/§4.6, that is nine-plus findings.

### 1.5 Python version skew between app and sandbox

The backend image is `python:3.12-slim`; `backend/sandbox/Dockerfile` is
`python:3.11-slim`. Generated code using 3.12 syntax passes the app and fails
the sandbox, and vice versa a 3.11-only construct behaves differently in
production. One version, chosen deliberately.

### 1.6 Frontend ships one 175 KB bundle

`vite.config.ts` defines no `manualChunks`; the whole SPA is 175 KB raw /
55.6 KB gzip, loaded before first paint, including the eval table and both
new panels that most sessions never open. nginx has `gzip` but no `brotli`.

## 2. Standards this plan is validated against

| Area | Standard | Where it binds |
|---|---|---|
| Containers | CIS Docker Benchmark v1.6 | §6 runner flags, non-root, read-only FS |
| Container images | OCI Image Spec v1.1 | §5 sandbox Dockerfile, labels |
| Orchestration | compose-spec | §9, `internal: true`, healthchecks |
| Auth / sessions | OWASP ASVS 4.0 (V2, V3) | §8 `sessions`, `api_keys`, `login_attempts` |
| Database | PostgreSQL 16 hardening | SCRAM auth, no `trust`, least-privilege role |
| Browser | CSP Level 3, RFC 9113 | unchanged from the playground work |
| Config | Twelve-Factor (env only) | already conformant; no config files added |

Where a standard and convenience conflict, the standard wins and the exception
is written down here rather than left in a comment.

## 3. Key design decisions (and why)

### 3.1 The sandbox gets an `internal` network, not `--network none`

Two requirements are in direct conflict:

* generated apps should default to PostgreSQL (§8)
* the sandbox should have no network (§1.4's `--network none`)

A container with no network cannot reach a database. Picking either one breaks
the other.

**Decision:** the sandbox runs on a compose network with `internal: true`. That
blocks egress at the Docker network level — no route to the internet, so a
generated app cannot exfiltrate to a remote host — while still permitting
service-to-service traffic to the `postgres` container. This is *stronger* than
`--network none` for the exfiltration case and it is the only topology that
satisfies both requirements.

The browser is unaffected: it talks to the gateway, and the gateway proxies to
the container's published loopback port. The UI's own `fetch('recipes')` is
served by the backend, not by the sandbox.

### 3.2 The database is an index; the filesystem stays the artifact store

`reconstruct_state` implements resume by reading `spec.yaml`, `code/`, `ui/`,
`summary.json` and friends off disk, and `RunStore.rehydrate` rebuilds the run
list by scanning directories. That contract is load-bearing and is what makes
"resume after a crash" work at all.

Moving artifact *contents* into PostgreSQL would mean rewriting resume, restart
and rehydrate, and would put multi-megabyte code blobs in a database. So:

* **PostgreSQL** holds rows: users, sessions, runs, event logs, preview state,
  and a `run_files` index carrying `path`, `bytes`, `sha256`.
* **The filesystem** remains the durable record of artifact contents.
* A startup reconciliation compares the index against the disk and can rebuild
  the index from disk — which is exactly what `rehydrate` does today.

This keeps the existing resume semantics intact and keeps large blobs out of
the database. It also means losing Postgres loses metadata, not work.

### 3.3 The Docker socket goes in a sidecar, never in the backend

Phases 2 and 3 need to spawn containers. The naive way is to mount
`/var/run/docker.sock` into the backend container — which is **root-equivalent
on the host**: anyone who can reach that socket can start a container with the
host filesystem mounted. Putting it in the backend would hand the platform's
entire privilege to the component that runs model-generated code, which is
exactly the boundary this plan exists to build.

**Decision:** a minimal `sandbox` service owns the socket and exposes only two
endpoints over the internal network:

```
POST   /sandbox/runs    {image, cmd, env, mounts}  -> {id, port}
DELETE /sandbox/runs/{id}
```

No arbitrary image pull, no privileged mode, no host path passthrough — mounts
are restricted to the run directory. The backend becomes an HTTP client of a
narrow API.

### 3.4 The preview gateway contract does not change

`PreviewSession` keeps `.port` as an `int` and the gateway keeps proxying to
`127.0.0.1:<port>`. Only the *provenance* of that port changes: from
`_free_port()` + `Popen` to `docker run --publish 127.0.0.1::8000` followed by
reading the assigned port back from `docker inspect`.

That is the whole migration. It also **deletes the bind race** in `_free_port()`,
which today binds `:0`, closes, and hands the port to uvicorn — a few
microseconds of exposure that Docker assigns away entirely.

## 4. Phase 0 — Baseline harness

Nothing above is actionable until "faster" is falsifiable.

1. `backend/evals/perf.py`: run *N* identical requirement generations
   (default 3, over `backend/evals/requirements_suite.json`) and report per
   stage: `p50`/`p90` wall time, LLM calls, LLM seconds, sandbox seconds, dev
   retries, and per-role token usage.
2. Write `backend/evals/perf_baseline.json`.
3. Add a `Perf` row to `EvalPanel.tsx` only if it earns its place; Phase 1's
   claim is otherwise unfalsifiable and should not survive review.

**Gate:** `perf_baseline.json` exists; `pytest -q` green (336 at time of
writing).

## 5. Phase 1 — Latency and image weight, no behaviour change

The highest-value, lowest-risk phase. Nothing here changes what the pipeline
*decides* — only how long it takes and how big the image is.

### 5.1 Latency

| # | Change | Rationale |
|---|---|---|
| 1 | Pass `role_num_predict(role)` to `_gemini` and `_groq` instead of the hardcoded 8192 | §1.2. `triage` drops from a 16384 ceiling to ~500 |
| 2 | Run `triage` and `reflection` concurrently | `workflow.py:593-594` fires two independent LLM calls on the same `TestReport` back to back. The only genuinely independent pair in the pipeline |
| 3 | Skip `pip install` in the playground when every requirement already imports | Measured 0.52 s plus a subprocess, on every preview start |
| 4 | Health probe: 50 ms → 250 ms exponential backoff instead of fixed 250 ms | Fast boots stop paying a 250 ms floor |
| 5 | Cache the sandbox image by Dockerfile hash; stop rebuilding per test cycle | §1.4 |
| 6 | `pytest -p no:cacheprovider` in the local runner | ~0.1–0.2 s per run, and removes a write from the sandbox dir |

**On #1 specifically:** the risk is truncation. The existing `done_reason ==
"length"` detection in `_ollama` already surfaces truncation as a failure, and
it must be extended to the hosted paths so a too-small cap is loud rather than
silent. Keep generous floors for `designer`/`developer`/`tester`, whose output
is genuinely large.

**Deliberately rejected:** HTTP connection pooling. Measured TLS handshake is
**21.3 ms** (TCP alone 2.0 ms), so keep-alive saves ~0.2 s per run. Rewriting
the transport for that is a bad trade; if the transport is ever rewritten for
other reasons, pooling comes along free.

### 5.2 Weight

| # | Change | Saving |
|---|---|---|
| 1 | Remove `langgraph` from `backend/requirements.txt` | 47 MB |
| 2 | Uninstall `pip` from the runtime stage after install | 7 MB |
| 3 | Move `pytest`, `ruff`, `bandit`, `openapi-spec-validator` to the sandbox image | 5 MB from the app image |
| 4 | Align sandbox and backend on one Python version (§1.5) | correctness |
| 5 | Frontend: lazy-load `EvalPanel`, `PlaygroundPanel`, `UiPanel` | 175 KB → ~55 KB gzip initial |
| 6 | Enable brotli in `frontend/nginx/default.conf` alongside gzip | ~15% on text |

Expected: 467 MB → ~410 MB before Phase 2, lower after.

**Verification:** the Phase 0 harness shows p50 wall time down with **no change
in dev-retry distribution and no change in `spec_coverage`** — a speedup bought
by skipping work the pipeline needs would show up there immediately.

## 6. Phase 2 — Real sandbox image and a hardened runner

### 6.1 Image (`backend/sandbox/Dockerfile`, rewritten)

* `python:3.12-slim`, matching the backend (§1.5).
* Multi-stage: build wheels in one stage, install in the next, ship no
  compiler.
* Non-root `uid/gid 10001`, matching the backend's `appuser` convention.
* Pre-baked wheelhouse of what generated apps realistically import:
  `fastapi`, `uvicorn`, `sqlalchemy`, `pydantic`, `pydantic-settings`,
  `httpx`, `pyyaml`, `python-multipart`, `email-validator`, plus the test and
  lint tooling the runner drives.
* OCI labels: `org.opencontainers.image.source`, `.version`, `.licenses`.
* `PYTHONDONTWRITEBYTECODE=1`; precompile during build instead.

### 6.2 Runner flags (CIS-aligned)

```
--network <internal_net>       # §3.1: no internet, Postgres reachable
--read-only
--cap-drop ALL
--security-opt no-new-privileges:true
--pids-limit 256
--memory 512m --memory-swap 512m   # swap off, so memory pressure is a hard stop
--cpus 1 --cpuset-cpus 0
--user 10001:10001
--tmpfs /tmp:rw,noexec,nosuid,size=64m
-v <run_dir>:/app
```

`--read-only` plus an explicit `tmpfs` is why the memory *and* filesystem
limits are both needed: a generated app that creates its SQLite file at import
needs somewhere writable, and `/tmp` on tmpfs is where that goes.

**New setting:** `sandbox_max_concurrent` (default 4), so a burst of failed runs
cannot fork-bomb the host.

**Also fixed:** the shared image tag and the per-cycle rebuild (§1.4, §5.1#5).

### 6.3 The wheelhouse consequence

With no network, a generated app importing anything outside the wheelhouse
fails to boot. That is the intended trade — a prebuilt image is the only
*actually* strong isolation available here — but it must be **legible**. The
`crashed` state and log tail already exist; the message must name the cause
("dependency `foo` is not in the sandbox wheelhouse") rather than leaving the
user to read an ImportError.

### 6.4 Gate

`pytest -q` green; new tests assert the exact argv; a deliberately escaping
fixture (writes outside `/app`, forks, opens a socket) is contained.

## 7. Phase 3 — Containerised preview

The hard phase, and the one the user-facing requirement lands on: **the
Playground tab must keep working exactly as it does.**

1. One image serves both roles. `pytest` for tests, `uvicorn` for the preview,
   selected by command — no second image to build, pull and keep in sync.
2. `PlaygroundService` gains a `sandbox` mode. `PreviewSession` swaps
   `proc: Popen | None` for `container_id: str | None`. Everything else —
   including the whole `/preview/{rid}/…` gateway, which serves `ui/` from disk
   and proxies the rest — is unchanged, per §3.4.
3. Port assignment comes from Docker (`--publish 127.0.0.1::8000`, read back
   via inspect), deleting the `_free_port()` race.
4. `SANDBOX_MODE=local|subprocess|container` so local development without a
   Docker daemon still works.
5. Crash-restart, TTL reap, and concurrency caps carry over unchanged.

### Gate

The Playground tab behaves identically against a container. Specifically: page
loads, assets load, relative API calls resolve, stop releases the container and
its port, and an idle preview is reaped.

## 8. Phase 4 — PostgreSQL, users, sessions

### 8.1 Schema

```
users            id, username (citext, unique), password_hash (scrypt),
                 is_admin, disabled_at, created_at, updated_at
sessions         id, user_id -> users, token_hash, csrf_hash, created_at,
                 expires_at, last_seen_at, ip, user_agent
api_keys         id, user_id, name, prefix, key_hash, scopes[],
                 last_used_at, revoked_at
login_attempts   id, ip, username, at, ok          -- durable rate limiting
runs             id, user_id, requirement, status, options jsonb,
                 created_at, updated_at, finished_at, error, summary jsonb
run_events       run_id, seq, node, phase, payload jsonb, ts
run_files        run_id, path, kind, bytes, sha256, created_at
preview_sessions run_id, container_id, image, port, state, ever_booted,
                 started_at, last_hit_at, error
conversations    id, user_id, title, created_at, updated_at
messages         id, conversation_id, role, content, run_id, created_at,
                 model, tokens_in, tokens_out
audit_log        id, user_id, action, target, at, meta jsonb
```

Three of these fix real weaknesses rather than adding features:

* **`sessions`** replaces stateless signed cookies. Today a copied cookie stays
  valid for 12 hours and logout cannot revoke it — the README says so. Hash-
  stored server-side sessions make logout real and enable per-device listing.
* **`login_attempts`** replaces the in-memory `RateLimiter`. It is a per-process
  `defaultdict`, so it resets on restart and does nothing across replicas —
  which matters the moment there is more than one backend container.
* **`run_events`** replaces the in-memory `deque(maxlen=500)`. Today a backend
  restart loses the event history and a reconnecting SSE client sees nothing.
  In a table, replay survives restarts and survives `--workers > 1`.

### 8.2 Generated apps default to PostgreSQL

Per-run **schema** (or database), never a shared one — a shared database means
one run can read another's tables, which would break the independence the rest
of the system depends on. The sidecar passes a scoped `DATABASE_URL` into the
container env with credentials for that schema only, and drops them when the
container is reaped.

Generated code currently creates SQLite tables at import. That must become a
no-op under PostgreSQL, or every generated app fails to boot. The Developer SOP
needs a line for it, and the sandbox needs the driver pre-baked (§6.1).

### 8.3 Scope flag

`conversations` and `messages` imply **chat**, which this application does not
have — it is requirement-in, run-out. That is a new capability (UI plus an
agent loop), not a data migration, and folding it into Phase 4 would make that
phase untestable.

**Recommendation:** ship the schema now so nothing has to be redesigned later,
and deliver chat as its own phase. Flagging rather than silently expanding
scope.

### 8.4 Migration

`RunStore` moves from reading `summary.json` to querying `runs`, but
`reconstruct_state` keeps reading the filesystem (§3.2). Cookies change format
once; document the one-time re-login.

## 9. Phase 5 — Compose topology

```
                       internet
                          │
                     ┌────▼────┐  TLS, frame headers
                     │  caddy  │
                     └────┬────┘
                     ┌────▼────┐  SPA + /api + /preview
                     │frontend │  (nginx)
                     └────┬────┘
                     ┌────▼────┐  the app; NO docker.sock  (§3.3)
                     │ backend │
                     └──┬───┬──┘
        internal network  │   │
              ┌───────────┘   └────────┐
       ┌──────▼──────┐            ┌──────▼──────┐
       │  postgres   │            │  sandbox   │  owns docker.sock
       │             │◄───────────│  sidecar    │  POST/DELETE /sandbox/runs
       └─────────────┘  per-run   └─────────────┘
                        schema
```

`internal: true` on the network carrying backend ↔ postgres ↔ sandbox (§3.1).
`sandbox` is the only service with a socket mount.

`deploy/docker-compose.prod.yml` currently runs an `ollama` service. If hosted
tiers are in use that is a large idle container; it should become a profile.

### Gate

Compose boots clean, `docker.sock` appears in exactly one service, full suite
green, and the Phase 0 harness is unchanged — this phase is topology only and
must not move the latency numbers.

## 10. Tests (all offline, no LLM)

| File | Covers |
|---|---|
| `test_sandbox_runner.py` | exact argv for every CIS flag; image-cache hit/miss; concurrency cap; non-root uid; a fixture app that tries to escape and cannot |
| `test_sandbox_sidecar.py` | mount path restricted to the run dir; rejects privileged mode and host path passthrough; image allowlist |
| `test_preview_container.py` | port read back from inspect; gateway contract unchanged; stop releases the container; TTL reap; crash-restart |
| `test_sessions.py` | logout revokes; expiry; API key scoping; login attempts survive restart |
| `test_perf.py` | token caps reach the hosted path; truncation is detected, not silent |
| regression | all 336 existing tests stay green |

### 10a. Status of the above

All five files exist and the whole suite is green (**439 passed, 8 skipped, 1
deselected**, `ruff check backend/` clean). Two deviations from the table, both
deliberate:

* `test_sessions.py` is split into two tiers. The offline tier runs everywhere;
  the durable tier (real revocation, attempts surviving a restart, the
  `(ip, at)` index) needs a real database, because a fake that reimplements the
  queries would only be testing itself. It runs when `TEST_DATABASE_URL` is set
  and skips otherwise — **27 passed against a live Postgres 16**.
* `test_preview_container.py` covers spawn, gateway contract, stop and
  crash-restart. The TTL row is not duplicated here: the reaper enforces a 30 s
  floor (`max(30, ttl)`) so a zero-TTL test would assert nothing, and the reaper
  logic is already covered by the subprocess tests. 6 passed against real
  containers on the internal network.

### 10b. Outstanding: `evals/perf_baseline.json`

**The Phase 0 baseline is not captured.** The harness itself is verified — its
`aggregate`/`compare` path correctly reports *"faster: p50 wall −41.0%"* when
the controls hold, and *"REGRESSION: control moved"* when `dev_retries` or
`spec_coverage` shifts, which is the property that makes a speedup claim
falsifiable rather than self-fulfilling.

Capturing it costs ~3 real LLM runs and **must** be done on the pre-change tree,
because a baseline measured after the Phase 1 changes would compare the new code
against itself:

```bash
git worktree add /tmp/pre-phase1 451e332          # last pre-plan commit
cd /tmp/pre-phase1 && <venv>/bin/python backend/evals/perf.py \
    --repeats 3 --write "$PWD/perf_baseline.json"
git worktree remove /tmp/pre-phase1
```

Until that exists, **no latency claim in this document is measured.** The
non-LLM improvements (weight, containment, `pip`-skip, probe backoff) are
verified independently and stand on their own.

## 11. Risks

| Risk | Mitigation |
|---|---|
| Token-cap reduction truncates real output | Keep generous floors for `designer`/`developer`/`tester`; extend `done_reason == length` detection to the hosted paths; measure against the Phase 0 baseline |
| Generated app needs a dependency outside the wheelhouse | Pre-baked wheelhouse; the `crashed` message names the missing package instead of leaving an ImportError |
| `internal: true` breaks something that needs egress | Only the sandbox is on that network, and it is meant to have no egress. Verified explicitly in Phase 2 |
| Postgres dependency in generated apps breaks SQLite-shaped code | Developer SOP line + pre-baked driver + per-run schema; the boot check still runs first |
| Session format change logs everyone out | One-time, documented. Cookie name unchanged so stale cookies fail closed rather than half-working |
| Rate limiting moves from memory to a table | Adds a query on the login path; index on `(ip, at)` |
| Sidecar becomes a new trust boundary | Narrow two-endpoint API, mount allowlist, no privileged mode; it is strictly smaller than the socket mount it replaces |
| `docker.sock` on the host is itself a risk | Documented in README as a host-level exposure independent of this application |

## 12. Out of scope

Kubernetes manifests · gVisor / Firecracker microVMs · per-tenant resource
quotas · multi-region · a message broker between agents · embedding generated
code in the UI as anything other than static files · streaming LLM tokens to
the browser (the pipeline is sequential; per-token streaming would change the
architecture, not speed it up).

## 13. Done definition

* `perf_baseline.json` exists and Phase 1 shows a measured p50 improvement with
  no change in dev retries or spec coverage.
* No generated code runs outside a container. `SANDBOX_MODE=subprocess` exists
  for local development and is off in every deployed profile.
* `docker.sock` is mounted in exactly one compose service.
* `grep -r langgraph backend/` returns nothing; image size is recorded before
  and after.
* Login rate limiting and logout survive a restart.
* All tests green, ruff clean.