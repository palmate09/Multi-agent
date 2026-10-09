"""Tests for the LLM backend selection layer."""

import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from agents import llm  # noqa: E402
from agents.llm import _safe_url  # noqa: E402


def test_safe_url_allows_http_and_https():
    assert _safe_url("https://api.groq.com/v1") == "https://api.groq.com/v1"
    assert _safe_url("http://localhost:11434/api/tags")


@pytest.mark.parametrize(
    "bad",
    ["file:///etc/passwd", "ftp://example.com", "javascript:alert(1)", "", "gopher://x"],
)
def test_safe_url_blocks_non_http_schemes(bad):
    """Backend URLs are partly env-derived, so urlopen must never see file://."""
    with pytest.raises(ValueError):
        _safe_url(bad)


def test_ollama_probe_short_circuits_in_testing(monkeypatch):
    """The suite must never touch a real Ollama, even if one is running."""
    monkeypatch.setenv("AGENT_TEAM_TESTING", "1")
    llm._OLLAMA_OK["probed"] = False
    llm._OLLAMA_OK["models"] = {"some-model"}
    assert llm._ollama_available("some-model") is False
    llm._OLLAMA_OK["probed"] = False
    llm._OLLAMA_OK["models"] = set()


def test_ollama_probe_requires_matching_model(monkeypatch):
    monkeypatch.delenv("AGENT_TEAM_TESTING", raising=False)
    llm._OLLAMA_OK["probed"] = True
    llm._OLLAMA_OK["models"] = {"qwen2.5-coder:7b-instruct-q4_K_M"}
    assert llm._ollama_available("qwen2.5-coder:7b-instruct-q4_K_M") is True
    # Tag-less name still matches by base name.
    assert llm._ollama_available("qwen2.5-coder:7b") is True
    assert llm._ollama_available("llama3.3:70b") is False
    llm._OLLAMA_OK["probed"] = False
    llm._OLLAMA_OK["models"] = set()


def test_generate_reports_failure_when_no_backend_answers(monkeypatch):
    """There is no template fallback any more: no backend means ok=False."""
    monkeypatch.setenv("SKIP_OLLAMA", "1")
    monkeypatch.setenv("AGENT_TEAM_TESTING", "1")
    monkeypatch.setenv("PREFER_LOCAL_ONLY", "0")
    for key in (
        "GEMINI_API_KEY",
        "GROQ_API_KEY",
        "OPENROUTER_API_KEY",
        "GITHUB_TOKEN",
        "HF_TOKEN",
        "OPENAI_API_KEY",
    ):
        monkeypatch.delenv(key, raising=False)
    llm.CACHE.clear()
    text, meta = llm.generate("unique prompt for no-backend test", role="developer")
    assert text == ""
    assert meta["ok"] is False
    assert meta["model"] != "template"
    assert meta["errors"]


