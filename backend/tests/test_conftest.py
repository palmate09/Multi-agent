"""Regression tests for the pytest collection hook itself.

CI once died with INTERNALERROR because the hook called ``pytest.skip()``
when Ollama was unreachable. Raising Skipped inside
``pytest_collection_modifyitems`` aborts the whole session on current
pluggy/pytest instead of skipping anything, so the hook must deselect.
"""

import importlib.util
import json
from pathlib import Path


def _load_hook():
    path = Path(__file__).resolve().parent / "conftest.py"
    spec = importlib.util.spec_from_file_location("team_conftest", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.pytest_collection_modifyitems


class _Item:
    def __init__(self, name, live=False):
        self.name = name
        self._live = live

    def get_closest_marker(self, name):
        return object() if (name == "live_llm" and self._live) else None


class _HookRecorder:
    def __init__(self):
        self.deselected = None

    def pytest_deselected(self, items):
        self.deselected = list(items)


class _Config:
    def __init__(self, recorder):
        self.hook = recorder


class _Response:
    def __init__(self, payload):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return json.dumps(self._payload).encode()


def test_hook_deselects_live_tests_when_ollama_is_down(monkeypatch):
    """No Ollama must deselect, never raise: this was CI's INTERNALERROR."""
    hook = _load_hook()

    def _down(*a, **k):
        raise ConnectionError("no Ollama on localhost:11434")

    monkeypatch.setattr("urllib.request.urlopen", _down)
    recorder = _HookRecorder()
    items = [_Item("test_a"), _Item("test_live", live=True)]
    hook(_Config(recorder), items)  # must not raise
    assert [i.name for i in items] == ["test_a"]
    assert recorder.deselected is not None
    assert [i.name for i in recorder.deselected] == ["test_live"]


def test_hook_keeps_live_tests_when_model_is_present(monkeypatch):
    hook = _load_hook()
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda *a, **k: _Response({"models": [{"name": "qwen2.5-coder:7b"}]}),
    )
    recorder = _HookRecorder()
    items = [_Item("test_a"), _Item("test_live", live=True)]
    hook(_Config(recorder), items)
    assert [i.name for i in items] == ["test_a", "test_live"]
    assert recorder.deselected is None


def test_hook_ignores_suites_without_live_tests(monkeypatch):
    """The Ollama probe must not even run when nothing is marked."""
    hook = _load_hook()

    def _boom(*a, **k):
        raise AssertionError("probe ran with no live_llm items collected")

    monkeypatch.setattr("urllib.request.urlopen", _boom)
    items = [_Item("test_a"), _Item("test_b")]
    hook(_Config(_HookRecorder()), items)
    assert [i.name for i in items] == ["test_a", "test_b"]
