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


def test_generate_falls_back_to_template_without_backends(monkeypatch):
    monkeypatch.setenv("SKIP_OLLAMA", "1")
    monkeypatch.setenv("AGENT_TEAM_TESTING", "1")
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
    text, meta = llm.generate("unique prompt for fallback test", role="developer")
    assert text == ""
    assert meta["model"] == "template"


def test_generate_caches_successful_responses(monkeypatch):
    monkeypatch.setenv("SKIP_OLLAMA", "1")
    monkeypatch.setenv("AGENT_TEAM_TESTING", "1")
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
    assert meta2 == {"model": "cache", "cached": True}
    assert len(calls) == 1, "second identical call must not hit the network"

    llm.CACHE.clear()