def test_require_raises_rather_than_returning_empty(monkeypatch):
    """The fail-loud contract every agent depends on."""
    monkeypatch.setenv("SKIP_OLLAMA", "1")
    monkeypatch.setenv("AGENT_TEAM_TESTING", "1")
    monkeypatch.setenv("PREFER_LOCAL_ONLY", "0")
    for key in ("GEMINI_API_KEY", "GROQ_API_KEY", "OPENROUTER_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    with pytest.raises(llm.GenerationError):
        llm.require("no backend can answer this", "you are a test", role="developer")


def test_prefer_local_only_blocks_hosted_tiers(monkeypatch):
    """A rate-limited cloud key must not stand in for the local run."""
    monkeypatch.setenv("SKIP_OLLAMA", "1")
    monkeypatch.setenv("PREFER_LOCAL_ONLY", "1")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    llm.CACHE.clear()

    called = []

    def fake_post(url, payload, headers=None, timeout=60):
        called.append(url)
        return {"candidates": [{"content": {"parts": [{"text": "cloud"}]}}]}

    monkeypatch.setattr(llm, "_post_json", fake_post)
    text, meta = llm.generate("prefer local please", role="developer")
    assert text == ""
    assert meta["ok"] is False
    assert not called, "hosted tier was contacted despite PREFER_LOCAL_ONLY=1"
    llm.CACHE.clear()


def test_role_models_are_configurable(monkeypatch):
    """Designer defaults to the larger model; dev/test default to the smaller."""
    for key in ("OLLAMA_MODEL", "OLLAMA_MODEL_DESIGNER", "OLLAMA_MODEL_DEVELOPER"):
        monkeypatch.delenv(key, raising=False)
    assert llm.role_model("designer") == "qwen2.5-coder:7b-instruct-q4_K_M"
    assert llm.role_model("developer") == "qwen2.5-coder:3b"
    monkeypatch.setenv("OLLAMA_MODEL_DEVELOPER", "custom:7b")
    assert llm.role_model("developer") == "custom:7b"


def test_generate_caches_successful_responses(monkeypatch):
    monkeypatch.setenv("SKIP_OLLAMA", "1")
    monkeypatch.setenv("AGENT_TEAM_TESTING", "1")
    monkeypatch.setenv("PREFER_LOCAL_ONLY", "0")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    llm.CACHE.clear()

    calls: list[str] = []

    def fake_post(url, payload, headers=None, timeout=60):
        calls.append(url)
        return {"candidates": [{"content": {"parts": [{"text": "generated"}]}}]}

    monkeypatch.setattr(llm, "_post_json", fake_post)

    text, meta = llm.generate("cache me please", role="developer")
    assert text == "generated"
    assert meta["model"].startswith("gemini")

    text2, meta2 = llm.generate("cache me please", role="developer")
    assert text2 == "generated"
    assert meta2["model"] == "cache" and meta2["cached"] is True
    assert len(calls) == 1, "second identical call must not hit the network"

    llm.CACHE.clear()


def test_cache_is_scoped_per_run(monkeypatch):
    """A run must never be served another run's completion.

    Runs share this module's CACHE. Keying only on prompt text means a second
    run asking the same question inherits the first run's code verbatim, which
    looks like the agent reusing pre-existing code.
    """
    monkeypatch.setenv("SKIP_OLLAMA", "1")
    monkeypatch.setenv("AGENT_TEAM_TESTING", "1")
    monkeypatch.setenv("PREFER_LOCAL_ONLY", "0")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    llm.clear_cache()

    responses = iter(["from run A", "from run B"])

    def fake_post(url, payload, headers=None, timeout=60):
        return {"candidates": [{"content": {"parts": [{"text": next(responses)}]}}]}

    monkeypatch.setattr(llm, "_post_json", fake_post)

    llm.set_run_scope("run-A")
    a, _ = llm.generate("identical prompt", role="developer")
    # Same run, same prompt: dedup still applies.
    a_again, meta = llm.generate("identical prompt", role="developer")
    assert a == a_again == "from run A"
    assert meta.get("cached") is True

    # Different run, same prompt: must go back to the backend.
    llm.set_run_scope("run-B")
    b, _ = llm.generate("identical prompt", role="developer")
    assert b == "from run B", "run B inherited run A's cached response"

    llm.clear_cache()
    llm.set_run_scope("")


def test_cache_is_bounded(monkeypatch):
    """A long-lived backend must not grow the cache without limit."""
    monkeypatch.setenv("SKIP_OLLAMA", "1")
    monkeypatch.setenv("AGENT_TEAM_TESTING", "1")
    monkeypatch.setenv("PREFER_LOCAL_ONLY", "0")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    llm.clear_cache()
    llm.set_run_scope("bounds")

    n = 0

    def fake_post(url, payload, headers=None, timeout=60):
        nonlocal n
        n += 1
        return {"candidates": [{"content": {"parts": [{"text": f"r{n}"}]}}]}

    monkeypatch.setattr(llm, "_post_json", fake_post)
    for i in range(llm.CACHE_MAX + 25):
        llm.generate(f"prompt {i}", role="developer")
    assert len(llm.CACHE) <= llm.CACHE_MAX

    llm.clear_cache()
    llm.set_run_scope("")


# ------------------------------------------------------------- retries ------
def test_transient_429_is_retried(monkeypatch):
    """A free-tier 429 must not abandon the run when the next call would work."""
    import urllib.error

    monkeypatch.setenv("LLM_RETRY_MAX", "3")
    monkeypatch.setenv("LLM_RETRY_BACKOFF", "0")
    monkeypatch.setenv("LLM_RETRY_SLEEP", "0")
    attempts = {"n": 0}

    def flaky(url, payload, headers=None, timeout=60):
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise urllib.error.HTTPError(url, 429, "Too Many Requests", None, None)
        return {"choices": [{"message": {"content": "recovered"}}]}

    monkeypatch.setattr(llm, "_post_json", flaky)
    out = llm._post_json_retry("https://example.invalid/x", {})
    assert out["choices"][0]["message"]["content"] == "recovered"
    assert attempts["n"] == 3


def test_503_is_retried_then_succeeds(monkeypatch):
    import urllib.error

    monkeypatch.setenv("LLM_RETRY_BACKOFF", "0")
    monkeypatch.setenv("LLM_RETRY_SLEEP", "0")
    calls = {"n": 0}

    def flaky(url, payload, headers=None, timeout=60):
        calls["n"] += 1
        if calls["n"] == 1:
            raise urllib.error.HTTPError(url, 503, "Service Unavailable", None, None)
        return {"ok": True}

    monkeypatch.setattr(llm, "_post_json", flaky)
    assert llm._post_json_retry("https://example.invalid/x", {}) == {"ok": True}
    assert calls["n"] == 2


def test_404_is_not_retried(monkeypatch):
    """A retired model id will never succeed; retrying only wastes quota."""
    import urllib.error

    monkeypatch.setenv("LLM_RETRY_BACKOFF", "0")
    monkeypatch.setenv("LLM_RETRY_SLEEP", "0")
    calls = {"n": 0}

    def gone(url, payload, headers=None, timeout=60):
        calls["n"] += 1
        raise urllib.error.HTTPError(url, 404, "Not Found", None, None)

    monkeypatch.setattr(llm, "_post_json", gone)
    with pytest.raises(urllib.error.HTTPError):
        llm._post_json_retry("https://example.invalid/x", {})
    assert calls["n"] == 1


def test_timeout_is_not_retried(monkeypatch):
    """Three 120s timeouts would become a six-minute stall."""
    monkeypatch.setenv("LLM_RETRY_BACKOFF", "0")
    monkeypatch.setenv("LLM_RETRY_SLEEP", "0")
    calls = {"n": 0}

    def slow(url, payload, headers=None, timeout=60):
        calls["n"] += 1
        raise TimeoutError("timed out")

    monkeypatch.setattr(llm, "_post_json", slow)
    with pytest.raises(TimeoutError):
        llm._post_json_retry("https://example.invalid/x", {})
    assert calls["n"] == 1


def test_cloud_order_respects_provider(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    assert [n for _, n in llm.cloud_order()] == ["gemini"]
    monkeypatch.setenv("LLM_PROVIDER", "local")
    assert llm.cloud_order() == []
    monkeypatch.setenv("LLM_PROVIDER", "auto")
    assert len(llm.cloud_order()) > 1


def test_gemini_tolerates_reasoning_only_first_candidate():
    """flash models may lead with a non-text part before the answer."""
    out = {
        "candidates": [
            {"content": {"parts": [{"text": "def f():\n    pass"}]}},
        ]
    }
    assert llm._gemini_text(out) == "def f():\n    pass"
    assert llm._gemini_text({"candidates": [{"content": {}}]}) == ""


@pytest.mark.parametrize(
    "provider,expected",
    [
        ("gemini", ["gemini"]),
        ("groq", ["groq/free"]),
        ("github", ["github-models"]),
        ("openrouter", ["openrouter/free"]),
        ("huggingface", ["huggingface"]),
        ("openai", ["openai/gpt-4o-mini"]),
        ("local", []),
    ],
)
def test_any_provider_can_be_selected_by_name(monkeypatch, provider, expected):
    """Naming one provider must not spend a request on the unconfigured ones."""
    monkeypatch.setenv("LLM_PROVIDER", provider)
    assert [n for _, n in llm.cloud_order()] == expected


def test_auto_tries_every_tier(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "auto")
    assert len(llm.cloud_order()) == 6


def test_local_provider_skips_hosted_tiers_entirely(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "local")
    assert llm.cloud_order() == []
    assert llm._prefer_cloud() is False


@pytest.mark.parametrize("provider", ["gemini", "groq", "github", "auto"])
def test_hosted_providers_bypass_ollama(monkeypatch, provider):
    """Otherwise a hosted run would first burn minutes on CPU inference."""
    monkeypatch.setenv("LLM_PROVIDER", provider)
    assert llm._prefer_cloud() is True


def test_requests_send_an_explicit_user_agent(monkeypatch):
    """Cloudflare answers urllib's default agent with 403 error 1010.

    That is indistinguishable from a rejected API key, so every request must
    carry an explicit User-Agent.
    """
    import urllib.request

    seen = {}

    class FakeResp:
        def read(self):
            return b'{"ok": true}'

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=None):
        seen["headers"] = dict(req.headers)
        return FakeResp()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    llm._post_json("https://api.groq.com/openai/v1/chat/completions", {})
    agent = seen["headers"].get("User-agent") or seen["headers"].get("User-Agent")
    assert agent, "no User-Agent header was sent"
    assert "Python-urllib" not in agent, agent


def test_explicit_user_agent_can_be_overridden(monkeypatch):
    import urllib.request

    seen = {}

    class FakeResp:
        def read(self):
            return b'{"ok": true}'

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=None):
        seen["headers"] = dict(req.headers)
        return FakeResp()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    llm._post_json("https://example.invalid/x", {}, {"User-Agent": "custom/9"})
    agent = seen["headers"].get("User-agent") or seen["headers"].get("User-Agent")
    assert agent == "custom/9"


