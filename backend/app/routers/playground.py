"""Playground gateway: control endpoints plus the ``/preview/{run_id}/`` proxy.

The proxy exists because the generated UI is mounted under a sub-path, so it
cannot know the port its backend happens to be on. Every request resolves
``run_id -> port`` server-side and is forwarded to that port on loopback.

Three endpoints describe a session (GET /api/runs/{id}/playground,
POST .../start, POST .../stop) and one forwards traffic (ANY /preview/{id}/{path}).
``/preview`` deliberately has no run-existence dependency of its own beyond the
session map, so an unknown run id is a 404, not a proxy to nothing.
"""

from __future__ import annotations

import logging
from pathlib import Path

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import FileResponse

from app.config import Settings, get_settings
from app.dependencies import get_store
from app.services.playground import PlaygroundService, get_playground
from app.services.runs import RunStore

log = logging.getLogger("app.api.playground")
router = APIRouter(tags=["playground"])

# Headers that describe a single hop and must not be copied through. Passing
# Connection/Transfer-Encoding on to httpx makes it frame the body wrongly;
# Upgrade would make us attempt a WebSocket we do not support.
_HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)


def _service() -> PlaygroundService:
    return get_playground()


@router.get("/api/runs/{run_id}/playground")
def playground_status(
    run_id: str, store: RunStore = Depends(get_store), svc: PlaygroundService = Depends(_service)
) -> dict:
    """Current session state, plus why it failed if it did."""
    _require_run(run_id, store)
    return svc.status(run_id)


@router.post("/api/runs/{run_id}/playground/start", status_code=status.HTTP_202_ACCEPTED)
def playground_start(
    run_id: str,
    store: RunStore = Depends(get_store),
    svc: PlaygroundService = Depends(_service),
    settings: Settings = Depends(get_settings),
) -> dict:
    """Boot the run's generated app (or return the live one unchanged).

    Idempotent: starting an already-running preview returns its existing
    session rather than occupying another port.
    """
    _require_run(run_id, store)
    if not settings.previews_enabled:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="previews are disabled on this server (PREVIEW_ALLOW_START)",
        )
    try:
        return svc.start(run_id).status()
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/api/runs/{run_id}/playground/stop")
def playground_stop(
    run_id: str, store: RunStore = Depends(get_store), svc: PlaygroundService = Depends(_service)
) -> dict:
    """Stop the session and discard its working directory."""
    _require_run(run_id, store)
    stopped = svc.stop(run_id)
    return {"run_id": run_id, "stopped": stopped, "state": "stopped"}


@router.api_route(
    "/preview/{run_id}/{path:path}",
    # Both routes are excluded from the OpenAPI schema: /preview is a
    # browser-facing proxy, not an API to document, and stacking the two
    # decorators on one function otherwise gives them the same generated
    # operation id, which collides in /api/docs and warns on every startup.
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
    include_in_schema=False,
)
@router.api_route(
    "/preview/{run_id}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
    include_in_schema=False,
)
async def preview_proxy(
    run_id: str,
    request: Request,
    path: str = "",
    svc: PlaygroundService = Depends(_service),
) -> Response:
    """Serve the run's UI, and proxy everything else to its preview process.

    The generated UI files are served **here**, straight from the run
    directory, rather than proxied to the app. The Developer that wrote
    ``main.py`` ran before the UI existed and never saw it, so it cannot mount
    it: a real Gemini run produced an API with no ``StaticFiles`` mount at all,
    and proxying ``/preview/<rid>/`` to it returned 404 with the page sitting
    unused on disk. Serving the files in the gateway makes the page work
    without depending on generated code cooperating.

    The UI's own API calls stay relative (``fetch('recipes')``), so from
    ``/preview/<rid>/`` they resolve to ``/preview/<rid>/recipes``, which is
    proxied to the app. One mount point, no port, no cooperation required.

    Streaming, not buffered, so SSE from a generated app works and large
    responses do not pin memory.
    """
    # A WebSocket upgrade cannot be proxied by httpx in streaming mode; say so
    # explicitly rather than hanging or returning a broken handshake.
    if request.headers.get("upgrade", "").strip().lower() == "websocket":
        return Response(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            content="WebSocket previews are not supported",
        )

    asset = _ui_asset(svc, run_id, path)
    if asset is not None:
        return asset

    session = svc.resolve(run_id)
    if session is None:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"no running preview for run '{run_id}'",
        )

    # `session.base_url`, not a hardcoded 127.0.0.1: the gateway's contract is
    # unchanged -- it still proxies to whatever address the session holds -- but
    # that address is the container's own in container mode and loopback in
    # subprocess mode. The client still only ever sees /preview/<rid>/.
    target = f"{session.base_url}/{path}"
    forwarded = {
        k: v
        for k, v in request.headers.items()
        if k.lower() not in _HOP_BY_HOP and k.lower() not in {"host", "content-length"}
    }
    forwarded["host"] = f"{session.host}:{session.port}"

    try:
        # A client per request: timeout and connection handling stay local to
        # the call, and no socket outlives the TTL that reaped its session.
        async with (
            httpx.AsyncClient(timeout=30.0, follow_redirects=False) as client,
            client.stream(
                request.method,
                target,
                params=request.query_params,
                content=await request.body(),
                headers=forwarded,
            ) as resp,
        ):
            body = b"".join([chunk async for chunk in resp.aiter_bytes()])
            # aiter_bytes already decoded the body, so the upstream's
            # encoding header and length would both be lies here.
            out_headers = {
                k: v
                for k, v in resp.headers.items()
                if k.lower() not in _HOP_BY_HOP
                and k.lower() not in {"content-encoding", "content-length"}
            }
            status_code = resp.status_code
            media_type = resp.headers.get("content-type")
    except httpx.HTTPError as exc:
        log.warning("preview %s proxy failed: %s", run_id, exc)
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc

    # The gateway's own SAMEORIGIN header (set by nginx/Caddy) is what permits
    # the iframe; the app's framing headers are left as the app wrote them.
    return Response(
        content=body,
        status_code=status_code,
        headers=out_headers,
        media_type=media_type,
    )


def _require_run(run_id: str, store: RunStore) -> None:
    if store.get(run_id) is None:
        raise HTTPException(status_code=404, detail=f"run '{run_id}' not found")


#: Only text assets. The UI is hand-written HTML/JS/CSS by construction, so
#: there is no reason to serve anything executable or binary from a run's ui/.
_UI_MEDIA = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json",
    ".svg": "image/svg+xml",
    ".txt": "text/plain; charset=utf-8",
}


def _ui_asset(svc: PlaygroundService, run_id: str, path: str) -> Response | None:
    """Serve ``<run>/ui/<path>`` if it names a text asset. None means "proxy it".

    ``""`` and ``"/"`` both mean index.html: that is what an iframe pointed at
    ``/preview/<rid>/`` is asking for.
    """
    relative = (path or "").strip("/")
    if relative in {"", "index.html"}:
        relative = "index.html"
    if not relative:
        return None

    run_dir = svc.runs_dir / run_id / "ui"
    if not run_dir.is_dir():
        return None
    media = _UI_MEDIA.get(Path(relative).suffix.lower())
    if media is None:
        return None

    # Resolve first, then re-check containment: `ui/../../secrets.env` must not
    # resolve to something outside the run's own ui/ directory.
    candidate = (run_dir / relative).resolve()
    try:
        candidate.relative_to(run_dir.resolve())
    except ValueError:
        return None
    if not candidate.is_file():
        return None
    return FileResponse(candidate, media_type=media)
