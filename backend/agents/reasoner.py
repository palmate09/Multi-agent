"""Reasoner: think the requirement through before anything is designed.

The pipeline used to go straight from stories to a spec, and a spec is the
cheapest place to be wrong in an expensive way: one misread requirement costs a
contract, a codebase and a test suite before anyone notices. This node asks for
the decomposition first and hands the answer to the Designer and Developer.

It is deliberately *not* a replacement for the PM's stories. The PM splits the
requirement into things to build; the Reasoner decides how they fit together,
what can go wrong, and which edge cases a later agent will otherwise discover
by failing a test.

The result is advisory: nothing validates the generated code against it, and it
never reaches the Tester or the Reviewer, whose judgements have to stay
independent of it. A reply that never parses as JSON keeps its own words in
``approach`` rather than failing a run that would otherwise have succeeded —
that is the model's reasoning, not a canned template.
"""

from __future__ import annotations

import json

from agents.blocks import strip_fence
from agents.llm import require
from schemas.messages import Plan, UserStories

SOP = (
    "You are the Reasoner on a software team. Read the requirement and the "
    "stories, then think it through before anyone writes a spec.\n"
    "Decide the approach, name the decisions that constrain the implementation, "
    "the risks that could make it fail, the edge cases the tests must cover, and "
    "any ambiguity that is still open.\n"
    "Be concrete: reference the entities and behaviours this requirement names, "
    "never generic advice.\n"
    "Reply with one fenced json block and no prose around it:\n"
    "```json\n"
    '{"approach": "...", "decisions": ["..."], "risks": ["..."], '
    '"edge_cases": ["..."], "open_questions": ["..."]}\n'
    "```"
)

_LIST_KEYS = ("decisions", "risks", "edge_cases", "open_questions")


def _parse(text: str) -> Plan | None:
    """Extract the Plan from a reply, tolerating prose and fences."""
    if not text:
        return None
    body = strip_fence(text)
    start = body.find("{")
    end = body.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        data = json.loads(body[start : end + 1])
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    approach = data.get("approach")
    if not isinstance(approach, str) or not approach.strip():
        return None
    lists: dict[str, list[str]] = {}
    for key in _LIST_KEYS:
        raw = data.get(key)
        lists[key] = (
            [str(i).strip() for i in raw if str(i).strip()] if isinstance(raw, list) else []
        )
    return Plan(approach=approach.strip(), structured=True, **lists)


def plan(requirement: str, stories: UserStories | None = None) -> Plan:
    """Reason about the requirement. Raises ``GenerationError`` if no backend
    answers — an advisory step is still a step the pipeline cannot invent.

    Structured output is retried, then degraded rather than raised: three
    attempts that come back as prose would otherwise block a run whose spec,
    code and tests were all going to be produced from the requirement anyway.
    """
    prompt = f"Requirement:\n{requirement}\n"
    if stories is not None:
        listing = "\n".join(
            f"- {s.id}: {s.title} ({'; '.join(s.acceptance)})" for s in stories.stories
        )
        prompt += f"\nStories:\n{listing}\n"
    for attempt in range(3):
        extra = "\nReply with the json block only." if attempt else ""
        text, _meta = require(
            f"{prompt}{extra}Output the json block.", SOP, role="reasoner"
        )
        parsed = _parse(text)
        if parsed is not None:
            return parsed
    return Plan(approach=text.strip()[:1500], structured=False)


def plan_block(plan: Plan | None) -> str:
    """Prompt fragment handing the plan to another agent; ``""`` when absent.

    Labelled advisory in the text itself: the Designer is about to be graded on
    a valid contract, not on how closely it followed a suggestion.
    """
    if plan is None:
        return ""
    sections: list[str] = []
    if plan.approach.strip():
        sections.append(f"Approach:\n{plan.approach.strip()}")
    for title, key in (
        ("Decisions", "decisions"),
        ("Risks", "risks"),
        ("Edge cases", "edge_cases"),
        ("Open questions", "open_questions"),
    ):
        items = getattr(plan, key)
        if items:
            sections.append(f"{title}:\n" + "\n".join(f"- {i}" for i in items))
    if not sections:
        return ""
    return (
        "\nReasoning plan (advisory; the contract is what counts):\n"
        + "\n\n".join(sections)
        + "\n"
    )
