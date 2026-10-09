"""Project Manager: requirement -> stories, and failure triage.

The template here used to be six hardcoded task-manager stories returned
whenever generation failed. That is why a library requirement produced task
stories. There is no template now: a failure raises and the run is blocked.
"""

from __future__ import annotations

import json
import re

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
    "assert 'message' in",
    'assert "message" in',
    "in response.json()",
    "in r.json()",
)

# The test assumed an interface the contract never declared: a getter that was
# never implemented, a dict field the object never had, or an HTTP-style
# response envelope on a plain function. Each of these sends the Developer off
# to "fix" correct code, because no implementation change can satisfy them.
_TEST_INVENTED_INTERFACE = re.compile(
    r"attributeerror:.*object has no attribute '(?:get_|is_|has_)"
    r"|attributeerror:.*has no setter"
    r"|typeerror: attribute name must be string"
    r"|typeerror: string indices must be integers"
    r"|typeerror: list indices must be integers"
    r"|keyerror: '[a-z][a-z0-9_]*'"
    r"|assert \d{3} in \[\d{3}"
    r"|where \d+ = get_current_player\(\)"
    r"|not found in game object|hasattr\(game, attr\)",
    re.I,
)

# A test that times out because it loops until a condition the implementation
# may never satisfy (e.g. `while not game.is_game_over()`). The developer cannot
# fix this; the test needs a bounded loop or a direct state setup.
_TEST_TIMEOUT = re.compile(r"pytest timeout|timed out|timeout", re.I)

# The suite cannot even be collected: the generated test file does not parse
# (an unterminated docstring, a stray fence). Nothing about the application has
# been exercised at this point, so the failure says nothing about the code.
# Not gated on the pass count: with zero collected tests there is no pass count.
_TEST_COLLECTION_ERROR = re.compile(
    r"error collecting|mod = import_path|errors? on import|"
    r"syntaxerror:.*(?:unterminated|expected|invalid)",
    re.I,
)

# A test that patches a symbol the implementation never imports or calls, so the
# patch silently does nothing and the assertion fails on unchanged state.
_PATCHED_BUT_NO_EFFECT = re.compile(
    r"assert\s+\d+\s*==\s*\d+", re.I
)

# Softer hints the LLM may still overrule.
_WEAK_TEST_BUG = (
    "assert 200 == 201",
    "assert 201 == 200",
    "expected status",
    "test expects",
    "wrong url",
    "test bug",
    # HTTP-style assertions against a non-HTTP program: the contract declares
    # plain return values, so a {'status': 200} wrapper was invented by the test.
    "status.*200",
    "status.*400",
    "status.*404",
    "dice_resp",
    "move_resp",
)

# A test asserting an error status where the app returned a success status. The
# request was valid, so the expectation is impossible to satisfy. Seen in a real
# run where the suite did GET /appointments/ and demanded 4xx, while a
# trailing-slash request on a collection is a legitimate list request returning
# 200. Routing that to the Developer sends it "fixing" correct code.
_2XX_VS_ERROR_ASSERT = re.compile(
    r"assert\s+4\d\d\s*(?:<=|==)\s*resp(?:onse)?\.?status_code"  # assert 4xx <= resp.status_code
    r"|assert\s+resp(?:onse)?\.?status_code\s*(?:<|<=)\s*500"
    r"|= <Response \[2\d\d",
    re.I,
)


def triage(report: TestReport) -> str:
    """Return ``code_bug`` or ``test_bug``.

    A deterministic failure signature decides on its own where it is
    unambiguous; otherwise the LLM is asked. An empty failure list is
    ``code_bug`` because the loop only triages after pytest reported red.
    """
    if not report.failures:
        return "code_bug"
    # The raw output matters: a suite that fails to even be collected reports
    # no individual failures, so a blob built from them alone is empty.
    blob = (
        " ".join(f.name + " " + f.error for f in report.failures)
        + " "
        + (report.raw or "")
    ).lower()

    # Only meaningful when other tests pass. A suite failing everywhere is a
    # broken application, not a bad expectation.
    enough_passing = report.passed >= 3

    if enough_passing and any(m in blob for m in _STRONG_TEST_BUG):
        # Decided without consulting the model. This shape of failure is
        # unambiguous, and the model previously talked itself out of it: a real
        # run rewrote correct code four times because it judged a
        # status-code-correct response "code_bug".
        return "test_bug"

    if enough_passing and _2XX_VS_ERROR_ASSERT.search(blob):
        # The app answered 2xx to a valid request while the test insisted on an
        # error status. No implementation can satisfy that.
        return "test_bug"

    if _TEST_INVENTED_INTERFACE.search(blob):
        # The test drove an interface the contract never declared. No code
        # change can satisfy it, so it has to be rewritten against the contract.
        return "test_bug"

    if _TEST_COLLECTION_ERROR.search(blob):
        # The suite did not parse, so no assertion ever ran. This is the Tester's
        # output being malformed, not a statement about the application.
        return "test_bug"

    if _TEST_TIMEOUT.search(blob):
        # A test that times out is almost always looping until a condition the
        # implementation may never satisfy. The developer cannot fix this; the
        # test needs a bounded loop or a direct state setup.
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
