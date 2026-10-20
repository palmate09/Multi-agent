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
    "You are the Project Manager. Read the requirement and think it through "
    "before writing anything.\n"
    "1) Identify the actors and roles, the resources/entities and their "
    "relationships, the full lifecycle of each resource (create, read, update, "
    "delete, list/search, report), the business rules and validations, and the "
    "failure cases. Name them concretely from the requirement, never generically.\n"
    "2) Judge the delivery complexity:\n"
    "   simple: one resource with basic CRUD, no cross-resource workflow.\n"
    "   moderate: two to four related resources or a multi-step workflow.\n"
    "   complex: five or more resources, roles/permissions, a state machine, "
    "scheduling/booking, payments, or reporting/analytics.\n"
    "   very_complex: multiple interacting subsystems with concurrency, "
    "background jobs, integrations, or real-time behaviour.\n"
    "3) Stay inside the requirement. Cover the surface it implies -- create, list, "
    "read, update, delete, the actions it names, the validations and the failure "
    "cases -- and nothing beyond it. Do not add accounts, roles, permissions, "
    "payments, notifications, sharing or ratings unless the requirement names them "
    "or clearly implies them. A deeper story list is not a licence to build a "
    "different product.\n"
    "4) Write one user story per distinct user-visible capability. The count "
    "MUST follow the complexity; never default to five and never pad:\n"
    "   simple: 3-5 stories, moderate: 6-9, complex: 10-16, "
    "very_complex: 17-25.\n"
    "Every story needs an id (US1, US2...), a short title, and 2-4 acceptance "
    "criteria that are concrete and testable: name the method, path, status "
    "codes and fields where relevant, and state the edge case it covers.\n"
    "Reply with JSON only, no prose and no markdown fence:\n"
    '{"analysis": "2-4 sentences naming the actors, entities, workflows, '
    'rules and failure cases you identified", '
    '"complexity": "simple|moderate|complex|very_complex", '
    '"stories": [{"id": "US1", "title": "...", "acceptance": ["..."]}], '
    '"clarifying_question": null}\n'
    "Set clarifying_question to a single question only when the requirement is "
    "genuinely ambiguous. Never invent a different product."
)

# Story-count bands per complexity. The PM is asked for these ranges; a reply
# outside the band is retried once rather than silently accepting five stories
# for a twenty-entity system.
_BANDS = {
    "simple": (3, 5),
    "moderate": (6, 9),
    "complex": (10, 16),
    "very_complex": (17, 25),
}

_COMPLEXITY_ALIASES = {
    "simple": "simple",
    "low": "simple",
    "moderate": "moderate",
    "medium": "moderate",
    "intermediate": "moderate",
    "complex": "complex",
    "high": "complex",
    "very_complex": "very_complex",
    "very_high": "very_complex",
    "enterprise": "very_complex",
}


def _normalise_complexity(raw: object) -> str | None:
    if not isinstance(raw, str):
        return None
    key = re.sub(r"[^a-z]+", "_", raw.strip().lower()).strip("_")
    return _COMPLEXITY_ALIASES.get(key)


def _story_count_problem(stories: UserStories) -> str | None:
    """Why the story list is the wrong size for the declared complexity."""
    if stories.complexity not in _BANDS:
        return None
    low, high = _BANDS[stories.complexity]
    count = len(stories.stories)
    if low <= count <= high:
        return None
    return (
        f"complexity {stories.complexity!r} calls for {low}-{high} stories, "
        f"but the reply had {count}"
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
            analysis=str(data.get("analysis") or "").strip()[:1500],
            complexity=_normalise_complexity(data.get("complexity")),
        )
    except Exception:
        return None


_FOLLOWUP_SOP = (
    "You are the Project Manager, and this is a CHANGE to an app that already "
    "exists. The stories below are the current state of the product. Apply the "
    "change request to them.\n"
    "Rules:\n"
    "- Your reply must contain EVERY story that already exists, with the same "
    "ids, plus the new ones. Edit an existing story's acceptance criteria only "
    "where the change alters that behaviour; leave the rest untouched.\n"
    "- Add one story per new user-visible capability the request implies. Do not "
    "re-list the whole product and do not renumber the existing stories.\n"
    "- Stay inside the request: it asks for a change, not a different product. "
    "Do not add accounts, roles, payments, notifications or features it never "
    "mentions.\n"
    "- The analysis explains what the change means for the app: the entities it "
    "touches, the rules it adds, and the failure cases it introduces.\n"
    "Reply with JSON only, no prose and no markdown fence:\n"
    '{"analysis": "2-4 sentences about the change", '
    '"complexity": "simple|moderate|complex|very_complex", '
    '"stories": [{"id": "US1", "title": "...", "acceptance": ["..."]}], '
    '"clarifying_question": null}\n'
)


