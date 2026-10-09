"""Wire schemas for the HTTP API (separate from agent message contracts)."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

RunStatus = Literal[
    "queued",
    "running",
    "accepted",
    "accepted_no_review",
    "tests_green",
    "unresolved",
    "unresolved_review",
    "failed",
]


class RunCreate(BaseModel):
    requirement: str = Field(min_length=1, max_length=8000)
    skip_tester: bool = False
    skip_reviewer: bool = False
    # Ablate the plan-then-act node, for comparing a run against the old
    # straight-to-design behaviour.
    skip_reasoner: bool = False
    use_docker: bool | None = None
    run_id: str | None = Field(default=None, max_length=64)


class RunSummary(BaseModel):
    run_id: str
    requirement: str
    status: str
    created_at: str
    updated_at: str
    tests_passed: int = 0
    tests_failed: int = 0
    attempts_dev: int = 0
    attempts_test: int = 0
    review_rounds: int = 0
    wall_time: float | None = None
    # Which module the generated app was imported from, e.g. "app". Discovered
    # from the code rather than assumed, so it is worth surfacing.
    entrypoint: str | None = None
    files: list[str] = []
    error: str | None = None
    # Set when this run continues a previous one (resume): the old run id.
    # The old run is kept as history; nothing is deleted by resuming.
    resumed_from: str | None = None


class RunDetail(RunSummary):
    events: list[dict[str, Any]] = Field(default_factory=list)
    artifacts: list[str] = Field(default_factory=list)
    stories: dict[str, Any] | None = None
    # Plan from the Reasoner node (approach/decisions/risks/edge cases/open
    # questions). Null when the node was ablated or has not run; advisory only.
    plan: dict[str, Any] | None = None
    spec: str | None = None
    code: dict[str, str] = Field(default_factory=dict)
    tests: dict[str, str] = Field(default_factory=dict)
    review: dict[str, Any] | None = None
    report: dict[str, Any] | None = None
    # Domain coverage verdict: which requirement terms the code reflects.
    coverage: dict[str, Any] | None = None
    memory: list[str] = Field(default_factory=list)


class LlmStatus(BaseModel):
    ollama_reachable: bool
    ollama_models: list[str] = Field(default_factory=list)
    skip_ollama: bool
    hosted_keys_present: list[str] = Field(default_factory=list)
    # Why the hosted tiers declined on the most recent call, e.g. an exhausted
    # free-tier quota. Without this a quota outage is indistinguishable from a
    # healthy run, because every call quietly falls back to template mode.
    last_failures: list[str] = Field(default_factory=list)
    last_model: str = ""


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"]
    version: str
    environment: str
    llm: LlmStatus
    active_runs: int
    max_concurrent_runs: int
