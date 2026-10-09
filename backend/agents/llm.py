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

Reasoning is opt-in per role (``LLM_REASONING_ROLES``): a role that asks for it
gets a model that thinks before answering, and the thinking comes back in
``meta["reasoning"]`` rather than mixed into the answer. A ``<thinking>`` block
in front of the Designer's YAML is a parse failure, not a plan.

Logs every call to $RUNS_DIR/llm_log.jsonl.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
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

# Thinking captured by the backend that served the last call, deliberately kept
# out of the answer text. Backends append; generate() takes and clears it
# through _take_reasoning(), so an attempt that reasoned before returning an
# empty string never leaks its thoughts into the next attempt's metadata.
_LAST_REASONING: list[str] = []
_LAST_REASONING_TOKENS: list[int] = []

# Roles whose output is long or structurally demanding. Everything else uses the
# faster small model; see role_model() for the throughput measurements.
_HEAVY_ROLES = frozenset({"designer"})

# Roles that ask the backend to think before answering. Every role except the
# Reviewer: its verdict comes from reading the generated code rather than from
# deliberating over the requirement, and it already sends the longest prompt of
# the group. Override with LLM_REASONING_ROLES or LLM_REASONING_ROLE_<ROLE>.
# "reasoner" is in by definition: it is the node that exists to think first.
_DEFAULT_REASONING_ROLES = frozenset(
    {"pm", "designer", "developer", "tester", "triage", "reflection", "reasoner"}
)

