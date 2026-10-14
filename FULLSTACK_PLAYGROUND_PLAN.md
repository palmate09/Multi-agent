# Improvement Plan — Full-Stack Generation + Live Playground

> Status: planned, not yet implemented. Decisions locked: static HTML/JS/CSS UI
> (no build) · UI verdict is advisory and never blocks acceptance · subprocess +
> reverse-proxy playground gateway · `api`-kind projects only.

## 0. Background

Today the pipeline (`backend/graph/workflow.py:run_team`) turns a plain-English
requirement into a **tested, reviewed FastAPI backend only**:

```
pm → reasoner → designer → developer → coverage → boot → tester → runner
  → triage ⇄ developer → reviewer → final (status)
```

The repo has no frontend generation and no way to see a generated app running.
This plan adds both:

1. **Frontend development by the agents** — a `ui_designer` + `ui_developer` pair
   that emit a static, no-build web UI (`index.html`, `app.js`, `styles.css`)
   bound to the generated API's endpoints.
2. **A playground sandbox** — the accepted app is booted as a live subprocess
   and streamed to the user through a reverse-proxy gateway at
   `/preview/<run_id>/`, with a Playground tab in the SPA.

Verified baseline it must not regress: Groq runs accept in ~45–100 s;
`backend/evals/requirements_suite.json` + `run_all.sh` gates stay green.

## 1. Key design choices (and why)

- **`has_ui: bool`, not a third `kind`.** `kind` (`Literal["api","program"]` in
  `backend/schemas/messages.py`) is threaded through 8 modules. A boolean on
  `ApiSpec` plus separate UI state gives the same behaviour with a fraction of
  the blast radius.
- **UI is generated *after* backend tests are green**, not in parallel. It binds
  to a known-good API; the pipeline is sequential by design (`workflow.py:5`).
- **The generated UI must use relative URLs.** It is served at
  `/preview/<run_id>/`, so `fetch('/books')` would hit the *platform's* API, not
  the app's. Enforced in the prompt and checked statically (`rooted_url` gate).
- **Pre-existing bug to fix while in the area:** `workflow.py:510` and `:520`
  both do `st.retry_dev += 1` in one loop iteration and run `boot_check` twice
  per pass — the boot-repair budget burns twice as fast as `max_dev`.

## 2. Pipeline change

```
pm → reasoner → designer → developer → coverage → boot → tester → runner
  → triage ⇄ developer (unchanged; ends tests_green)
  → ui_designer                   NEW  spec+stories → UI contract
  → ui_developer                  NEW  contract → index.html, app.js, styles.css
  → ui_gate                       NEW  static checks + live probe (advisory)
  → reviewer                      UI findings: major/minor only, never blocker
  → final                         terminal status unchanged
```

New node names for `NODES` in `backend/graph/workflow.py`:
`ui_designer`, `ui_developer`, `ui_gate`.

## 3. Schemas (`backend/schemas/messages.py`)

```python
class UiCheck(BaseModel):
    name: str
    ok: bool
    detail: str = ""

class UiSpec(BaseModel):          # contract from the UI designer
    pages: list[str]              # e.g. ["/", "/books"]
    components: list[str]
    data_bindings: list[str]      # e.g. "GET /books -> book table"
    files: list[str]              # e.g. ["index.html", "app.js", "styles.css"]
    contract_text: str = ""

class UiReport(BaseModel):        # verdict, never gates acceptance
    __test__ = False
    ok: bool
    checks: list[UiCheck] = []
    probed: list[str] = []
```

- `ApiSpec` gains `has_ui: bool = False`.
- `GraphState` gains `ui_spec: UiSpec | None`, `ui_code: CodeBundle | None`,
  `ui_report: UiReport | None`, `skip_ui: bool = False`.
- `reconstruct_state` (`workflow.py:78`) must learn `ui_contract.json`,
  `ui/`, `ui_report.json`, or resume silently drops the UI.

## 4. Agents — new `backend/agents/ui.py`

### 4.1 `design_ui(spec, stories) -> UiSpec`

- 3 attempts, same shape as `_design_api` in `backend/agents/designer.py`.
- Validates: `files` includes `index.html`; at least one `data_bindings` entry
  resolves against `spec_paths(spec.openapi_yaml)` from
  `backend/agents/introspect.py`.