def test_groq_model_ids_are_the_live_ones(monkeypatch):
    """Groq returns 403 (not 404) for ids it has decommissioned.

    The previous list used qwen3-32b and llama-3.3-70b-versatile, which made
    every Groq call fail while looking like a bad key.
    """
    import agents.llm as m

    src = m._groq.__doc__ or ""
    monkeypatch.setenv("GROQ_MODEL", "")
    captured = []

    def fake_compat(url, key, model, prompt, system, timeout, **kw):
        captured.append(model)
        return "text" if model == "qwen/qwen3.8-27b" else None

    monkeypatch.setattr(m, "_openai_compat", fake_compat)
    monkeypatch.setenv("GROQ_API_KEY", "gsk_test")
    assert m._groq("p", "s", 30) == "text"
    assert captured[0] == "qwen/qwen3.8-27b"
    assert "qwen3-32b" not in captured
    assert "llama-3.3-70b-versatile" not in captured
    assert src == ""


# ------------------------------------------------------------- reasoning ----
def test_reasoning_is_on_for_every_role_except_the_reviewer():
    """The default set is every role whose answer improves from deliberation."""
    for role in (
        "pm",
        "reasoner",
        "designer",
        "developer",
        "tester",
        "triage",
        "reflection",
    ):
        assert llm.reasoning_requested(role) is True, role
    assert llm.reasoning_requested("reviewer") is False


