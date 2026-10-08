"""LLM wrapper: Ollama local -> free hosted tiers -> template fallback.

Free hosted (no pull, no GPU/RAM needed — all OpenAI-compatible, no card):
  1. Gemini AI Studio (GEMINI_API_KEY): gemini-2.0-flash, 15 RPM / 1500 RPD, 1M ctx
  2. Groq (GROQ_API_KEY): qwen3.8-27b / gpt-oss-20b, ~14.4k req/day, fastest LPU
  3. OpenRouter (OPENROUTER_API_KEY): :free suffix, 50 req/day (1000 after $10 top-up)
  4. GitHub Models (GITHUB_TOKEN): free with GitHub account, rate-limited
  5. HuggingFace (HF_TOKEN): small monthly credit on Inference Providers

Set SKIP_OLLAMA=1 to skip local entirely (recommended on 10GB RAM / 4GB VRAM).
Logs every call to outputs/llm_log.jsonl. Never raises: returns ("", meta)
when no backend reachable so callers use deterministic templates.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.request
from pathlib import Path
from typing import Any

try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:
    pass

LOG_PATH = Path("outputs/llm_log.jsonl")
CACHE: dict[str, str] = {}
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
_OLLAMA_OK: dict[str, Any] = {"probed": False, "models": set()}


def _log(entry: dict[str, Any]) -> None:
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_PATH, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        pass


def _ollama_available(model: str) -> bool:
    """One-shot reachability probe + model presence check.

    Avoids a long generation attempt when Ollama is down or the model was never
    pulled, which otherwise burns the full request timeout per agent call.
    """
    if os.getenv("AGENT_TEAM_TESTING") == "1":
        return False
    if not _OLLAMA_OK["probed"]:
        _OLLAMA_OK["probed"] = True
        try:
            tags_url = _safe_url(f"{OLLAMA_URL}/api/tags")
            with urllib.request.urlopen(tags_url, timeout=2) as r:
                data = json.loads(r.read().decode())
            _OLLAMA_OK["models"] = {m.get("name", "") for m in data.get("models", [])}
        except Exception:
            _OLLAMA_OK["models"] = set()
    models = _OLLAMA_OK["models"]
    return model in models or any(m.split(":")[0] == model.split(":")[0] for m in models)


def _safe_url(url: str) -> str:
    """Reject non-HTTP schemes before handing a URL to urlopen.

    Backend URLs come partly from the environment (OLLAMA_URL), so without this
    a misconfigured value could point urlopen at file:// or another handler.
    """
    if not url.startswith(("http://", "https://")):
        raise ValueError(f"refusing non-HTTP backend URL scheme: {url!r}")
    return url


def _post_json(url: str, payload: dict, headers: dict | None = None, timeout: int = 60) -> dict:
    _safe_url(url)
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        url, data=data, headers=headers or {"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def _ollama(prompt: str, system: str, model: str, timeout: int) -> str | None:
    try:
        out = _post_json(
            f"{OLLAMA_URL}/api/generate",
            {"model": model, "prompt": prompt, "system": system, "stream": False},
            timeout=timeout,
        )
        return out.get("response", "")
    except Exception:
        return None


def _gemini(prompt: str, system: str, timeout: int) -> str | None:
    key = os.getenv("GEMINI_API_KEY")
    if not key:
        return None
    model = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")
    try:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"
        out = _post_json(
            url, {"contents": [{"parts": [{"text": system + "\n\n" + prompt}]}]}, timeout=timeout
        )
        return out["candidates"][0]["content"]["parts"][0]["text"]
    except Exception:
        return None


def _openai_compat(
    url: str,
    key: str,
    model: str,
    prompt: str,
    system: str,
    timeout: int,
    extra_headers: dict | None = None,
) -> str | None:
    try:
        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {key}"}
        if extra_headers:
            headers.update(extra_headers)
        out = _post_json(
            url,
            {
                "model": model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0.1,
                "max_tokens": 3000,
            },
            headers=headers,
            timeout=timeout,
        )
        return out["choices"][0]["message"]["content"]
    except Exception:
        return None


def _groq(prompt: str, system: str, timeout: int) -> str | None:
    key = os.getenv("GROQ_API_KEY")
    if not key:
        return None
    # 2026 free Groq models in order of code quality
    for m in [
        os.getenv("GROQ_MODEL", ""),
        "qwen/qwen3-32b",
        "openai/gpt-oss-20b",
        "llama-3.3-70b-versatile",
    ]:
        if not m:
            continue
        text = _openai_compat(
            "https://api.groq.com/openai/v1/chat/completions", key, m, prompt, system, timeout
        )
        if text:
            return text
    return None


def _openrouter(prompt: str, system: str, timeout: int) -> str | None:
    key = os.getenv("OPENROUTER_API_KEY")
    if not key:
        return None
    for m in [
        os.getenv("OPENROUTER_MODEL", ""),
        "cohere/north-mini-code:free",
        "openai/gpt-oss-20b:free",
        "qwen/qwen3-32b:free",
        "meta-llama/llama-3.3-70b-instruct:free",
    ]:
        if not m:
            continue
        text = _openai_compat(
            "https://openrouter.ai/api/v1/chat/completions",
            key,
            m,
            prompt,
            system,
            timeout,
            {"HTTP-Referer": "https://localhost", "X-Title": "multi-agent-team"},
        )
        if text:
            return text
    return None


def _github_models(prompt: str, system: str, timeout: int) -> str | None:
    key = os.getenv("GITHUB_TOKEN")
    if not key:
        return None
    model = os.getenv("GITHUB_MODEL", "openai/gpt-4o-mini")
    return _openai_compat(
        "https://models.github.ai/inference/chat/completions", key, model, prompt, system, timeout
    )


def _huggingface(prompt: str, system: str, timeout: int) -> str | None:
    key = os.getenv("HF_TOKEN")
    if not key:
        return None
    model = os.getenv("HF_MODEL", "Qwen/Qwen2.5-Coder-32B-Instruct")
    try:
        out = _post_json(
            f"https://api-inference.huggingface.co/models/{model}/v1/chat/completions",
            {
                "model": model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": prompt},
                ],
                "max_tokens": 3000,
                "temperature": 0.1,
            },
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
            timeout=timeout,
        )
        return out["choices"][0]["message"]["content"]
    except Exception:
        return None


def _openai(prompt: str, system: str, timeout: int) -> str | None:
    key = os.getenv("OPENAI_API_KEY")
    if not key:
        return None
    try:
        out = _post_json(
            "https://api.openai.com/v1/chat/completions",
            {
                "model": "gpt-4o-mini",
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0.1,
                "max_tokens": 3000,
            },
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
            timeout=timeout,
        )
        return out["choices"][0]["message"]["content"]
    except Exception:
        return None


def generate(
    prompt: str,
    system: str = "You are a helpful coding assistant.",
    role: str = "developer",
    max_tokens: int = 3000,
    temperature: float = 0.1,
    timeout: int = 90,
) -> tuple[str, dict]:
    """Try backends in cost order. Returns (text, meta). Empty text = use template."""
    h = hashlib.sha256((system + prompt + role).encode()).hexdigest()[:16]
    if h in CACHE:
        return CACHE[h], {"model": "cache", "cached": True}
    t0 = time.time()
    skip_local = os.getenv("SKIP_OLLAMA", "") == "1"
    text, model = "", ""
    # 1. local ollama (free, private) — skipped with SKIP_OLLAMA=1
    if not skip_local:
        local_model = os.getenv(
            "OLLAMA_MODEL",
            "qwen2.5-coder:3b"
            if role in ("pm", "triage", "reflection")
            else "qwen2.5-coder:7b-instruct-q4_K_M",
        )
        if _ollama_available(local_model):
            text = _ollama(prompt, system, local_model, timeout=min(timeout, 60))
            model = f"ollama/{local_model}" if text else ""
    # 2. free hosted tiers — tried for ALL roles when local skipped/missed
    #    (previously heavy roles only; now pm/triage also use cloud if SKIP_OLLAMA=1)
    if not text and (
        skip_local
        or role in ("developer", "tester", "reviewer", "designer", "pm", "triage", "reflection")
    ):
        for fn, name in (
            (_gemini, "gemini/gemini-2.0-flash"),
            (_groq, "groq/free"),
            (_openrouter, "openrouter/free"),
            (_github_models, "github-models"),
            (_huggingface, "huggingface"),
            (_openai, "openai/gpt-4o-mini"),
        ):
            text = fn(prompt, system, timeout)
            if text:
                model = name
                break
    latency = round(time.time() - t0, 2)
    if not text:
        text, model = "", "template"
    if text:
        CACHE[h] = text
    _log(
        {
            "role": role,
            "model": model,
            "prompt": prompt[:2000],
            "response": (text or "")[:2000],
            "latency": latency,
        }
    )
    return text, {"model": model, "latency": latency}
