"""Login/logout/session endpoints."""

from __future__ import annotations

import logging
import secrets

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app import auth
from app.auth import clear_session_cookies, current_user, require_auth, set_session_cookies
from app.db.sessions import get_attempts, get_sessions

log = logging.getLogger("app.api.auth")
router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=1024)


class SessionInfo(BaseModel):
    authenticated: bool
    username: str | None = None
    auth_enabled: bool
    csrf_token: str | None = None


@router.get("/me", response_model=SessionInfo)
def me(request: Request) -> SessionInfo:
    cfg = auth.get_auth_config()
    user = current_user(request)
    return SessionInfo(
        authenticated=user is not None,
        username=user,
        auth_enabled=cfg.enabled,
        # The SPA needs the CSRF value to make writes.
        csrf_token=request.cookies.get(auth.CSRF_COOKIE) if user else None,
    )


@router.post("/login", response_model=SessionInfo)
def login(payload: LoginRequest, request: Request, response: Response) -> SessionInfo:
    cfg = auth.get_auth_config()
    if not cfg.enabled:
        # Nothing to log into. Say so instead of pretending to authenticate.
        return SessionInfo(authenticated=True, username="anonymous", auth_enabled=False)

    identity = auth.client_identity(request)
    limiter = get_attempts()
    wait = limiter.check(identity, payload.username)
    if wait:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"too many failed attempts; retry in {wait}s",
            headers={"Retry-After": str(wait)},
        )

    ok = (
        payload.username == cfg.username
        and bool(cfg.password_hash)
        and auth.verify_password(payload.password, cfg.password_hash)
    )
    if not ok:
        limiter.record_failure(identity, payload.username)
        log.warning("failed login for %r from %s", payload.username, identity)
        # Deliberately vague, and identical whether the user or the password
        # was wrong.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid username or password"
        )

    limiter.record_success(identity, payload.username)
    # Stored, not signed: the cookie is a reference to a row, so logout can
    # revoke it and a stolen cookie dies the moment the row is deleted.
    issued = get_sessions().issue(
        cfg.username,
        csrf=secrets.token_urlsafe(32),
        ip=identity,
        user_agent=request.headers.get("user-agent", ""),
    )
    set_session_cookies(response, issued.token, issued.csrf)
    return SessionInfo(
        authenticated=True, username=cfg.username, auth_enabled=True, csrf_token=issued.csrf
    )


@router.post("/logout")
def logout(request: Request) -> Response:
    # Deleting the row is the logout. Without this the cookie stays valid for
    # its whole lifetime and "log out" only cleared the browser.
    token = request.cookies.get(auth.SESSION_COOKIE)
    if token:
        get_sessions().revoke(token)
    # Build a real response rather than returning FastAPI's injected bare
    # Response, whose status_code is still None at that point.
    response = JSONResponse({"authenticated": False})
    clear_session_cookies(response)
    return response


@router.get("/limits")
def limits(request: Request, _: str = Depends(require_auth)) -> dict:
    """Diagnostic: current login budget for this client."""
    identity = auth.client_identity(request)
    limiter = get_attempts()
    return {
        "identity": identity,
        "max_attempts": limiter.max_attempts,
        "window_seconds": limiter.window,
        "blocked_for": limiter.check(identity, ""),
        # "durable" is the honest signal about whether this survives a restart.
        # It is the whole reason the limiter moved into a table.
        "durable": limiter.persistent,
    }


@router.get("/sessions")
def list_sessions(request: Request, _: str = Depends(require_auth)) -> dict:
    """The current user's live sessions.

    Only possible because sessions are rows. A signed cookie has no server-side
    identity to list against, which is why "which devices am I signed in on"
    was not answerable before.
    """
    token = request.cookies.get(auth.SESSION_COOKIE)
    resolved = get_sessions().resolve(token) if token else None
    return {
        "username": resolved[0] if resolved else None,
        "user_id": resolved[1] if resolved else None,
        "current": bool(resolved),
        "persistent": get_sessions().persistent,
    }
