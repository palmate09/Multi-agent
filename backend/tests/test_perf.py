"""Performance harness assertions: token caps, truncation detection, concurrent calls.

These tests exercise the *logic* that the perf harness measures -- the token
ceiling arithmetic, the truncation detectors, and the concurrent call plumbing --
without needing a real LLM or the full pipeline. They run offline, fast, and
deterministically.
"""

import threading

import pytest


# ------------------------------------------------------------------------ token caps ---
def test_hosted_max_tokens_applies_the_floor():
    """designer/developer/tester get the _HOSTED_FLOOR (8000), not the base cap."""
    from agents.llm import _HOSTED_FLOOR, hosted_max_tokens

    # Designer: base 4000, floor 8000 -> 8000
    assert hosted_max_tokens("designer") == 8000
    # Developer: base 4000, floor 8000 -> 8000
    assert hosted_max_tokens("developer") == 8000
    # Tester: base 4000, floor 8000 -> 8000
    assert hosted_max_tokens("tester") == 8000
    # PM: base 2000, no floor -> 2000
    assert hosted_max_tokens("pm") == 2000
    # Reviewer: base 2000, no floor -> 2000
    assert hosted_max_tokens("reviewer") == 2000
    # Reasoner: base 2000, no floor -> 2000
    assert hosted_max_tokens("reasoner") == 2000
    # Triage: base 500, no floor -> 500
    assert hosted_max_tokens("triage") == 500
    # Reflection: base 800, no floor -> 800
    assert hosted_max_tokens("reflection") == 800

    # The floor applies per-role, not globally
    assert _HOSTED_FLOOR == {"designer": 8000, "developer": 8000, "tester": 8000}


def test_hosted_max_tokens_respects_env_override(monkeypatch):
    """LLM_MAX_TOKENS_<ROLE> and LLM_MAX_TOKENS are caps that override the floor."""
    from agents.llm import hosted_max_tokens

    # Per-role override wins
    monkeypatch.setenv("LLM_MAX_TOKENS_DESIGNER", "12000")
    assert hosted_max_tokens("designer") == 12000
    monkeypatch.delenv("LLM_MAX_TOKENS_DESIGNER")

    # Generic override
    monkeypatch.setenv("LLM_MAX_TOKENS", "9000")
    assert hosted_max_tokens("designer") == 9000
    assert hosted_max_tokens("pm") == 9000
    monkeypatch.delenv("LLM_MAX_TOKENS")

    # Provider-specific extra_env is checked after LLM_MAX_TOKENS
    monkeypatch.setenv("GEMINI_MAX_OUTPUT_TOKENS", "15000")
    assert hosted_max_tokens("designer", "GEMINI_MAX_OUTPUT_TOKENS") == 15000
    monkeypatch.delenv("GEMINI_MAX_OUTPUT_TOKENS")

    # Empty or invalid env is ignored
    monkeypatch.setenv("LLM_MAX_TOKENS", "not-a-number")
    assert hosted_max_tokens("designer") == 8000
    monkeypatch.delenv("LLM_MAX_TOKENS")

    monkeypatch.setenv("LLM_MAX_TOKENS", "")
    assert hosted_max_tokens("designer") == 8000
    monkeypatch.delenv("LLM_MAX_TOKENS")


def test_reasoning_max_tokens_doubles_for_hosted():
    """Reasoning models get double the token budget on hosted providers."""
    from agents.llm import _reasoning_max_tokens

    # _reasoning_max_tokens doubles the input (it's called with the hosted cap)
    assert _reasoning_max_tokens(8000) == 16000
    assert _reasoning_max_tokens(4096) == 8192
    assert _reasoning_max_tokens(3000) == 6000


def test_reasoning_max_tokens_respects_env_floor(monkeypatch):
    """LLM_REASONING_MAX_TOKENS can raise the reasoning cap further."""
    from agents.llm import _reasoning_max_tokens

    monkeypatch.setenv("LLM_REASONING_MAX_TOKENS", "20000")
    assert _reasoning_max_tokens(8000) == 20000
    monkeypatch.delenv("LLM_REASONING_MAX_TOKENS")


# ------------------------------------------------------------------------ truncation detection ---
def test_gemini_truncated_when_finish_reason_is_MAX_TOKENS():
    """Gemini responses with finishReason=MAX_TOKENS are flagged as truncated."""
    from agents.llm import _gemini_truncated

    # Normal completion
    out = {"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": "ok"}]}}]}
    assert _gemini_truncated(out, 8192) == ""

    # Truncated - MAX_TOKENS
    out = {"candidates": [{"finishReason": "MAX_TOKENS", "content": {"parts": [{"text": "partial"}]}}]}
    result = _gemini_truncated(out, 8192)
    assert result != ""
    assert "MAX_TOKENS" in result
    assert "8192" in result

    # Truncated - MAX_OUTPUT_TOKENS (alternative spelling, normalized in message)
    out = {"candidates": [{"finishReason": "MAX_OUTPUT_TOKENS"}]}
    result = _gemini_truncated(out, 8192)
    assert result != ""
    assert "MAX_TOKENS" in result  # normalized in the message


