"""Project Manager: requirement -> stories, and failure triage.

The template here used to be six hardcoded task-manager stories returned
whenever generation failed. That is why a library requirement produced task
stories. There is no template now: a failure raises and the run is blocked.
"""

from __future__ import annotations

import json

from agents.llm import GenerationError, require
from schemas.messages import TestReport, UserStories, UserStory

SOP = (
    "You are the Project Manager. Break the requirement into 4-7 user stories.\n"
    "Each story needs an id (US1, US2...), a short title, and 1-3 acceptance "
    "criteria that are concrete and testable (name the status codes and fields).\n"
    "Reply with JSON only, no prose and no markdown fence:\n"
    '{"stories": [{"id": "US1", "title": "...", "acceptance": ["..."]}], '
    '"clarifying_question": null}\n'
    "Set clarifying_question to a single question only when the requirement is "
    "genuinely ambiguous."
)


def _parse(text: str) -> UserStories | None:
    """Extract the JSON object from a reply, tolerating prose and fences."""
    if not text:
        return None
    body = text
    if "```" in body:
        try:
            from agents.blocks import strip_fence

            body = strip_fence(text)
        except Exception:
            pass
    start = body.find("{")
    end = body.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        data = json.loads(body[start : end + 1])
        raw = data.get("stories")
        if not isinstance(raw, list) or not raw:
            return None
        stories = [UserStory(**s) for s in raw]
        if not stories:
            return None
        return UserStories(
            stories=stories,
            clarifying_question=data.get("clarifying_question") or None,
        )
    except Exception:
        return None


def requirement_to_stories(requirement: str) -> UserStories:
    """Generate stories. Raises ``GenerationError`` if none can be produced.

    Retries once with a stricter instruction, because a small local model
    occasionally wraps its JSON in prose or a fence.
    """
    errors: list[str] = []
    for attempt in range(2):
        extra = "\nReply with the JSON object only." if attempt else ""
        try:
            text, _meta = require(f"Requirement:\n{requirement}\n{extra}", SOP, role="pm")
        except GenerationError as exc:
            errors.append(str(exc))
            continue
        parsed = _parse(text)
        if parsed is not None:
            return parsed
        errors.append("reply was not parseable JSON with a non-empty stories list")
    raise GenerationError("PM could not produce user stories: " + "; ".join(errors))


# Deterministic signatures of a *test* bug. These decide on their own: an
# earlier revision let the LLM overrule them, and a real run rewrote correct
# code four times because the model judged a status-code-correct response a
# "code_bug".
_STRONG_TEST_BUG = (
    "assert 'error' in",
    'assert "error" in',
    "assert 'detail' in",
    'assert "detail" in',
    "in response.json()",
    "in r.json()",
)

# Softer hints the LLM may still overrule.
_WEAK_TEST_BUG = (
    "assert 200 == 201",
    "assert 201 == 200",
    "expected status",
    "test expects",
    "wrong url",
    "test bug",
)


def triage(report: TestReport) -> str:
    """Return ``code_bug`` or ``test_bug``.

    A deterministic failure signature decides on its own where it is
    unambiguous; otherwise the LLM is asked. An empty failure list is
    ``code_bug`` because the loop only triages after pytest reported red.
    """
    if not report.failures:
        return "code_bug"
    blob = " ".join(f.name + " " + f.error for f in report.failures).lower()

    # Only meaningful when other tests pass. A suite failing everywhere is a
    # broken application, not a bad expectation.
    enough_passing = report.passed >= 3

    if enough_passing and any(m in blob for m in _STRONG_TEST_BUG):
        # Decided without consulting the model. This shape of failure is
        # unambiguous, and the model previously talked itself out of it: a real
        # run rewrote correct code four times because it judged a
        # status-code-correct response "code_bug".
        return "test_bug"

    verdict = (
        "test_bug" if enough_passing and any(m in blob for m in _WEAK_TEST_BUG) else "code_bug"
    )
    try:
        text, _meta = require(
            f"Failures:\n{blob[:1500]}\n\nThe tests assert the status codes the spec declares. "
            "Is the application wrong, or did the test assert something the spec never "
            "promised (such as a specific error-body shape)? "
            "Reply with exactly one word: code_bug or test_bug.",
            "You triage test failures. A test asserting an error-body key the spec never "
            "defined is a test_bug, not a code_bug.",
            role="triage",
        )
    except GenerationError:
        return verdict
    t = (text or "").strip().lower()
    if "test_bug" in t:
        return "test_bug"
    if "code_bug" in t:
        return "code_bug"
    return verdict
