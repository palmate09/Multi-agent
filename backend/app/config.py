"""Application settings, loaded from environment with sane production defaults."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # `env_file` is overridable so the test suite can point it at nothing.
    # pydantic-settings reads this path directly, bypassing `os.environ`, so a
    # `monkeypatch.delenv` cannot stop it -- which means a developer with
    # PREVIEW_ALLOW_START=1 in their .env silently changes what a test asserting
    # the *default* measures. The suite failed here for exactly that reason.
    model_config = SettingsConfigDict(
        env_file=os.getenv("MAT_ENV_FILE", ".env"), env_file_encoding="utf-8", extra="ignore"
    )

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

    # ---- playground ----
    # A preview is a real subprocess running generated code on this host. It
    # is NOT isolation: the caps below bound how long and how many, not what the
    # code can reach. Only the process's own credentials are stripped.
    preview_ttl_seconds: int = 1800
    preview_max_concurrent: int = 3
    # How long a preview may take to answer its first request before it is
    # declared crashed. Generated apps do a create_all at import, so this is
    # generous on purpose.
    preview_boot_timeout_seconds: int = 30
    # How often the TTL reaper scans for idle or dead previews.
    preview_reap_interval: int = 15
    # Whether the API may start a preview at all. On by default outside
    # production so a developer can just click it; production has to opt in,
    # because there it means executing generated code on the server.
    preview_allow_start: bool | None = None

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

    @property
    def previews_enabled(self) -> bool:
        """Whether starting a preview is permitted.

        Default on in development, off in production: there, ``/preview/*``
        means running generated code on the server, which has to be a decision
        somebody made rather than a side effect of clicking a button.
        """
        if self.preview_allow_start is None:
            return not self.is_production
        return self.preview_allow_start


@lru_cache
def get_settings() -> Settings:
    return Settings()