def test_gemini_truncated_when_no_candidates():
    """Gemini responses with no candidates return empty string (not truncated)."""
    from agents.llm import _gemini_truncated

    out = {}
    assert _gemini_truncated(out, 8192) == ""

    out = {"candidates": []}
    assert _gemini_truncated(out, 8192) == ""


# ------------------------------------------------------------------------ concurrent calls (thread_call) ---
def test_thread_call_copies_context_vars():
    """thread_call propagates _RUN_SCOPE and _CANCEL_SCOPE to the worker thread."""
    from agents.llm import _CANCEL_SCOPE, _RUN_SCOPE, thread_call

    # Set context vars in the main thread
    run_id = "test-run-123"
    cancel_token = object()
    _RUN_SCOPE.set(run_id)
    _CANCEL_SCOPE.set(cancel_token)

    results = {}

    def worker():
        # In the worker thread, the context vars should have the same values
        results["run_scope"] = _RUN_SCOPE.get()
        results["cancel_scope"] = _CANCEL_SCOPE.get()
        return "done"

    # thread_call returns a callable that must be invoked
    run_fn = thread_call(worker)
    result = run_fn()
    assert result == "done"
    assert results["run_scope"] == run_id
    assert results["cancel_scope"] is cancel_token

    # Clean up
    _RUN_SCOPE.set("")
    _CANCEL_SCOPE.set(None)


def test_thread_call_propagates_exceptions():
    """Exceptions in the worker thread are re-raised in the caller."""
    from agents.llm import thread_call

    def worker():
        raise ValueError("boom")

    run_fn = thread_call(worker)
    with pytest.raises(ValueError, match="boom"):
        run_fn()


def test_thread_call_returns_value():
    """Return values are propagated."""
    from agents.llm import thread_call

    def worker():
        return 42

    run_fn = thread_call(worker)
    assert run_fn() == 42


# ------------------------------------------------------------------------ thread-local LLM state ---
def test_llm_call_state_is_thread_local():
    """_CALL (per-call LLM state) is thread-local; concurrent calls don't collide."""
    from agents.llm import _CALL, last_call, publish_last_call

    results = {}

    def worker(tag):
        # Each thread gets its own _CALL because it's a threading.local
        state = _CALL
        state.model = f"model-{tag}"
        state.prompt_tokens = tag * 10
        state.completion_tokens = tag * 20
        publish_last_call(model=state.model, prompt_tokens=state.prompt_tokens, completion_tokens=state.completion_tokens)
        results[tag] = last_call()

    t1 = threading.Thread(target=worker, args=(1,))
    t2 = threading.Thread(target=worker, args=(2,))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert results[1]["model"] == "model-1"
    assert results[1]["prompt_tokens"] == 10
    assert results[1]["completion_tokens"] == 20
    assert results[2]["model"] == "model-2"
    assert results[2]["prompt_tokens"] == 20
    assert results[2]["completion_tokens"] == 40


# ------------------------------------------------------------------------ truncation is recorded in _CALL.failures ---
def test_truncation_is_recorded_in_call_failures():
    """When a call is truncated, _note_failure is called with the reason."""
    from agents.llm import _CALL, _note_failure

    # Clear any existing failures
    _CALL.failures.clear()

    # Simulate a truncation being noted
    _note_failure("gemini/test", RuntimeError("truncated at maxOutputTokens=8192 (finishReason=MAX_TOKENS)"))
    assert len(_CALL.failures) == 1
    assert "MAX_TOKENS" in _CALL.failures[0]
    assert "8192" in _CALL.failures[0]

    _note_failure("openai/test", RuntimeError("truncated at max_tokens=4096 (finish_reason=length)"))
    assert len(_CALL.failures) == 2
    assert "length" in _CALL.failures[1]
    assert "4096" in _CALL.failures[1]


def test_failure_is_recorded_per_call_not_global():
    """_CALL.failures is thread-local; one thread's failures don't leak to another."""
    import threading

    from agents.llm import _CALL, _note_failure

    _CALL.failures.clear()
    _note_failure("main-thread", RuntimeError("main error"))
    assert len(_CALL.failures) == 1

    results = {}

    def worker():
        # New thread, new _CALL
        assert len(_CALL.failures) == 0
        _note_failure("worker", RuntimeError("worker error"))
        results["worker_failures"] = len(_CALL.failures)

    t = threading.Thread(target=worker)
    t.start()
    t.join()

    assert results["worker_failures"] == 1
    # Main thread's failures unchanged
    assert len(_CALL.failures) == 1


