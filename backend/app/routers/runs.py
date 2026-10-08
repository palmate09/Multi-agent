"""Run lifecycle endpoints: create, list, inspect, stream, delete, artifacts."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import Response, StreamingResponse

from app.config import Settings, get_settings
from app.dependencies import get_store
from app.schemas import RunCreate, RunDetail, RunSummary
from app.services.runs import Run, RunStore

log = logging.getLogger("app.api.runs")
router = APIRouter(prefix="/api/runs", tags=["runs"])


def _get_run(run_id: str, store: RunStore) -> Run:
    run = store.get(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"run '{run_id}' not found")
    return run


@router.post("", response_model=RunSummary, status_code=status.HTTP_202_ACCEPTED)
def create_run(
    payload: RunCreate,
    store: RunStore = Depends(get_store),
    settings: Settings = Depends(get_settings),
) -> RunSummary:
    if len(payload.requirement.strip()) < 10:
        raise HTTPException(
            status_code=422,
            detail="requirement must contain at least 10 characters of real description",
        )
    if store.active_count() >= settings.max_concurrent_runs:
        raise HTTPException(
            status_code=429,
            detail=f"server busy: {store.active_count()}/"
            f"{settings.max_concurrent_runs} runs in flight",
        )
    run = store.create(
        payload.requirement.strip(),
        run_id=payload.run_id,
        skip_tester=payload.skip_tester,
        skip_reviewer=payload.skip_reviewer,
        use_docker=payload.use_docker,
    )
    store.submit(run)
    return RunSummary(**run.summary)


@router.get("", response_model=list[RunSummary])
def list_runs(store: RunStore = Depends(get_store)) -> list[RunSummary]:
    return [RunSummary(**r.summary) for r in store.list()]


@router.get("/{run_id}", response_model=RunDetail)
def get_run(run_id: str, store: RunStore = Depends(get_store)) -> RunDetail:
    return RunDetail(**_get_run(run_id, store).detail())


@router.get("/{run_id}/events")
async def stream_events(
    request: Request, run_id: str, store: RunStore = Depends(get_store)
) -> StreamingResponse:
    """Server-sent events for one run.

    Replays buffered history first, so attaching mid-run still shows context.
    """
    run = _get_run(run_id, store)

    async def generator() -> AsyncIterator[bytes]:
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[dict | None] = asyncio.Queue()

        def forward() -> None:
            try:
                for event in run.stream():
                    loop.call_soon_threadsafe(queue.put_nowait, event)
            finally:
                loop.call_soon_threadsafe(queue.put_nowait, None)

        task = loop.create_task(asyncio.to_thread(forward))
        try:
            yield b": stream open\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15.0)
                except TimeoutError:
                    yield b": keepalive\n\n"
                    continue
                if event is None:
                    break
                yield f"data: {json.dumps(event)}\n\n".encode()
            # Named event so the browser can close the stream explicitly.
            # Without this, EventSource sees a closed connection and retries
            # forever against a finished run.
            final = {"node": "pipeline", "phase": "end", "status": run.status, "error": run.error}
            yield f"event: end\ndata: {json.dumps(final)}\n\n".encode()
        finally:
            task.cancel()

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/{run_id}/artifacts")
def list_artifacts(run_id: str, store: RunStore = Depends(get_store)) -> dict:
    run = _get_run(run_id, store)
    return {"run_id": run_id, "files": run.list_artifacts()}


@router.get("/{run_id}/artifacts/{path:path}")
def get_artifact(run_id: str, path: str, store: RunStore = Depends(get_store)) -> Response:
    run = _get_run(run_id, store)
    found = run.read_artifact(path)
    if found is None:
        raise HTTPException(status_code=404, detail="artifact not found")
    content, media = found
    return Response(content=content, media_type=media)


@router.delete("/{run_id}", status_code=status.HTTP_204_NO_CONTENT, response_class=Response)
def delete_run(run_id: str, store: RunStore = Depends(get_store)):
    if not store.delete(run_id):
        raise HTTPException(status_code=404, detail=f"run '{run_id}' not found")
    return Response(status_code=status.HTTP_204_NO_CONTENT)
