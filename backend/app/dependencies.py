"""Shared FastAPI dependencies."""

from __future__ import annotations

from fastapi import Request

from app.services.runs import RunStore


def get_store(request: Request) -> RunStore:
    """Return the app-scoped RunStore.

    Deliberately request-scoped rather than a module global: the store owns a
    thread pool and a runs directory, both of which are configured per app
    instance. A global would leak state between app instances (notably in tests,
    where each app gets its own temporary RUNS_DIR).
    """
    return request.app.state.store
