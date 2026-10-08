"""Application settings, loaded from environment with sane production defaults."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "Multi-Agent Software Team"
    version: str = "1.0.0"
    environment: str = "development"
    debug: bool = False

    # Where run artifacts are written. In compose this is a mounted volume.
    runs_dir: Path = Field(default=Path("data/runs"))

    # Comma-separated origins. Empty means same-origin only (no CORS headers).
    cors_origins: str = ""

    # Pipeline execution guards.
    max_concurrent_runs: int = 2
    run_timeout_seconds: int = 1800
    max_requirement_chars: int = 8000

    # Sandbox execution. Docker gives network isolation; local does not.
    sandbox_use_docker: bool = False
    sandbox_timeout_seconds: int = 60

    # Hosted UI location, used for CORS in split dev setups.
    frontend_origin: str = "http://localhost:5173"

    @field_validator("runs_dir", mode="before")
    @classmethod
    def _abs(cls, v: str | Path) -> Path:
        return Path(v)

    @property
    def cors_origin_list(self) -> list[str]:
        raw = [o.strip() for o in self.cors_origins.split(",") if o.strip()]
        return raw or ([self.frontend_origin] if self.debug else [])

    @property
    def is_production(self) -> bool:
        return self.environment.lower() in {"production", "prod"}


@lru_cache
def get_settings() -> Settings:
    return Settings()