- Prose/unparseable reply raises `GenerationError` (no template fallback —
  the repo's standing rule).

### 4.2 `build_ui(spec, stories, ui_spec, entry) -> CodeBundle`

Developer SOP hard-constraints:

- Emit exactly `### <filename>` blocks + fenced code (same format as
  `backend/agents/developer.py:_API_SOP`). No build step, no npm, no bundler.
- All API calls **relative** (`fetch('books')`); never `/books`, never
  `http://localhost:8000/...` (breaks under the `/preview/<run_id>/` mount).
- No external origins (no CDN/fonts/scripts) — the sandbox has no network.
- No `innerHTML` interpolation (XSS); no `eval`/`exec`.
- Must render what the bound endpoints return.
- **Parse guard:** `parse_file_blocks` (`backend/agents/blocks.py:37`)
  silently falls back to `{"test_tasks.py": body}` when no `###` headers
  exist — reject that case explicitly so a UI reply that loses its headers
  can never produce a stray `.py`.

### 4.3 `gate(spec, ui_spec, files) -> UiReport`

Pure static checks, no LLM, millisecond cost:

| # | Check | Failure id |
|---|---|---|
| 1 | planned files present, non-empty, `index.html` exists | `missing_files` |
| 2 | no `fetch('/…')` or absolute/root-anchored URL in JS | `rooted_url` |
| 3 | no external origins (`//cdn`, unpkg, cdnjs, googleapis) | `external_asset` |
| 4 | no `innerHTML =` with interpolation | `xss_innerhtml` |
| 5 | every `data_bindings` entry maps to a `(method, path)` in the spec | `undeclared_endpoint` |
| 6 | every spec `(method, path)` has ≥1 binding | `unbound_endpoint` (warn) |
| 7 | HTML has a mount element + `<script src="app.js">` | `no_mount` |

## 5. Playground

### 5.1 Service — new `backend/app/services/playground.py`

- `PreviewSession{run_id, port, proc, started_at, last_hit, state, log_path}`.
- **Start:** allocate an ephemeral port (bind `:0`, close, reuse —
  race-tolerant), copy `code/` to `$RUNS_DIR/.preview/<run_id>/`, best-effort
  `pip install -r requirements.txt` (cached, 60 s cap, never fatal), then
  `Popen([sys.executable, "-m", "uvicorn",
  f"{entry_module}:{entry_attr}", "--host", "127.0.0.1", "--port", port])`.
- **Env hardening:** strip `GEMINI_API_KEY`, `GROQ_API_KEY`, `OPENAI_API_KEY`,
  `OPENROUTER_API_KEY`, `HF_TOKEN`, `GITHUB_TOKEN`, `AUTH_SECRET`,
  `AUTH_PASSWORD_HASH` from the child env. Generated code and platform
  credentials must not share a process. Set `PREVIEW=1`; point any SQLite
  path inside the preview dir.
- **Health probe:** poll the spec's first path (else `/health`, else `/`) to
  HTTP 200 within 30 s → `running`; else `crashed` with the log tail.
- **Lifecycle:** TTL reaper thread (`PREVIEW_TTL_SECONDS=1800` idle),
  `PREVIEW_MAX_CONCURRENT=3`; stopped on run delete / restart / backend
  shutdown (`RunStore.shutdown`).
- **Crash resilience:** `start` is idempotent; the gateway auto-restarts once
  if the process died (survives a review patch).

### 5.2 Gateway — new `backend/app/routers/playground.py`

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/runs/{rid}/playground` | `{state, url, port, started_at, error, log_tail}` |
| POST | `/api/runs/{rid}/playground/start` | start / restart, 202 |
| POST | `/api/runs/{rid}/playground/stop` | stop |
| ANY | `/preview/{rid}/{path:path}` | reverse proxy to `127.0.0.1:{port}` |

- Proxy via `httpx` streaming: forwards method/query/body, passes back status
  + body, strips hop-by-hop headers (`Connection`, `Transfer-Encoding`,
  `Upgrade`). WebSockets → 501 (out of scope for v1).

### 5.3 Integration edits (all required)

- `backend/app/main.py:100` (`auth_gate`): keep `/preview/*`
  **authenticated** (it must never become an open proxy); exempt it from the
  CSRF double-submit check with an explanatory comment — the caller is
  sandboxed generated code, not the platform API. `PUBLIC_PATHS` unchanged.
- `frontend/nginx/default.conf`: add `location /preview/` mirroring `/api/`
  (variable upstream, `proxy_buffering off`, 3600 s read timeout) plus
  `add_header X-Frame-Options "SAMEORIGIN"` and a preview-appropriate CSP.
  A nested block's `add_header` replaces the site-wide ones
  (currently `DENY` at line 14, which would refuse the iframe).
- `deploy/Caddyfile` + `deploy/Caddyfile.tls`: add
  `@preview path /preview/*` + `header @preview X-Frame-Options "SAMEORIGIN"`
  so Caddy does not re-apply `DENY` after nginx.
- `frontend/vite.config.ts`: add
  `"/preview": { target: BACKEND, changeOrigin: true }`.
- `backend/app/config.py`: `preview_ttl_seconds`,
  `preview_max_concurrent`, `preview_allow_start` (default on outside
  production).

## 6. Frontend

- `frontend/src/App.tsx`: two new tabs —
  - **Playground**: state pill, Start/Stop/Restart buttons, log tail,
    `<iframe src="/preview/<rid>/">` with an "open in new tab" fallback and a
    refresh button.
  - **UI**: contract pages/bindings, `FileViewer` for the UI files,
    `UiReport` checks as pass/fail rows styled like the existing
    domain-coverage alert.
- `frontend/src/components/RequirementForm.tsx`: "Generate a web UI"
  checkbox beside the three ablations.
- `frontend/src/api.ts` / `types.ts`: `previewStatus`, `startPreview`,
  `stopPreview`; `RunDetail` += `ui`, `ui_code`, `ui_report`, `playground`;
  `PipelineNode` += `ui_designer | ui_developer | ui_gate` with
  `NODE_LABELS`/`NODE_ORDER` entries so `PipelineTimeline` renders them.
- `api.ts createRun` + `app/schemas.py RunCreate`: add `ui: bool = False`
  (**default off**, so current acceptance numbers are untouched).

## 7. Evals, docs, gates

- `backend/evals/metrics.py:summarize_run` += `ui_pass_rate`,
  `preview_booted`; `results.json` and `EvalPanel.tsx` gain a UI column.
- `backend/evals/requirements_suite.json`: 2 full-stack requirements
  (book club + UI, recipe UI) so the path is *measured*, not demoed.
- `run_all.sh`: new gate `Phase 6: full-stack UI + playground boot`.
- Update `README.md` (stack, new fields, limits), `ARCHITECTURE.md` §3 agent
  table + node list, `EXECUTION_PLAN.md` (phase 6), `.env.example`.

## 8. Tests (all offline, no LLM)

| File | Covers |
|---|---|
| `backend/tests/test_ui_gate.py` | each static check as a table case; missing `index.html`; prose reply → `GenerationError`; `parse_file_blocks` header-loss guard |
| `backend/tests/test_playground.py` | start/stop/TTL/port reuse/crash-restart against a tiny fixture app; proxy GET+POST pass-through; **unauthenticated `/preview` → 401**; POST without CSRF header → not 403; child env contains no `*_API_KEY` |
| `backend/tests/test_ui_workflow.py` | UI node traversal with stubbed agents; `ui_report.ok == False` leaves terminal status `accepted`; resume restores `ui_spec`/`ui_code`/`ui_report` |
| regression | all 11 existing test files stay green |

## 9. Phasing (each ships and is verifiable alone)

- **Phase A — Generation + gate, no playground.** `has_ui` plumbing,
  `agents/ui.py`, three new workflow nodes, artifacts, resume support.
  Verify: `pytest -q` + one full-stack `cli.py --run`.
- **Phase B — Playground.** Service, gateway, auth/CSRF, nginx/Caddy/Vite.
  Verify: `curl -i localhost/preview/<rid>/` returns the app's HTML with the
  correct headers.
- **Phase C — UX + measurement.** Tabs, checkbox, timeline nodes, metrics,
  suite entries, docs, `run_all.sh` gate.

## 10. Risks / deliberately out of scope

| Risk | Mitigation |
|---|---|
| 3b models emit a broken UI | UI gate reports it; terminal status unaffected; optional later `ui_retry` cap mirroring `max_dev` |
| Generated code runs with platform network access | keys stripped from child env; bind `127.0.0.1`; TTL + concurrency caps; docs state this is **not** isolation |
| Preview leaks between runs | `/preview` stays auth-required; run-id → port map validated per request |
| Iframe refused by frame headers | nested nginx `add_header` + Caddy matcher, verified by `curl -I` |
| Acceptance rate regresses | `ui` defaults to **off**; existing suite unchanged in Phase A |

**Out of scope for v1:** React/Vite/npm builds · Docker-socket preview ·
`program`-kind previews · WebSockets · user-triggered code editing from the
browser.