# Per-role token ceilings, sized to the largest plausible artifact.
_NUM_PREDICT = {
    "pm": 2000,
    "designer": 4000,
    "developer": 4000,
    "tester": 4000,
    "reviewer": 2000,
    "reasoner": 2000,
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


def _ollama(
    prompt: str, system: str, model: str, num_predict: int, timeout: int, role: str = ""
) -> str | None:
    # Ollama only honours ``think`` for models that reason; sending it to a
    # coder model that has no reasoning support fails the request outright.
    think = reasoning_requested(role) and _looks_reasoning(model)
    body: dict = {
        "model": model,
        "prompt": prompt,
        "system": system,
        "stream": False,
        # An explicit cap. Without it Ollama truncates mid-file and the
        # parser reports a malformed generation instead of a short one.
        "options": {
            "num_predict": _reasoning_max_tokens(num_predict) if think else num_predict,
            "temperature": 0.1,
        },
    }
    if think:
        # Thinking arrives in its own field, never mixed into ``response``.
        body["think"] = True
    try:
        out = _post_json(f"{OLLAMA_URL}/api/generate", body, timeout=timeout)
        if out.get("done_reason") == "length":
            _note_failure(
                f"ollama/{model}", f"truncated at num_predict={num_predict} (done_reason=length)"
            )
        thinking = (out.get("thinking") or "").strip()
        if thinking:
            _LAST_REASONING.append(thinking)
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


def _gemini(prompt: str, system: str, timeout: int, role: str = "") -> str | None:
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
    thinking = reasoning_requested(role)
    budget = reasoning_budget(role)
    for model in models:
        # A failed attempt's thinking must not ride into the next model's answer.
        _take_reasoning()
        try:
            cfg: dict = {"temperature": 0.1, "maxOutputTokens": max_out}
            if thinking:
                cfg["maxOutputTokens"] = max_out + budget
                cfg["thinkingConfig"] = _gemini_thinking_config(budget)
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"
            out = _post_json_retry(
                url,
                {
                    "contents": [{"parts": [{"text": system + "\n\n" + prompt}]}],
                    "generationConfig": cfg,
                },
                timeout=timeout,
            )
            answer, thoughts = _gemini_parts(out)
            if answer:
                if thoughts:
                    _LAST_REASONING.append(thoughts)
                used = (out.get("usageMetadata") or {}).get("thoughtsTokenCount")
                if isinstance(used, int) and used > 0:
                    _LAST_REASONING_TOKENS.append(used)
                _LAST_MODEL.append(f"gemini/{model}")
                return answer
            _note_failure(f"gemini/{model}", f"empty content: {str(out)[:160]}")
        except Exception as exc:
            _note_failure(f"gemini/{model}", exc)
    return None


def _gemini_thinking_config(budget: int) -> dict:
    """``thinkingConfig`` for the request.

    The 2.5 series only understands ``thinkingBudget`` while 3.x prefers a
    level, so the level is opt-in via ``GEMINI_THINKING_LEVEL`` and the budget
    is the default: the cookbook documents it as accepted by both generations.
    """
    level = (os.getenv("GEMINI_THINKING_LEVEL", "") or "").strip().lower()
    config: dict = {"includeThoughts": True}
    if level in {"low", "medium", "high"}:
        config["thinkingLevel"] = level
    else:
        config["thinkingBudget"] = budget
    return config


def _gemini_parts(out: dict) -> tuple[str, str]:
    """Split a response into ``(answer, thoughts)``.

    A thinking model marks its reasoning with ``thought: true``. Joining those
    parts into the answer -- which is what this function used to do -- put
    reasoning prose in front of the YAML the Designer parses, and a
    reasoning-only first candidate was returned as if it were the answer.
    """
    candidates = out.get("candidates") or []
    collected: list[str] = []
    for cand in candidates:
        parts = (cand.get("content") or {}).get("parts") or []
        answer: list[str] = []
        thoughts: list[str] = []
        for p in parts:
            if not isinstance(p, dict) or not p.get("text"):
                continue
            (thoughts if p.get("thought") else answer).append(p["text"])
        collected.extend(thoughts)
        if answer:
            # A reasoning-only candidate keeps its thoughts and hands the
            # search to the next one, which is where the answer lives.
            return "".join(answer), "".join(collected)
    return "", "".join(collected)


def _gemini_text(out: dict) -> str:
    """The answer only; reasoning parts are never part of it."""
    return _gemini_parts(out)[0]


def _openai_compat(
    url: str,
    key: str,
    model: str,
    prompt: str,
    system: str,
    timeout: int,
    extra_headers: dict | None = None,
    max_tokens: int = 3000,
    reasoning_body: dict | None = None,
) -> str | None:
    try:
        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {key}"}
        if extra_headers:
            headers.update(extra_headers)
        # Reasoning is billed against the same ceiling as the answer, so the
        # cap has to be sized for both or the reply comes back empty.
        budget = _reasoning_max_tokens(max_tokens) if reasoning_body else max_tokens
        payload: dict = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.1,
            "max_tokens": budget,
        }
        if reasoning_body:
            payload.update(reasoning_body)
        out = _post_json_retry(url, payload, headers=headers, timeout=timeout)
        choice = out["choices"][0]
        message = choice.get("message") or {}
        content = message.get("content") or ""
        # Groq's `parsed` format and OpenRouter both put the thinking in a
        # sibling field; Groq's `raw` default inlines it as <thinking> tags.
        # Either way it must never be handed back as the answer.
        reasoning = (message.get("reasoning") or message.get("reasoning_content") or "").strip()
        answer, inline = _strip_thinking(content)
        if not answer.strip():
            # Reasoning models return an empty message when the ceiling is
            # consumed before any visible output.
            finish = choice.get("finish_reason")
            why = ", reasoning consumed the budget" if reasoning else ""
            raise ValueError(
                f"empty content (finish_reason={finish}, max_tokens={budget}){why}"
            )
        if reasoning or inline:
            _LAST_REASONING.append(reasoning or inline)
        return answer
    except Exception as exc:
        detail = str(exc)
        if "403" in detail:
            # Groq answers 403 for decommissioned model ids as well as for a
            # rejected key; naming the id is the only way to tell them apart.
            _note_failure(f"groq/{model}", f"HTTP 403 (id retired or key rejected) {detail[:120]}")
        else:
            _note_failure(f"openai-compat/{model}", detail[:200])
        return None


