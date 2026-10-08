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
  └────────► Sandbox Runner (Docker, no net) ──► TestReport
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
Tester ∥ Developer in parallel — Tester cannot copy Developer's mistakes (AgentCoder rule).

## 3. Agent contracts

Each agent: one job, fixed in/out schema, measurable done condition, file in `agents/`.

| Agent | Takes in | Produces | Tools | Done when |
|---|---|---|---|---|
| Project Manager (`agents/pm.py`) | Requirement, status reports | UserStories, AC, task board, final summary, triage verdict | Task board JSON, message router | Every AC maps to ≥1 test |
| Designer (`agents/designer.py`) | UserStories | OpenAPI YAML, data model, folder structure | OpenAPI validator | Spec validates + covers every story |
| Developer (`agents/developer.py`) | ApiSpec + task + failure reports + reflections | FastAPI code + DB models | `read_file/write_file/run` (SWE-agent ACI), linter | App boots, lints clean, Tester suite passes |
| Tester (`agents/tester.py`) | ApiSpec + stories (NOT code) | pytest suite, coverage, bug reports | pytest, coverage, Docker shell | Each endpoint: success + validation + error tests |
| Reviewer (`agents/reviewer.py`) | Code + tests + TestReport + ruff/bandit output | Ranked comments blocker/major/minor | ruff, bandit, diff reader | 0 blockers + spec conformance confirmed |

Schemas (`schemas/messages.py`, Pydantic v2):
`Requirement, UserStory, UserStories, ApiSpec, CodeBundle{files: {path: content}}, TestSuite, TestFailure, TestReport, ReviewComment, ReviewReport, GraphState{retry_dev, retry_test, retry_review, memory[]}`.

## 4. Orchestration (`graph/workflow.py`, LangGraph)

Nodes: `pm → designer → [developer ∥ tester] → runner → triage? → reviewer → pm_final`.
Conditional edges:

* `runner -- all pass --> reviewer`
* `runner -- fail + retry_dev<4 --> developer (with TestReport + reflection)`
* `triage -- test_bug + retry_test<2 --> tester`
* `reviewer -- blocker + retry_review<2 --> developer`
* else `-- cap --> pm_final(status=unresolved)` — never hangs.

State carries `memory: str[]` (Reflexion reflections), counters, last reports.
Every LLM call logs `{prompt, response, tokens, latency, model}` to JSONL.
All hand-offs saved under `outputs/<run_id>/` (MetaGPT inspectable-docs rule).

Fallback: if `langgraph` not installed, `workflow.py` uses a sequential
orchestrator with identical node order and retry logic.

## 5. Stack (local-first, 10GB RAM + 4GB RTX 2050)

* **Orchestration:** Python 3.11+ + LangGraph (AutoGen documented as alternative).
* **Generated target:** FastAPI + SQLAlchemy + SQLite + pytest.
* **Sandbox:** Docker `python:3.11-slim`, `--network none`, `--memory 512m --cpus 1`, 60s test timeout, 30s boot timeout.
* **Static analysis:** ruff + bandit (Reviewer input, not just LLM opinion).
* **LLM (`agents/llm.py`):**
  * Primary local: Ollama `qwen2.5-coder:7b-instruct-q4_K_M` (fits 4GB VRAM partial offload), fallback `qwen2.5-coder:3b` / `llama3.2:3b` for PM/triage/reflection. Works when `ollama` binary present.
  * Cloud fallback router per role (Developer/Tester/Reviewer heavy): Gemini 2.0 Flash free → Groq `llama-3.3-70b` free tier → OpenRouter free → `gpt-4o-mini` paid last. Keys via env: `GEMINI_API_KEY, GROQ_API_KEY, OPENROUTER_API_KEY, OPENAI_API_KEY`.
  * **Template mode (no keys, no Ollama):** deterministic built-in generators for task-manager produce valid spec/code/tests so E2E gates pass offline. LLM used opportunistically when available.
  * Cost savers: cache by input hash, truncate traces to 20 lines, temp 0.0–0.2, token caps per role.

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
agents/    # pm.py designer.py developer.py tester.py reviewer.py llm.py __init__.py
schemas/   # messages.py __init__.py
graph/     # workflow.py __init__.py
sandbox/   # Dockerfile runner.py run.sh __init__.py
evals/     # task-manager.md requirements_suite.json baseline.py metrics.py
outputs/   # <run_id>/spec.yaml code/... tests/... reports/ (generated)
tests/     # test_schemas.py test_agents.py test_workflow.py
docs/      # original project plan PDF
```

## 8. Quality gates and risks

* Phase gates: baseline recorded → stub graph valid → app boots → loop terminates → planted bugs found (3/3) → results table published.
* Risks: loops→caps+`unresolved`; wrong test→triage+spec-only+hand spot-check; unsafe code→Docker no-net; cost→logging+small models; luck→3× runs report spread; scope creep→REST-only.
