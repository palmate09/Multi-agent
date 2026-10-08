import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def test_stories_valid():
    from agents.pm import requirement_to_stories

    s = requirement_to_stories("task manager")
    assert len(s.stories) >= 4


def test_spec_valid():
    from agents.designer import stories_to_spec
    from agents.pm import requirement_to_stories

    spec = stories_to_spec(requirement_to_stories("task manager"))
    assert "/tasks" in spec.openapi_yaml


def test_code_boots_and_tests_green():
    from agents.developer import _template_bundle
    from agents.tester import TEMPLATE_TESTS
    from sandbox.runner import boot_check, run_tests
    from schemas.messages import TestSuite

    code = _template_bundle()
    ok, _ = boot_check(code)
    assert ok
    rep = run_tests(code, TestSuite(files={"test_tasks.py": TEMPLATE_TESTS}))
    assert rep.ok, rep.raw[-2000:]
