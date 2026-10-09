"""LLM wrapper: Ollama local -> free hosted tiers -> explicit failure.

Local-first. A role that needs a real answer gets one, or the run stops.

  * Per-role model routing via ``OLLAMA_MODEL_<ROLE>`` (or ``OLLAMA_MODEL``).
  * ``PREFER_LOCAL_ONLY=1`` forbids the hosted tiers, so a rate-limited cloud
    key can never quietly stand in for the local run you are inspecting.
  * ``generate(required=False)`` returns ``("", {"ok": False, "errors": [...]})``.
    ``generate(required=True)`` (the default) and :func:`require` raise
    :class:`GenerationError` instead.

There is deliberately **no template fallback**. Substituting a built-in
task-manager app for a failed generation produced runs that reported
``accepted`` while ignoring the requested domain entirely.

Logs every call to $RUNS_DIR/llm_log.jsonl.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.error
import urllib.request
from contextvars import ContextVar
from pathlib import Path
from typing import Any

try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:
    pass

LOG_PATH = Path("outputs/llm_log.jsonl")  # default; _log() prefers $RUNS_DIR
CACHE: dict[str, str] = {}
CACHE_MAX = 512
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
# Cloudflare fronts several hosted APIs and returns 403 "error code: 1010" to
# urllib's default agent, which looks exactly like an authentication failure.
USER_AGENT = os.getenv("LLM_USER_AGENT", "multi-agent-team/1.0 (+https://localhost)")
_OLLAMA_OK: dict[str, Any] = {"probed": False, "models": set()}

# Runs execute on worker threads and share this module's CACHE. Without a scope
# in the key, a second run sending an identical prompt is served the first run's
# completion verbatim, so it silently reuses code it never generated. run_team()
# sets this per run; the default keeps ad-hoc/CLI calls working.
_RUN_SCOPE: ContextVar[str] = ContextVar("agent_team_run_scope", default="")

# Why each backend declined, for the current generate() call. Every backend
# swallows its own exception and returns None so one failure never breaks the
# pipeline -- which previously also erased the reason, leaving a run silently
# reporting model=template with no explanation of what went wrong.
_FAILURES: list[str] = []

# Set by a backend to the id that actually served the request, so the logged
# model is the real one rather than the routing label.
_LAST_MODEL: list[str] = []

# Roles whose output is long or structurally demanding. Everything else uses the
# faster small model; see role_model() for the throughput measurements.
_HEAVY_ROLES = frozenset({"designer"})

# Per-role token ceilings, sized to the largest plausible artifact.
_NUM_PREDICT = {
    "pm": 2000,
    "designer": 4000,
    "developer": 4000,
    "tester": 4000,
    "reviewer": 2000,
    "triage": 500,
    "reflection": 800,
}


def _note_failure(name: str, exc: BaseException) -> None:
    detail = str(exc).strip().replace("\n", " ")[:200]
    _FAILURES.append(f"{name}: {type(exc).__name__} {detail}".strip())


def set_run_scope(run_id: str) -> None:
    _RUN_SCOPE.set(run_id)


# Cooperative stop signal for in-flight LLM backoff sleeps, set per run by
# run_team() and read by _post_json_retry(). A plain ContextVar (like
# _RUN_SCOPE) so no agent or backend signature changes: the event belongs to
# the worker thread running the pipeline. While set, retry sleeps return
# immediately instead of stalling a stop behind 4s/8s/16s backoffs; the
# in-flight HTTP request itself is never aborted, and the next node boundary
# in run_team ends the run. Default None keeps ad-hoc/CLI calls unchanged.
_CANCEL_SCOPE: ContextVar = ContextVar("agent_team_cancel", default=None)


def set_cancel(event) -> None:
    _CANCEL_SCOPE.set(event)


def clear_cache() -> None:
    CACHE.clear()


def _log_path() -> Path:
    """Where the call log goes.

    Defaults to RUNS_DIR, which is the mounted volume in the backend container.
    The old hardcoded relative "outputs/" is unwritable there: the app runs as
    appuser under a root-owned /app, so every write failed inside a bare except
    and the log was silently lost exactly when it mattered.
    """
    return Path(os.getenv("RUNS_DIR", "") or "outputs") / "llm_log.jsonl"


def _log(entry: dict[str, Any]) -> None:
    path = _log_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a") as f:
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
    hdrs = {"Content-Type": "application/json"}
    hdrs.update(headers or {})
    # Cloudflare in front of Groq (and some other hosted APIs) rejects the
    # default "Python-urllib/x.y" agent with HTTP 403 error 1010, which is
    # indistinguishable from a bad key. An explicit agent fixes it.
    hdrs.setdefault("User-Agent", USER_AGENT)
    req = urllib.request.Request(url, data=data, headers=hdrs)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def _ollama(prompt: str, system: str, model: str, num_predict: int, timeout: int) -> str | None:
    try:
        out = _post_json(
            f"{OLLAMA_URL}/api/generate",
            {
                "model": model,
                "prompt": prompt,
                "system": system,
                "stream": False,
                # An explicit cap. Without it Ollama truncates mid-file and the
                # parser reports a malformed generation instead of a short one.
                "options": {"num_predict": num_predict, "temperature": 0.1},
            },
            timeout=timeout,
        )
        if out.get("done_reason") == "length":
            _note_failure(
                f"ollama/{model}", f"truncated at num_predict={num_predict} (done_reason=length)"
            )
        return out.get("response", "")
    except Exception as exc:
        _note_failure(f"ollama/{model}", exc)
        return None


# Transient HTTP statuses worth retrying. Free tiers (Gemini's 15 RPM, Gemini's
# 503 under load) fail this way constantly, and a single 429 would otherwise
# abandon the whole run even though the next model id would have answered.
_RETRY_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})


def _retry_sleep() -> float:
    """Read per call, not at import, so the setting is tunable without a restart."""
    try:
        return max(0.0, float(os.getenv("LLM_RETRY_BACKOFF", "") or 4.0))
    except ValueError:
        return 4.0


def _retry_max() -> int:
    try:
        return max(1, int(os.getenv("LLM_RETRY_MAX", "") or 3))
    except ValueError:
        return 3


def _interruptible_sleep(delay: float) -> None:
    """Backoff sleep that a run stop cuts short.

    Reads the cancel scope (if run_team set one for this worker thread) and
    waits on the event instead of sleeping blindly, so stopping a run that is
    hammering a rate-limited provider returns in milliseconds, not after the
    full 4s/8s/16s backoff chain.
    """
    ev = _CANCEL_SCOPE.get()
    if ev is not None:
        ev.wait(delay)
    else:
        time.sleep(delay)


def _post_json_retry(
    url: str, payload: dict, headers: dict | None = None, timeout: int = 60
) -> dict:
    """POST JSON, retrying transient failures with exponential backoff."""
    last: Exception | None = None
    attempts = _retry_max()
    for attempt in range(attempts):
        try:
            return _post_json(url, payload, headers, timeout)
        except urllib.error.HTTPError as e:
            if e.code not in _RETRY_STATUS or attempt == attempts - 1:
                raise
            last = e
            delay = _retry_sleep() * (2**attempt)
            try:
                body = e.read().decode()[:120]
            except Exception:
                body = ""
            _note_failure(
                f"retry:{url.split('/')[2]}", f"HTTP {e.code}, sleeping {delay:.0f}s {body}"
            )
            _interruptible_sleep(delay)
        except (urllib.error.URLError, ConnectionError) as e:
            # Note: TimeoutError is deliberately NOT retried. With a 120s budget
            # per hosted call, three timeouts would turn one slow provider into a
            # six-minute stall; a timeout is better answered by the next provider.
            if attempt == attempts - 1:
                raise
            last = e
            delay = _retry_sleep() * (2**attempt)
            _note_failure(
                f"retry:{url.split('/')[2]}", f"{type(e).__name__}, sleeping {delay:.0f}s"
            )
            _interruptible_sleep(delay)
    if last:
        raise last
    raise RuntimeError("unreachable")


def _gemini(prompt: str, system: str, timeout: int) -> str | None:
    key = os.getenv("GEMINI_API_KEY")
    if not key:
        return None
    # Model ids churn: several were retired (404) for new users and some return
    # 503 under load, so an ordered list is tried rather than one pinned id.
    configured = os.getenv("GEMINI_MODEL", "").strip()
    models = (
        [configured]
        if configured
        else [
            m.strip()
            for m in os.getenv(
                "GEMINI_MODELS",
                "gemini-3-flash-preview,gemini-flash-latest,gemini-3.8-flash",
            ).split(",")
            if m.strip()
        ]
    )
    # Generous ceiling: the flash models spend output tokens on reasoning before
    # answering, and a small cap returns empty content with finishReason
    # MAX_TOKENS, which reads as a generation failure.
    max_out = int(os.getenv("GEMINI_MAX_OUTPUT_TOKENS", "") or 8192)
    for model in models:
        try:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"
            out = _post_json_retry(
                url,
                {
                    "contents": [{"parts": [{"text": system + "\n\n" + prompt}]}],
                    "generationConfig": {
                        "temperature": 0.1,
                        "maxOutputTokens": max_out,
                    },
                },
                timeout=timeout,
            )
            text = _gemini_text(out)
            if text:
                _LAST_MODEL.append(f"gemini/{model}")
                return text
            _note_failure(f"gemini/{model}", f"empty content: {str(out)[:160]}")
        except Exception as exc:
            _note_failure(f"gemini/{model}", exc)
    return None


def _gemini_text(out: dict) -> str:
    """Extract the text parts, tolerating a reasoning-only first candidate."""
    candidates = out.get("candidates") or []
    for cand in candidates:
        parts = (cand.get("content") or {}).get("parts") or []
        texts = [p["text"] for p in parts if isinstance(p, dict) and p.get("text")]
        if texts:
            return "".join(texts)
    return ""


def _openai_compat(
    url: str,
    key: str,
    model: str,
    prompt: str,
    system: str,
    timeout: int,
    extra_headers: dict | None = None,
    max_tokens: int = 3000,
) -> str | None:
    try:
        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {key}"}
        if extra_headers:
            headers.update(extra_headers)
        out = _post_json_retry(
            url,
            {
                "model": model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0.1,
                "max_tokens": max_tokens,
            },
            headers=headers,
            timeout=timeout,
        )
        content = out["choices"][0]["message"].get("content")
        if not content:
            # Reasoning models (gpt-oss) can return an empty message when the
            # ceiling is consumed before any visible output.
            finish = out["choices"][0].get("finish_reason")
            raise ValueError(f"empty content (finish_reason={finish}, max_tokens={max_tokens})")
        return content
    except Exception as exc:
        detail = str(exc)
        if "403" in detail:
            # Groq answers 403 for decommissioned model ids as well as for a
            # rejected key; naming the id is the only way to tell them apart.
            _note_failure(f"groq/{model}", f"HTTP 403 (id retired or key rejected) {detail[:120]}")
        else:
            _note_failure(f"groq/{model}", detail[:200])
        return None


def _groq(prompt: str, system: str, timeout: int) -> str | None:
    key = os.getenv("GROQ_API_KEY")
    if not key:
        return None
    # Verified against GET /openai/v1/models. Groq answers 403 (not 404) for
    # ids it has decommissioned, which is indistinguishable from an auth failure
    # unless the list is correct — the previous qwen3-32b and llama-3.3-70b ids
    # are gone and made every Groq call fail.
    for m in [
        os.getenv("GROQ_MODEL", ""),
        "qwen/qwen3.8-27b",
        "openai/gpt-oss-20b",
        "openai/gpt-oss-120b",
    ]:
        if not m:
            continue
        text = _openai_compat(
            "https://api.groq.com/openai/v1/chat/completions",
            key,
            m,
            prompt,
            system,
            timeout,
            max_tokens=8192,
        )
        if text:
            _LAST_MODEL.append(f"groq/{m}")
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
        out = _post_json_retry(
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
    except Exception as exc:
        _note_failure("openai-compat", exc)
        return None


def _openai(prompt: str, system: str, timeout: int) -> str | None:
    key = os.getenv("OPENAI_API_KEY")
    if not key:
        return None
    try:
        out = _post_json_retry(
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
    except Exception as exc:
        _note_failure("openai-compat", exc)
        return None


def role_model(role: str) -> str:
    """Resolve the Ollama model for a role.

    Per-role rather than one global ``OLLAMA_MODEL`` because the roles have
    very different token appetites. Measured on a CPU-only host: the 3b model
    sustains ~12.6 tok/s and the 7b q4 ~6.5 tok/s, so a full app costs minutes.
    The smaller model is used for volume (code + tests), the larger one only
    where structured-output quality matters most (the OpenAPI spec).

    Each role can still be overridden individually, e.g. ``OLLAMA_MODEL_DEV``.
    """
    default = "qwen2.5-coder:7b-instruct-q4_K_M" if role in _HEAVY_ROLES else "qwen2.5-coder:3b"
    return os.getenv(f"OLLAMA_MODEL_{role.upper()}") or os.getenv("OLLAMA_MODEL") or default


def role_num_predict(role: str) -> int:
    """Token ceiling for a role.

    Ollama's own default cap truncates a multi-file answer mid-statement, which
    the file parser then reports as a malformed generation. Generous caps cost
    nothing when the model stops early: ``done_reason == "stop"`` ends the call.
    """
    override = os.getenv("OLLAMA_NUM_PREDICT")
    if override and override.isdigit():
        return int(override)
    return _NUM_PREDICT.get(role, 3000)


def cloud_order() -> list[tuple[Any, str]]:
    """Hosted backends to try, in order.

    ``LLM_PROVIDER`` selects the strategy and accepts any provider name:

    ================  ==================================================
    ``gemini``        Gemini only
    ``groq``          Groq only (free, no card, the largest free quota)
    ``github``        GitHub Models only (free with a GitHub account)
    ``openrouter``    OpenRouter free models only
    ``huggingface``   HuggingFace Inference Providers only
    ``openai``        OpenAI only (paid)
    ``local``         no hosted tier at all; Ollama only
    ``auto``          every hosted tier, in the order below
    ``groq,gemini``   an explicit chain: Groq first, Gemini only when Groq
                      declines (rate limit, outage). Unknown names are
                      ignored; a chain with no known name behaves as ``auto``.
    ================  ==================================================

    Naming one provider matters on a free tier: ``auto`` would spend a request
    on each provider that has no key before reaching the one you configured.
    """
    provider = os.getenv("LLM_PROVIDER", "auto").strip().lower()
    tiers = {
        "gemini": (_gemini, "gemini"),
        "groq": (_groq, "groq/free"),
        "openrouter": (_openrouter, "openrouter/free"),
        "github": (_github_models, "github-models"),
        "huggingface": (_huggingface, "huggingface"),
        "openai": (_openai, "openai/gpt-4o-mini"),
    }
    if provider in tiers:
        return [tiers[provider]]
    if provider == "local":
        return []
    if "," in provider:
        ordered = []
        for name in provider.split(","):
            tier = tiers.get(name.strip())
            if tier is not None and tier not in ordered:
                ordered.append(tier)
        if ordered:
            return ordered
    return list(tiers.values())


def _prefer_cloud() -> bool:
    """True when the hosted tiers should be tried before (or without) Ollama.

    True for every hosted selection except ``local``, so ``LLM_PROVIDER=groq``
    does not quietly spend minutes on CPU inference first.
    """
    return os.getenv("LLM_PROVIDER", "auto").strip().lower() != "local"


def generate(
    prompt: str,
    system: str = "You are a helpful coding assistant.",
    role: str = "developer",
    max_tokens: int = 3000,
    temperature: float = 0.1,
    timeout: int | None = None,
    required: bool = True,
) -> tuple[str, dict]:
    """Try backends in cost order. Returns ``(text, meta)``.

    With ``required=True`` (the default) an empty ``text`` means the generation
    genuinely failed and ``meta["ok"]`` is False. Callers must then stop or
    retry — they may not substitute a fixed answer, because that is how a
    task-manager template got reported as a successful library API.
    """
    scope = _RUN_SCOPE.get()
    h = hashlib.sha256(f"{scope}\x00{system}\x00{prompt}\x00{role}".encode()).hexdigest()[:16]
    if h in CACHE:
        return CACHE[h], {"model": "cache", "cached": True, "ok": True}
    t0 = time.time()
    _FAILURES.clear()
    _LAST_MODEL.clear()
    skip_local = os.getenv("SKIP_OLLAMA", "") == "1"
    # When set, a local model is the only acceptable backend. Prevents a hosted
    # tier quietly standing in for a local run whose output you are inspecting.
    prefer_local = os.getenv("PREFER_LOCAL_ONLY", "") == "1"
    # LLM_PROVIDER=gemini sends the request to a hosted model first and skips
    # Ollama entirely, which is the difference between a ~6 minute run and a
    # ~30 second one on CPU-only hardware.
    cloud_first = _prefer_cloud()
    if timeout is None:
        default = 120 if cloud_first else 600
        timeout = int(os.getenv("LLM_TIMEOUT", "") or default)
    text, model = "", ""

    def _try_cloud() -> None:
        nonlocal text, model
        for fn, name in cloud_order():
            text = fn(prompt, system, timeout)
            if text:
                # Report the concrete model that served, not the routing label.
                model = _LAST_MODEL[-1] if _LAST_MODEL else name
                return

    if cloud_first and not prefer_local:
        _try_cloud()
    if not text and not skip_local:
        local_model = role_model(role)
        if _ollama_available(local_model):
            text = _ollama(
                prompt, system, local_model, num_predict=role_num_predict(role), timeout=timeout
            )
            model = f"ollama/{local_model}" if text else ""
    if not text and not prefer_local and not cloud_first:
        _try_cloud()
    latency = round(time.time() - t0, 2)
    # Backends return None on failure, so the empty result is normalised to ""
    # here: callers rely on a falsy *string*, never None.
    text = text or ""
    if text:
        CACHE[h] = text
        # Bounded so a long-lived backend cannot grow the cache without limit.
        while len(CACHE) > CACHE_MAX:
            del CACHE[next(iter(CACHE))]
    else:
        model = model or "none"
    errors = list(_FAILURES)
    _log(
        {
            "role": role,
            "model": model,
            "prompt": prompt[:2000],
            "response": (text or "")[:2000],
            "latency": latency,
            # Why the fallbacks declined. Without this a retired model id, a
            # refused connection or a truncated answer is indistinguishable
            # from a healthy run.
            "failures": errors,
        }
    )
    meta = {"model": model, "latency": latency, "ok": bool(text), "failures": errors}
    if not text:
        meta["errors"] = errors or [f"no backend answered (role={role})"]
    return text, meta


class GenerationError(RuntimeError):
    """Raised by :func:`require` when no backend produced text."""


def require(prompt: str, system: str, role: str, **kw) -> tuple[str, dict]:
    """Like :func:`generate` but raises instead of returning an empty string.

    Use in every role whose output defines the run's correctness. Silently
    continuing past an empty generation is exactly what let the pipeline report
    ``accepted`` for a task manager when a library API was requested.
    """
    text, meta = generate(prompt, system, role=role, **kw)
    if not text:
        raise GenerationError(
            f"LLM generation failed for role={role!r}: "
            + "; ".join(meta.get("errors") or ["no backend answered"])
        )
    return text, meta