def test_reasoning_kill_switch_disables_every_role(monkeypatch):
    monkeypatch.setenv("LLM_REASONING", "0")
    assert llm.reasoning_requested("pm") is False
    assert llm.reasoning_requested("reviewer") is False


def test_reasoning_roles_env_replaces_the_default_set(monkeypatch):
    """An empty value means no role reasons; it must not fall back to default."""
    monkeypatch.setenv("LLM_REASONING_ROLES", "")
    assert llm.reasoning_requested("pm") is False

    monkeypatch.setenv("LLM_REASONING_ROLES", "reviewer, triage")
    assert llm.reasoning_requested("reviewer") is True
    assert llm.reasoning_requested("triage") is True
    assert llm.reasoning_requested("pm") is False


def test_a_single_role_flag_adds_one_role_back(monkeypatch):
    monkeypatch.setenv("LLM_REASONING_ROLES", "")
    monkeypatch.setenv("LLM_REASONING_ROLE_REVIEWER", "1")
    assert llm.reasoning_requested("reviewer") is True
    assert llm.reasoning_requested("pm") is False


def test_reasoning_effort_and_budget_are_env_tunable(monkeypatch):
    assert llm.reasoning_effort("pm") == "medium"
    monkeypatch.setenv("LLM_REASONING_EFFORT", "high")
    assert llm.reasoning_effort("pm") == "high"
    monkeypatch.setenv("LLM_REASONING_EFFORT_REVIEWER", "low")
    assert llm.reasoning_effort("reviewer") == "low"
    monkeypatch.setenv("LLM_REASONING_EFFORT_PM", "nonsense")
    assert llm.reasoning_effort("pm") == "medium", "an unknown effort must not reach the API"

    assert llm.reasoning_budget("pm") == 1024
    monkeypatch.setenv("LLM_REASONING_BUDGET", "2048")
    assert llm.reasoning_budget("pm") == 2048
    monkeypatch.setenv("LLM_REASONING_BUDGET", "not-a-number")
    assert llm.reasoning_budget("pm") == 1024


