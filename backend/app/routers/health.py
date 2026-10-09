"""Liveness/readiness and LLM backend introspection."""

from __future__ import annotations

import logging
import os
import urllib.request

from fastapi import APIRouter, Depends, Response, status

from app.config import Settings, get_settings
from app.dependencies import get_store
from app.schemas import HealthResponse, LlmStatus
from app.services.runs import RunStore

log = logging.getLogger("app.health")
router = APIRouter(tags=["health"])

HOSTED_KEYS = (
    "GEMINI_API_KEY",
    "GROQ_API_KEY",
    "OPENROUTER_API_KEY",
    "GITHUB_TOKEN",
    "HF_TOKEN",
    "OPENAI_API_KEY",
)


def _probe_ollama(url: str) -> list[str]:
    from agents.llm import _safe_url

    try:
        tags_url = _safe_url(f"{url}/api/tags")
        with urllib.request.urlopen(tags_url, timeout=2) as r:
            import json

            data = json.loads(r.read().decode())
        return sorted(m.get("name", "") for m in data.get("models", []))
    except Exception:
        return []


def llm_status(settings: Settings) -> LlmStatus:
    url = os.getenv("OLLAMA_URL", "http://localhost:11434")
    models = [] if os.getenv("AGENT_TEAM_TESTING") == "1" else _probe_ollama(url)
    skip = os.getenv("SKIP_OLLAMA", "") == "1"
    from agents import llm as llm_mod

    return LlmStatus(
        ollama_reachable=bool(models),
        ollama_models=models,
        skip_ollama=skip,
        hosted_keys_present=[k for k in HOSTED_KEYS if os.getenv(k)],
        last_failures=list(llm_mod._FAILURES),
        last_model=llm_mod._LAST_MODEL[-1] if llm_mod._LAST_MODEL else "",
    )


@router.get("/health", response_model=HealthResponse)
def health(
    response: Response,
    settings: Settings = Depends(get_settings),
    store: RunStore = Depends(get_store),
) -> HealthResponse:
    llm = llm_status(settings)
    # LLM availability is reported but never gates readiness: template mode is a
    # fully working mode that only covers task-manager CRUD.
    ready = store.active_count() < settings.max_concurrent_runs
    status_value = "ok" if ready else "degraded"
    if not ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return HealthResponse(
        status=status_value,
        version=settings.version,
        environment=settings.environment,
        llm=llm,
        active_runs=store.active_count(),
        max_concurrent_runs=settings.max_concurrent_runs,
    )


@router.get("/api/llm")
def llm(settings: Settings = Depends(get_settings)) -> LlmStatus:
    return llm_status(settings)


@router.get("/api/llm/log", tags=["debug"])
def llm_log_tail(lines: int = 50) -> dict:
    """Tail of the LLM call log, for the UI diagnostics panel."""
    from agents.llm import LOG_PATH

    if not LOG_PATH.exists():
        return {"entries": []}
    lines = max(1, min(lines, 500))
    content = LOG_PATH.read_text(errors="replace").strip().splitlines()
    import json

    out = []
    for raw in content[-lines:]:
        try:
            entry = json.loads(raw)
        except json.JSONDecodeError:
            continue
        entry.pop("prompt", None)
        entry.pop("response", None)
        out.append(entry)
    return {"entries": out}
