"""Typed messages between agents (Pydantic v2). Phase 1 contract."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class Requirement(BaseModel):
    text: str = Field(min_length=1)
    run_id: str = "default"


class UserStory(BaseModel):
    id: str
    title: str
    acceptance: list[str]


class UserStories(BaseModel):
    stories: list[UserStory]
    clarifying_question: str | None = None


class ApiSpec(BaseModel):
    openapi_yaml: str
    endpoints: list[str] = []
    models: list[str] = []


class CodeBundle(BaseModel):
    files: dict[str, str]  # path -> content


class TestSuite(BaseModel):
    __test__ = False
    files: dict[str, str]


class TestFailure(BaseModel):
    __test__ = False
    name: str
    error: str
    trace: str = ""


class TestReport(BaseModel):
    __test__ = False
    passed: int = 0
    failed: int = 0
    failures: list[TestFailure] = []
    raw: str = ""

    @property
    def ok(self) -> bool:
        return self.failed == 0


class ReviewComment(BaseModel):
    severity: Literal["blocker", "major", "minor"]
    message: str
    location: str = ""


class ReviewReport(BaseModel):
    comments: list[ReviewComment] = []

    @property
    def blockers(self) -> list[ReviewComment]:
        return [c for c in self.comments if c.severity == "blocker"]


class GraphState(BaseModel):
    requirement: str = ""
    run_id: str = "default"
    stories: UserStories | None = None
    spec: ApiSpec | None = None
    code: CodeBundle | None = None
    tests: TestSuite | None = None
    report: TestReport | None = None
    review: ReviewReport | None = None
    memory: list[str] = []
    retry_dev: int = 0
    retry_test: int = 0
    retry_review: int = 0
    status: str = "pending"
