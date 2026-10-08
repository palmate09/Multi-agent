"""Project Manager: requirement -> stories + triage. SOP from MetaGPT, clarifying-Q from ChatDev."""
from __future__ import annotations
from schemas.messages import UserStories, UserStory, TestReport
from agents.llm import generate
import json

SOP = ("You are the Project Manager. Turn the requirement into 4-7 user stories, "
       "each with 1-3 testable acceptance criteria. If ambiguous, ask ONE clarifying "
       "question first. Output JSON: {stories:[{id,title,acceptance[]}], clarifying_question?}.")


def requirement_to_stories(requirement: str) -> UserStories:
    text, meta = generate(f"Requirement:\n{requirement}\nOutput JSON only.", SOP, role="pm")
    if text and "{" in text:
        try:
            start = text.index("{")
            data = json.loads(text[start:text.rindex("}") + 1])
            return UserStories(stories=[UserStory(**s) for s in data["stories"]],
                               clarifying_question=data.get("clarifying_question"))
        except Exception:
            pass
    # Template fallback: simple task-manager CRUD stories
    return UserStories(
        stories=[
            UserStory(id="US1", title="Health check", acceptance=["GET /health returns 200 {status: ok}"]),
            UserStory(id="US2", title="Create task", acceptance=["POST /tasks with title returns 201 + id", "POST /tasks without title returns 422"]),
            UserStory(id="US3", title="List tasks", acceptance=["GET /tasks returns list containing created task"]),
            UserStory(id="US4", title="Get task", acceptance=["GET /tasks/{id} returns task", "GET /tasks/999999 returns 404"]),
            UserStory(id="US5", title="Update task", acceptance=["PUT /tasks/{id} updates fields", "PUT /tasks/999999 returns 404"]),
            UserStory(id="US6", title="Delete task", acceptance=["DELETE /tasks/{id} returns 200", "GET after delete returns 404"]),
        ],
        clarifying_question=None if "task" in requirement.lower() else "Is this a task-manager CRUD API with SQLite persistence?",
    )


def triage(report: TestReport) -> str:
    """Return 'code_bug' or 'test_bug'. Heuristic + LLM override."""
    if not report.failures:
        return "code_bug"
    blob = " ".join(f.name + " " + f.error for f in report.failures).lower()
    test_markers = ["assert 200 == 201", "assert 201 == 200", "keyerror: 'id'",
                    "expected status", "test expects", "wrong url", "test bug"]
    verdict = "test_bug" if any(m in blob for m in test_markers) and report.passed >= 3 else "code_bug"
    text, _ = generate(f"Failures:\n{blob[:1500]}\nReply with one word: code_bug or test_bug.",
                       "You triage test failures.", role="triage")
    t = (text or "").strip().lower()
    if "test_bug" in t:
        return "test_bug"
    if "code_bug" in t:
        return "code_bug"
    return verdict
