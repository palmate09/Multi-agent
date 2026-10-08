# Multi-Agent Software Team

5 LLM agents turn plain English into a tested, reviewed FastAPI REST API.
Local-first: Ollama primary, free cloud fallback, template mode offline.

## Architecture

```
Requirement -> PM (stories) -> Designer (OpenAPI) -> Developer || Tester
  -> Sandbox Runner -> triage+reflection -> Developer -> Reviewer (ruff+bandit) -> done
```

See `ARCHITECTURE.md` and `EXECUTION_PLAN.md`. Original brief: `docs/`.

## Quickstart

```bash
pip install -r requirements.txt
pytest -q
python cli.py --stub
python cli.py --run "task manager" --out outputs/demo
python evals/baseline.py
python cli.py --eval
```

Optional LLM (else template mode):
```bash
# ollama pull qwen2.5-coder:7b-instruct-q4_K_M
export GEMINI_API_KEY=... GROQ_API_KEY=... OPENROUTER_API_KEY=... OPENAI_API_KEY=...
```

## Results

| Run | Status | Pass rate | Spec coverage | Attempts |
|---|---|---|---|---|
| eval_task-manager | accepted | 1.0 | 1.0 | 0 |
| eval_task-manager-filter | accepted | 1.0 | 1.0 | 0 |
| eval_task-manager-notes | accepted | 1.0 | 1.0 | 0 |
| baseline (single-agent) | 7/7 pass | 1.0 | — | — |
| abl_no_tester | accepted | 1.0 | — | 0 |
| abl_no_reviewer | accepted_no_review | 1.0 | — | 0 |

Full: `evals/results.json`, per-run `outputs/*/summary.json`.

## Limits

* Template mode covers task-manager CRUD only; novel domains need LLM keys.
* 4GB VRAM → use 3B–7B Q4 models; heavy roles route to free cloud tiers.
* SWE-bench Lite is stretch, not yet run.
* Docker optional; local subprocess fallback used when Docker absent.

## Layout

`agents/ graph/ schemas/ sandbox/ evals/ outputs/ tests/ cli.py`