def _groq(prompt: str, system: str, timeout: int, role: str = "") -> str | None:
    key = os.getenv("GROQ_API_KEY")
    if not key:
        return None
    wants = reasoning_requested(role)
    # Verified against GET /openai/v1/models. Groq answers 403 (not 404) for
    # ids it has decommissioned, which is indistinguishable from an auth failure
    # unless the list is correct — the previous qwen3-32b and llama-3.3-70b ids
    # are gone and made every Groq call fail.
    models = [
        os.getenv("GROQ_MODEL", ""),
        "qwen/qwen3.8-27b",
        "openai/gpt-oss-20b",
        "openai/gpt-oss-120b",
    ]
    if wants:
        # Lead with an id that reasons: qwen3.8 answers without thinking unless
        # asked, and a configured GROQ_MODEL may be a plain instruct model.
        pref = (os.getenv("GROQ_REASONING_MODEL", "") or "").strip()
        models = ([pref] if pref else ["openai/gpt-oss-120b", "openai/gpt-oss-20b"]) + models
    for m in _dedupe(models):
        text = _openai_compat(
            "https://api.groq.com/openai/v1/chat/completions",
            key,
            m,
            prompt,
            system,
            timeout,
            max_tokens=8192,
            reasoning_body=_reasoning_body("groq", m, role) if wants else None,
        )
        if text:
            _LAST_MODEL.append(f"groq/{m}")
            return text
    return None


def _openrouter(prompt: str, system: str, timeout: int, role: str = "") -> str | None:
    key = os.getenv("OPENROUTER_API_KEY")
    if not key:
        return None
    wants = reasoning_requested(role)
    models = [
        os.getenv("OPENROUTER_MODEL", ""),
        "cohere/north-mini-code:free",
        "openai/gpt-oss-20b:free",
        "qwen/qwen3-32b:free",
        "meta-llama/llama-3.3-70b-instruct:free",
    ]
    if wants:
        pref = (os.getenv("OPENROUTER_REASONING_MODEL", "") or "").strip()
        models = (
            [pref]
            if pref
            else ["openai/gpt-oss-20b:free", "qwen/qwen3-32b:free"]
        ) + models
    for m in _dedupe(models):
        text = _openai_compat(
            "https://openrouter.ai/api/v1/chat/completions",
            key,
            m,
            prompt,
            system,
            timeout,
            {"HTTP-Referer": "https://localhost", "X-Title": "multi-agent-team"},
            reasoning_body=_reasoning_body("openrouter", m, role) if wants else None,
        )
        if text:
            _LAST_MODEL.append(f"openrouter/{m}")
            return text
    return None


def _github_models(prompt: str, system: str, timeout: int, role: str = "") -> str | None:
    key = os.getenv("GITHUB_TOKEN")
    if not key:
        return None
    model = os.getenv("GITHUB_MODEL", "openai/gpt-4o-mini")
    return _openai_compat(
        "https://models.github.ai/inference/chat/completions", key, model, prompt, system, timeout
    )


def _huggingface(prompt: str, system: str, timeout: int, role: str = "") -> str | None:
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


def _openai(prompt: str, system: str, timeout: int, role: str = "") -> str | None:
    # role is accepted for the uniform backend signature; the paid tier has no
    # separate reasoning model configured, so no reasoning params are sent.
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


def reasoning_requested(role: str) -> bool:
    """True when this role should spend tokens thinking before answering.

    Read per call rather than at import, so a running pipeline can be retuned
    without a restart — the same convention as :func:`role_num_predict`.

    ================  =========================================================
    ``LLM_REASONING`` ``0``/``off`` disables reasoning everywhere; anything
                      else leaves it to the role sets below.
    ``LLM_REASONING_ROLES``  replaces the default set (comma separated). Set it
                      to the empty string to reason for no role at all.
    ``LLM_REASONING_ROLE_<ROLE>``  ``1`` adds one role back without
                      re-listing the rest, e.g. ``LLM_REASONING_ROLE_REVIEWER=1``.
    ================  =========================================================
    """
    if (os.getenv("LLM_REASONING", "") or "").strip().lower() in {"0", "off", "false", "no"}:
        return False
    name = (role or "").strip().lower()
    if not name:
        return False
    raw = os.getenv("LLM_REASONING_ROLES")
    roles = (
        frozenset(p.strip().lower() for p in raw.split(",") if p.strip())
        if raw is not None
        else _DEFAULT_REASONING_ROLES
    )
    if name in roles:
        return True
    flag = (os.getenv(f"LLM_REASONING_ROLE_{name.upper()}", "") or "").strip().lower()
    return flag in {"1", "on", "true", "yes"}