def test_reasoning_doubles_the_completion_ceiling(monkeypatch):
    """Thinking is billed against the answer's budget, so 3000 is not enough."""
    assert llm._reasoning_max_tokens(3000) == 6000
    monkeypatch.setenv("LLM_REASONING_MAX_TOKENS", "16384")
    assert llm._reasoning_max_tokens(3000) == 16384


def test_inline_thinking_tags_are_pulled_out_of_the_answer():
    raw = "<thinking>let me work it out</thinking>\n```yaml\nopenapi: 3.1.0\n```"
    answer, thoughts = llm._strip_thinking(raw)
    assert "work it out" not in answer
    assert "openapi" in answer
    assert "work it out" in thoughts
    assert llm._strip_thinking("no thinking here") == ("no thinking here", "")


def test_reasoning_body_is_shaped_per_provider_and_model():
    """gpt-oss rejects reasoning_format; a plain instruct model rejects both."""
    gpt_oss = llm._reasoning_body("groq", "openai/gpt-oss-20b", "developer")
    assert gpt_oss == {"reasoning_effort": "medium", "include_reasoning": True}
    assert "reasoning_format" not in gpt_oss, "mutually exclusive with include_reasoning"

    qwen = llm._reasoning_body("groq", "qwen/qwen3.8-27b", "developer")
    assert qwen["reasoning_format"] == "parsed"
    assert "include_reasoning" not in qwen

    assert llm._reasoning_body("groq", "llama-3.3-70b-versatile", "developer") is None
    assert llm._reasoning_body("openrouter", "openai/gpt-oss-20b:free", "pm") == {
        "reasoning": {"effort": "medium"}
    }


def test_out_of_range_effort_is_clamped_down_not_sent(monkeypatch):
    """Anything but low/medium/high is a 400 from either Groq model family."""
    monkeypatch.setenv("LLM_REASONING_EFFORT", "xhigh")
    body = llm._reasoning_body("groq", "openai/gpt-oss-120b", "developer")
    assert body["reasoning_effort"] == "high"
    assert (
        llm._reasoning_body("groq", "qwen/qwen3.8-27b", "developer")["reasoning_effort"] == "high"
    )

    monkeypatch.setenv("LLM_REASONING_EFFORT", "minimal")
    # Below every allowed value: fall to the lowest one that exists.
    assert llm._reasoning_body("groq", "qwen/qwen3.8-27b", "developer")["reasoning_effort"] == "low"

    monkeypatch.setenv("LLM_REASONING_EFFORT", "none")
    # "none" means do not reason, so no effort key is sent at all.
    assert "reasoning_effort" not in llm._reasoning_body("groq", "qwen/qwen3.8-27b", "developer")


def test_openai_compat_returns_the_answer_and_captures_the_reasoning(monkeypatch):
    monkeypatch.setattr(
        llm,
        "_post_json",
        lambda *a, **k: {
            "choices": [
                {
                    "message": {
                        "content": "the answer",
                        "reasoning": "step by step I decided",
                    },
                    "finish_reason": "stop",
                }
            ]
        },
    )
    llm._LAST_REASONING.clear()
    text = llm._openai_compat(
        "https://api.groq.com/openai/v1/chat/completions",
        "key",
        "openai/gpt-oss-20b",
        "p",
        "s",
        30,
        reasoning_body={"include_reasoning": True},
    )
    assert text == "the answer"
    assert llm._LAST_REASONING == ["step by step I decided"]


def test_openai_compat_strips_raw_format_thinking_from_the_answer(monkeypatch):
    """Groq's default `raw` format inlines <thinking> in the content."""
    monkeypatch.setattr(
        llm,
        "_post_json",
        lambda *a, **k: {
            "choices": [
                {
                    "message": {"content": "<thinking>hmm</thinking>\nthe answer"},
                    "finish_reason": "stop",
                }
            ]
        },
    )
    llm._LAST_REASONING.clear()
    text = llm._openai_compat(
        "https://api.groq.com/openai/v1/chat/completions",
        "key",
        "qwen/qwen3.8-27b",
        "p",
        "s",
        30,
        reasoning_body={"reasoning_format": "raw"},
    )
    assert text == "\nthe answer"
    assert llm._LAST_REASONING == ["<thinking>hmm</thinking>"]