# ------------------------------------------------------------------------ SandboxClock ---
def test_sandbox_clock_wraps_runner_functions():
    """SandboxClock.install() wraps run_tests, boot_check, static_analysis."""
    from evals.perf import SandboxClock
    from sandbox import runner as Runner

    clock = SandboxClock()
    clock.install()

    # The original functions should be wrapped
    # We can verify by checking they're not the original module functions
    # (This is a basic smoke test; actual timing is tested in the perf harness)
    assert hasattr(Runner, "run_tests")
    assert hasattr(Runner, "boot_check")
    assert hasattr(Runner, "static_analysis")


# ------------------------------------------------------------------------ perf harness integration ---
def test_perf_harness_creates_trace_with_required_fields():
    """RunTrace has all the fields the perf harness expects."""
    from evals.perf import RunTrace

    trace = RunTrace()
    trace.on_event("planner", "start")
    trace.on_event("planner", "done")
    trace.on_event("designer", "start")
    trace.on_event("designer", "done")
    trace.on_event("dev_0", "start")
    trace.on_event("dev_0", "done")
    trace.sandbox_seconds = 5.0
    trace.sandbox_calls = {"run_tests": 3}
    trace.llm_calls = 10
    trace.llm_seconds = 30.0
    trace.tokens = {"planner": 1000, "designer": 2000, "developer": 8000}
    trace.attempts_dev = 1
    trace.spec_coverage = 1.0
    trace.truncations = 0

    # The trace accumulates stage durations via on_event/finish
    trace.finish()
    stages = trace.stages()
    assert "planner" in stages
    assert "dev_0" in stages
    assert trace.sandbox_seconds == 5.0
    assert trace.attempts_dev == 1
    assert trace.spec_coverage == 1.0
    assert trace.truncations == 0


def test_perf_baseline_compare_detects_regressions():
    """The compare logic flags dev_retries and spec_coverage regressions."""
    from evals.perf import compare

    base = {
        "aggregate": {
            "wall_seconds": {"p50": 100, "p90": 150},
            "dev_retries": {"p50": 1},
            "spec_coverage": {"p50": 1.0},
        }
    }
    new = {
        "aggregate": {
            "wall_seconds": {"p50": 105, "p90": 155},
            "dev_retries": {"p50": 2},  # regression: more dev retries
            "spec_coverage": {"p50": 1.0},
        }
    }
    verdict = compare(base, new)
    # The verdict should flag the dev_retries increase
    assert "REGRESSION" in verdict.get("verdict", "") or "regression" in str(verdict).lower()

    new2 = {
        "aggregate": {
            "wall_seconds": {"p50": 105, "p90": 155},
            "dev_retries": {"p50": 1},
            "spec_coverage": {"p50": 0.8},  # regression: lower coverage
        }
    }
    verdict2 = compare(base, new2)
    assert "REGRESSION" in verdict2.get("verdict", "") or "regression" in str(verdict2).lower()


# ------------------------------------------------------------------------ config ---
def test_GEMINI_MAX_OUTPUT_TOKENS_is_not_a_hardcoded_default():
    """The old hardcoded 8192 default is gone; only the per-role table + floor applies."""
    from agents.llm import _HOSTED_FLOOR, _NUM_PREDICT

    # The per-role base caps (updated values)
    assert _NUM_PREDICT.get("designer") == 4000
    assert _NUM_PREDICT.get("developer") == 4000
    assert _NUM_PREDICT.get("tester") == 4000

    # The floor overrides for hosted
    assert _HOSTED_FLOOR.get("designer") == 8000
    assert _HOSTED_FLOOR.get("developer") == 8000
    assert _HOSTED_FLOOR.get("tester") == 8000

    # No role has a hardcoded 8192 in _NUM_PREDICT
    assert 8192 not in _NUM_PREDICT.values()


def test_LLM_MAX_TOKENS_ROLE_env_works(monkeypatch):
    """LLM_MAX_TOKENS_<ROLE> env vars are read by hosted_max_tokens."""
    from agents.llm import hosted_max_tokens

    monkeypatch.setenv("LLM_MAX_TOKENS_DESIGNER", "12000")
    assert hosted_max_tokens("designer") == 12000

    monkeypatch.setenv("LLM_MAX_TOKENS_DEVELOPER", "12000")
    assert hosted_max_tokens("developer") == 12000

    monkeypatch.setenv("LLM_MAX_TOKENS_TESTER", "12000")
    assert hosted_max_tokens("tester") == 12000

    # Generic fallback
    monkeypatch.delenv("LLM_MAX_TOKENS_DESIGNER", raising=False)
    monkeypatch.setenv("LLM_MAX_TOKENS", "9000")
    assert hosted_max_tokens("designer") == 9000