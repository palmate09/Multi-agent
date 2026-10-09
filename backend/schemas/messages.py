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
    # The file layout the Designer intends, decided from the requirement's
    # complexity rather than a fixed template. ``entrypoint`` is the module the
    # Developer must create that holds the ASGI app object.
    entrypoint: str = "main.py"
    files: list[str] = []
    # Entities the requirement named, used by the coverage gate so a generation
    # that silently substitutes a different domain cannot pass.
    domain_terms: list[str] = []

    # ---- non-service projects ----
    # "api" (the default) means an HTTP service: the contract above is OpenAPI
    # and the app object is discovered from the code. "program" means a
    # self-contained piece of software such as a game or a library, where an
    # OpenAPI document would be an invention rather than a translation.
    kind: Literal["api", "program"] = "api"
    # For kind="program": a prose contract naming the modules, the entrypoint,
    # the public interface and the behaviours that must hold.
    contract_text: str = ""
    # For kind="program": the symbols the implementation must expose, checked by
    # the boot check and the Reviewer.
    public_api: list[str] = []


class CodeBundle(BaseModel):
    files: dict[str, str]  # path -> content
    # Module and attribute the ASGI app is exposed as, e.g. ("app", "app").
    # Discovered from the generated code rather than assumed to be "main".
    entry_module: str = ""
    entry_attr: str = ""


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
        # Must also require that tests actually ran. A collection error, an
        # import crash or a timeout all report 0 passed / 0 failed, which
        # would otherwise be indistinguishable from a green run.
        return self.failed == 0 and self.passed > 0


class ReviewComment(BaseModel):
    __test__ = False
    severity: Literal["blocker", "major", "minor"]
    message: str
    location: str = ""


class CoverageReport(BaseModel):
    """Whether the generated code actually implements the requested domain."""

    __test__ = False
    covered: list[str] = []
    missing: list[str] = []
    # False when the requirement was too abstract to extract terms from, in
    # which case the gate is advisory and must not fail the run.
    conclusive: bool = True

    @property
    def ok(self) -> bool:
        return self.conclusive and not self.missing


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
    coverage: CoverageReport | None = None
    memory: list[str] = []
    retry_dev: int = 0
    retry_test: int = 0
    retry_review: int = 0
    status: str = "pending"
    # Populated when the run stops early so the failure is explainable rather
    # than presenting as a successful run with default metrics.
    error: str = ""