def test_reasoning_model_reports_why_the_content_came_back_empty(monkeypatch):
    """A cap sized for the answer alone returns nothing but the reasoning."""
    monkeypatch.setattr(
        llm,
        "_post_json",
        lambda *a, **k: {
            "choices": [
                {"message": {"content": "", "reasoning": "spent it all"}, "finish_reason": "length"}
            ]
        },
    )
    llm._LAST_REASONING.clear()
    llm._FAILURES.clear()
    text = llm._openai_compat(
        "https://api.groq.com/openai/v1/chat/completions",
        "key",
        "openai/gpt-oss-20b",
        "p",
        "s",
        30,
        reasoning_body={"include_reasoning": True},
    )
    assert text is None
    assert not llm._LAST_REASONING, "a failed attempt must not leave its thinking behind"
    assert any("reasoning consumed the budget" in f for f in llm._FAILURES)


def test_gemini_thinking_parts_never_reach_the_answer():
    out = {
        "candidates": [
            {
                "content": {
                    "parts": [
                        {"text": "summarised thought", "thought": True},
                        {"text": "def f():\n    pass"},
                    ]
                }
            }
        ]
    }
    answer, thoughts = llm._gemini_parts(out)
    assert answer == "def f():\n    pass"
    assert thoughts == "summarised thought"
    assert llm._gemini_text(out) == "def f():\n    pass"


def test_gemini_sends_a_thinking_config_only_when_the_role_asks(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.delenv("GEMINI_MODEL", raising=False)
    monkeypatch.setenv("GEMINI_MODELS", "gemini-3-flash-preview")
    llm._LAST_MODEL.clear()
    llm._LAST_REASONING.clear()
    seen = {}

    def fake_post(url, payload, headers=None, timeout=60):
        seen.update(payload["generationConfig"])
        return {"candidates": [{"content": {"parts": [{"text": "ok"}]}}]}

    monkeypatch.setattr(llm, "_post_json", fake_post)

    # Reasoning off for this role: no thinkingConfig, untouched token ceiling.
    monkeypatch.setenv("LLM_REASONING_ROLES", "reviewer")
    assert llm._gemini("p", "s", 30, role="pm") == "ok"
    assert "thinkingConfig" not in seen
    assert seen["maxOutputTokens"] == 8192

    # Reasoning on: thinkingConfig present and the ceiling grows by the budget.
    monkeypatch.setenv("LLM_REASONING_ROLES", "pm")
    monkeypatch.setenv("LLM_REASONING_BUDGET", "2048")
    assert llm._gemini("p", "s", 30, role="pm") == "ok"
    assert seen["thinkingConfig"] == {"includeThoughts": True, "thinkingBudget": 2048}
    assert seen["maxOutputTokens"] == 8192 + 2048


def test_generate_surfaces_reasoning_in_meta_and_log(monkeypatch):
    monkeypatch.setenv("SKIP_OLLAMA", "1")
    monkeypatch.setenv("AGENT_TEAM_TESTING", "1")
    monkeypatch.setenv("PREFER_LOCAL_ONLY", "0")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.delenv("GEMINI_MODEL", raising=False)
    monkeypatch.setenv("GEMINI_MODELS", "gemini-3-flash-preview")
    llm.CACHE.clear()

    def fake_post(url, payload, headers=None, timeout=60):
        return {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {"text": "a design", "thought": True},
                            {"text": "the spec text"},
                        ]
                    }
                }
            ],
            "usageMetadata": {"thoughtsTokenCount": 77},
        }

    monkeypatch.setattr(llm, "_post_json", fake_post)

    text, meta = llm.generate("design this", role="designer")
    assert text == "the spec text"
    assert meta["reasoning"] == "a design"
    assert meta["reasoning_tokens"] == 77


def test_cached_call_reports_no_reasoning(monkeypatch):
    monkeypatch.setenv("SKIP_OLLAMA", "1")
    monkeypatch.setenv("AGENT_TEAM_TESTING", "1")
    monkeypatch.setenv("PREFER_LOCAL_ONLY", "0")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    llm.CACHE.clear()
    monkeypatch.setattr(
        llm,
        "_post_json",
        lambda *a, **k: {"candidates": [{"content": {"parts": [{"text": "once"}]}}]},
    )
    _, first = llm.generate("cache reasoning", role="pm")
    _, second = llm.generate("cache reasoning", role="pm")
    assert second["cached"] is True
    assert second["reasoning"] == ""
    assert second["reasoning_tokens"] == 0
    assert first["reasoning"] == ""
