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


class Plan(BaseModel):
    """What the Reasoner worked out before anything is designed.

    Advisory rather than binding: it is handed to the Designer and Developer as
    context and written to ``reasoning.json`` for the human, but nothing
    validates the generated code against it — the OpenAPI contract and the
    public interface stay the only things a run is graded on.

    ``structured`` is False when the model replied in prose and the JSON block
    never parsed; ``approach`` then carries its raw words, which are still the
    model's own reasoning rather than a canned answer.
    """

    approach: str = ""
    decisions: list[str] = []
    risks: list[str] = []
    edge_cases: list[str] = []
    open_questions: list[str] = []
    structured: bool = True


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

    # Whether this run should also generate a web UI for the API above. A
    # boolean rather than a third ``kind``: only api-kind projects get a UI, and
    # ``kind`` is threaded through eight modules, so widening it would buy
    # nothing and cost a migration. Defaults to off, so a run that does not ask
    # for a UI behaves exactly as it did before.
    has_ui: bool = False


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


class UiCheck(BaseModel):
    """One static UI gate check. ``ok`` is a fact about the files, not a verdict."""

    __test__ = False
    name: str
    ok: bool
    detail: str = ""


class UiSpec(BaseModel):
    """The UI Designer's contract: what the pages are and what they read.

    ``data_bindings`` are the load-bearing part. Each entry names a
    ``METHOD /path`` that the UI actually calls, which is what lets the gate
    check the UI against the generated API rather than trusting the model's own
    summary of what it built.
    """

    __test__ = False
    pages: list[str] = []
    components: list[str] = []
    data_bindings: list[str] = []
    files: list[str] = []
    contract_text: str = ""


class UiReport(BaseModel):
    """The UI gate's verdict.

    Deliberately not a gate: a 3b model producing a mediocre UI must never turn
    a green, reviewed backend run into a failure. ``ok`` is reported to the user
    and to the eval metrics, and nothing else reads it.
    """

    __test__ = False
    ok: bool = True
    checks: list[UiCheck] = []
    probed: list[str] = []

    @property
    def failures(self) -> list[UiCheck]:
        return [c for c in self.checks if not c.ok]


class GraphState(BaseModel):
    requirement: str = ""
    run_id: str = "default"
    stories: UserStories | None = None
    # The Reasoner's read on the requirement; None when the node is ablated.
    plan: Plan | None = None
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
    # ---- full-stack generation ----
    ui_spec: UiSpec | None = None
    ui_code: CodeBundle | None = None
    ui_report: UiReport | None = None
    # Set when the run asked for a UI but produced none (no backend, or a
    # non-api project). The UI nodes are then skipped rather than retried, and
    # the terminal status is whatever the backend half decided.
    skip_ui: bool = False
    status: str = "pending"
    # Populated when the run stops early so the failure is explainable rather
    # than presenting as a successful run with default metrics.
    error: str = ""
