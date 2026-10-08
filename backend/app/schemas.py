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


class RunDetail(RunSummary):
    events: list[dict[str, Any]] = Field(default_factory=list)
    artifacts: list[str] = Field(default_factory=list)
    stories: dict[str, Any] | None = None
    spec: str | None = None
    code: dict[str, str] = Field(default_factory=dict)
    tests: dict[str, str] = Field(default_factory=dict)
    review: dict[str, Any] | None = None
    report: dict[str, Any] | None = None
    memory: list[str] = Field(default_factory=list)
    error: str | None = None


class LlmStatus(BaseModel):
    ollama_reachable: bool
    ollama_models: list[str] = Field(default_factory=list)
    skip_ollama: bool
    hosted_keys_present: list[str] = Field(default_factory=list)


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"]
    version: str
    environment: str
    llm: LlmStatus
    active_runs: int
    max_concurrent_runs: int