# Groq's reasoning_effort scale. Accepted by the endpoint as a whole; the
# individual models take a subset (see _reasoning_body).
_REASONING_EFFORTS = frozenset(
    {"none", "default", "minimal", "low", "medium", "high", "xhigh", "max"}
)

# gpt-oss accepts only these three efforts; anything else is a 400.
_GPT_OSS_EFFORTS = frozenset({"low", "medium", "high"})

# The efforts both Groq reasoning families accept. ``none``/``default`` mean
# "do not reason" and are simply omitted; ``xhigh``/``max`` are not offered by
# gpt-oss or qwen3.8, so an out-of-range request is clamped down rather than
# allowed to fail the whole tier with a 400.
_COMMON_EFFORTS = frozenset({"low", "medium", "high"})

_EFFORT_RANK = ("none", "default", "minimal", "low", "medium", "high", "xhigh", "max")

# Model ids known to expose a reasoning channel. Params are only ever sent to
# one of these: Groq answers 400 for a reasoning key on a model that has none,
# and a 400 is not retried, so a wrong guess would burn the whole tier.
_REASONING_IDS = ("gpt-oss", "qwen3", "deepseek", "minimax", "r1")


def _clamp_effort(effort: str, allowed: frozenset[str]) -> str:
    """The closest allowed effort at or below the requested one."""
    if effort in allowed:
        return effort
    order = list(_EFFORT_RANK)
    if effort not in order:
        effort = "medium"
    below = [e for e in order[: order.index(effort)] if e in allowed]
    if below:
        return below[-1]
    ranked = [e for e in order if e in allowed]
    return ranked[0] if ranked else "medium"


def _looks_reasoning(model: str) -> bool:
    return any(tok in (model or "").lower() for tok in _REASONING_IDS)


def reasoning_effort(role: str) -> str:
    """Effort for a role, per-role override first. Defaults to ``medium``."""
    raw = os.getenv(f"LLM_REASONING_EFFORT_{role.upper()}", "") or os.getenv(
        "LLM_REASONING_EFFORT", ""
    )
    effort = (raw or "medium").strip().lower()
    return effort if effort in _REASONING_EFFORTS else "medium"


def reasoning_budget(role: str) -> int:
    """Gemini ``thinkingBudget`` in tokens for a role; ``0`` turns it off.

    Separate from :func:`reasoning_effort` because the 2.5 series only accepts
    a budget while 3.x prefers a level (see :func:`_gemini_thinking_config`).
    """
    raw = os.getenv(f"LLM_REASONING_BUDGET_{role.upper()}", "") or os.getenv(
        "LLM_REASONING_BUDGET", ""
    )
    try:
        return max(0, int(raw))
    except ValueError:
        return 1024


def _reasoning_max_tokens(base: int) -> int:
    """Completion ceiling for a call that thinks first.

    Thinking is billed against the same budget as the answer, so a cap sized
    for the answer alone comes back as empty content with ``finish_reason
    length`` — the failure already named in :func:`_openai_compat`. Doubling is
    the floor; ``LLM_REASONING_MAX_TOKENS`` raises it further.
    """
    try:
        floor = int(os.getenv("LLM_REASONING_MAX_TOKENS", "") or 0)
    except ValueError:
        floor = 0
    return max(base * 2, floor)


def _take_reasoning() -> tuple[str, int]:
    """Hand over and clear the thinking captured by the last backend call."""
    text = "".join(_LAST_REASONING)
    tokens = sum(_LAST_REASONING_TOKENS)
    _LAST_REASONING.clear()
    _LAST_REASONING_TOKENS.clear()
    return text, tokens


def _dedupe(models: list[str]) -> list[str]:
    """Drop blanks and repeats, keeping the first occurrence's order."""
    seen: set[str] = set()
    out = []
    for m in models:
        m = (m or "").strip()
        if m and m not in seen:
            seen.add(m)
            out.append(m)
    return out


