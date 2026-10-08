"""Typed messages between agents (Pydantic v2). Phase 1 contract."""
from __future__ import annotations
from typing import Dict, List, Literal, Optional
from pydantic import BaseModel, Field


class Requirement(BaseModel):
    text: str = Field(min_length=1)
    run_id: str = "default"


class UserStory(BaseModel):
    id: str
    title: str
    acceptance: List[str]


class UserStories(BaseModel):
    stories: List[UserStory]
    clarifying_question: Optional[str] = None


class ApiSpec(BaseModel):
    openapi_yaml: str
    endpoints: List[str] = []
    models: List[str] = []


class CodeBundle(BaseModel):
    files: Dict[str, str]  # path -> content


class TestSuite(BaseModel):
    __test__ = False
    files: Dict[str, str]


class TestFailure(BaseModel):
    __test__ = False
    name: str
    error: str
    trace: str = ""


class TestReport(BaseModel):
    __test__ = False
    passed: int = 0
    failed: int = 0
    failures: List[TestFailure] = []
    raw: str = ""

    @property
    def ok(self) -> bool:
        return self.failed == 0


class ReviewComment(BaseModel):
    severity: Literal["blocker", "major", "minor"]
    message: str
    location: str = ""


class ReviewReport(BaseModel):
    comments: List[ReviewComment] = []

    @property
    def blockers(self) -> List[ReviewComment]:
        return [c for c in self.comments if c.severity == "blocker"]


class GraphState(BaseModel):
    requirement: str = ""
    run_id: str = "default"
    stories: Optional[UserStories] = None
    spec: Optional[ApiSpec] = None
    code: Optional[CodeBundle] = None
    tests: Optional[TestSuite] = None
    report: Optional[TestReport] = None
    review: Optional[ReviewReport] = None
    memory: List[str] = []
    retry_dev: int = 0
    retry_test: int = 0
    retry_review: int = 0
    status: str = "pending"