def update_stories(requirement: str, request: str, previous: UserStories) -> UserStories:
    """The stories of an existing app plus one change.

    A follow-up must *evolve* the product, not restart it: the previous
    stories are the base, and the request is the delta. Starting from scratch
    is how a follow-up silently drops a capability the user still wants.
    """
    listing = "\n".join(
        f"- {s.id}: {s.title} ({'; '.join(s.acceptance)})" for s in previous.stories
    )
    prompt = (
        f"Original requirement:\n{requirement}\n\n"
        f"Current stories:\n{listing}\n\n"
        f"Change request:\n{request}\n"
    )
    previous_ids = {s.id for s in previous.stories}
    errors: list[str] = []
    correction = ""
    for _ in range(3):
        text, _meta = require(
            f"{prompt}{correction}Reply with the JSON object only.", _FOLLOWUP_SOP, role="pm"
        )
        parsed = _parse(text)
        if parsed is None:
            errors.append("reply was not parseable JSON with a non-empty stories list")
            continue
        # Every story that already exists must still be there. A follow-up that
        # quietly drops a capability is the worst way to lose one: the person
        # asked for a change, not for a smaller product.
        dropped = sorted(previous_ids - {s.id for s in parsed.stories})
        if dropped:
            errors.append(f"the reply dropped existing stories: {dropped}")
            correction = (
                f"\nYour previous reply dropped these existing stories: "
                f"{', '.join(dropped)}. Every story that already exists must be in the "
                "reply again, unchanged unless the change alters it, plus the new ones.\n"
            )
            continue
        return parsed
    raise GenerationError("PM could not apply the change to the stories: " + "; ".join(errors))


def requirement_to_stories(requirement: str) -> UserStories:
    """Generate depth-first, complexity-sized stories.

    A small model occasionally wraps its JSON in prose or a fence, and it will
    happily return five stories for a system with twenty entities if nobody
    checks. The first problem is retried with the exact correction; a story
    count outside the declared complexity band is retried too. If the model
    still disagrees after three attempts, its last usable answer is returned
    rather than blocking an otherwise generatable run.
    """
    errors: list[str] = []
    last_good: UserStories | None = None
    correction = ""
    for _attempt in range(3):
        try:
            text, _meta = require(
                f"Requirement:\n{requirement}\n{correction}Reply with the JSON object only.",
                SOP,
                role="pm",
            )
        except GenerationError as exc:
            errors.append(str(exc))
            continue
        parsed = _parse(text)
        if parsed is None:
            errors.append("reply was not parseable JSON with a non-empty stories list")
            correction = (
                "\nYour previous reply was not valid JSON. Reply with the JSON object only, "
                "including 'analysis', 'complexity' and a non-empty 'stories' list.\n"
            )
            continue
        last_good = parsed
        why = _story_count_problem(parsed)
        if why is None:
            return parsed
        errors.append(why)
        correction = (
            f"\nCorrection: {why}. Keep the same requirement and complexity, but add "
            "the missing distinct capabilities (or remove padding) so the count fits the "
            "band. Rewrite the complete JSON object.\n"
        )
    if last_good is not None:
        return last_good
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
_PATCHED_BUT_NO_EFFECT = re.compile(r"assert\s+\d+\s*==\s*\d+", re.I)

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

# The app crashes copying a live resource: a route handler takes a session
# object as a default argument (``db=SessionLocal()``), FastAPI deepcopies
# parameter defaults when it builds each route, and a SQLAlchemy session
# references the DBAPI module, which cannot be copied
# (``TypeError: cannot pickle 'module'``). Seen in a real run where all 9
# tests failed identically and the LLM triage misrouted to the Tester twice
# before the Developer stalled with no change. Only the implementation can fix
# this, so it leads the deterministic checks: an app-runtime traceback naming
# the ORM can never be a bad test expectation.
_SESSION_COPY_CTX = ("sqlalchemy", "deepcopy", "dbapi", "sessionmaker")


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
        " ".join(f.name + " " + f.error for f in report.failures) + " " + (report.raw or "")
    ).lower()

    # Only meaningful when other tests pass. A suite failing everywhere is a
    # broken application, not a bad expectation.
    enough_passing = report.passed >= 3

    if "cannot pickle" in blob and any(m in blob for m in _SESSION_COPY_CTX):
        return "code_bug"

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