def _reasoning_body(provider: str, model: str, role: str) -> dict | None:
    """Provider-specific reasoning params, or ``None`` for an unknown id.

    The shapes differ by provider and are mutually exclusive within Groq:
    ``include_reasoning`` cannot be combined with ``reasoning_format``, and
    gpt-oss rejects ``reasoning_format`` outright.
    """
    if not _looks_reasoning(model):
        return None
    effort = reasoning_effort(role)
    if provider == "groq":
        if "gpt-oss" in model:
            # reasoning_format is rejected for gpt-oss (HTTP 400); it exposes
            # the thinking in `reasoning` whether asked or not, and takes only
            # low/medium/high -- "none" clamps to the lowest it knows.
            return {
                "reasoning_effort": _clamp_effort(effort, _GPT_OSS_EFFORTS),
                "include_reasoning": True,
            }
        body: dict = {"reasoning_format": "parsed"}
        if effort not in {"none", "default"}:
            body["reasoning_effort"] = _clamp_effort(effort, _COMMON_EFFORTS)
        return body
    if provider == "openrouter":
        if effort in {"none", "default"}:
            return None
        return {"reasoning": {"effort": effort}}
    return None


_THINK_FENCE = re.compile(
    r"<(?:thinking|reasoning)>.*?</(?:thinking|reasoning)>", re.IGNORECASE | re.DOTALL
)


def _strip_thinking(content: str) -> tuple[str, str]:
    """Pull inline thinking tags out of a completion: ``(answer, thoughts)``.

    Groq's ``raw`` reasoning format — the default for qwen-class models —
    inlines the reasoning as ``<thinking>...</thinking>`` inside the content.
    Left in place it precedes the fenced YAML/JSON the parsers hunt for and is
    echoed back into every later prompt.
    """
    if "<" not in content:
        return content, ""
    found = "".join(_THINK_FENCE.findall(content))
    if not found:
        return content, ""
    return _THINK_FENCE.sub("", content), found


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

    When the serving role asked for reasoning, ``meta["reasoning"]`` carries the
    thinking (truncated) and ``meta["reasoning_tokens"]`` the count the backend
    reported. Both are empty/zero otherwise, and on a cache hit.
    """
    scope = _RUN_SCOPE.get()
    h = hashlib.sha256(f"{scope}\x00{system}\x00{prompt}\x00{role}".encode()).hexdigest()[:16]
    if h in CACHE:
        return CACHE[h], {
            "model": "cache",
            "cached": True,
            "ok": True,
            "reasoning": "",
            "reasoning_tokens": 0,
        }
    t0 = time.time()
    _FAILURES.clear()
    _LAST_MODEL.clear()
    _take_reasoning()
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
    reasoning_text, reasoning_tokens = "", 0

    def _try_cloud() -> None:
        nonlocal text, model, reasoning_text, reasoning_tokens
        for fn, name in cloud_order():
            text = fn(prompt, system, timeout, role)
            # Taken on every attempt, successful or not: a backend that
            # reasoned and then returned nothing must not have its thinking
            # attributed to whichever backend answers next.
            captured, tokens = _take_reasoning()
            if text:
                # Report the concrete model that served, not the routing label.
                model = _LAST_MODEL[-1] if _LAST_MODEL else name
                reasoning_text, reasoning_tokens = captured, tokens
                return

    if cloud_first and not prefer_local:
        _try_cloud()
    if not text and not skip_local:
        local_model = role_model(role)
        if _ollama_available(local_model):
            text = _ollama(
                prompt,
                system,
                local_model,
                num_predict=role_num_predict(role),
                timeout=timeout,
                role=role,
            )
            captured, tokens = _take_reasoning()
            if text:
                model = f"ollama/{local_model}"
                reasoning_text, reasoning_tokens = captured, tokens
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
            # The thinking, kept out of `response` so a log reader sees the
            # same text the parsers saw.
            "reasoning": reasoning_text[:2000],
            "reasoning_tokens": reasoning_tokens,
            # Why the fallbacks declined. Without this a retired model id, a
            # refused connection or a truncated answer is indistinguishable
            # from a healthy run.
            "failures": errors,
        }
    )
    meta = {"model": model, "latency": latency, "ok": bool(text), "failures": errors}
    # Always present, so callers can read it without a membership test.
    meta["reasoning"] = reasoning_text[:4000]
    meta["reasoning_tokens"] = reasoning_tokens
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
