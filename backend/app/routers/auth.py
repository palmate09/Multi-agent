"""Login/logout/session endpoints."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field

from app import auth
from app.auth import (
    clear_session_cookies,
    current_user,
    issue_session,
    login_limiter,
    require_auth,
    set_session_cookies,
)

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
        csrf_token=request.cookies.get(auth._CSRF_COOKIE) if user else None,
    )


@router.post("/login", response_model=SessionInfo)
def login(payload: LoginRequest, request: Request, response: Response) -> SessionInfo:
    cfg = auth.get_auth_config()
    if not cfg.enabled:
        # Nothing to log into. Say so instead of pretending to authenticate.
        return SessionInfo(authenticated=True, username="anonymous", auth_enabled=False)

    identity = auth.client_identity(request)
    key = f"login:{identity}"
    wait = login_limiter.check(key)
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
        login_limiter.record_failure(key)
        log.warning("failed login for %r from %s", payload.username, identity)
        # Deliberately vague, and identical whether the user or the password
        # was wrong.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid username or password"
        )

    login_limiter.record_success(key)
    token, csrf = issue_session(cfg.username)
    set_session_cookies(response, token, csrf)
    return SessionInfo(
        authenticated=True, username=cfg.username, auth_enabled=True, csrf_token=csrf
    )


@router.post("/logout")
def logout(response: Response) -> Response:
    clear_session_cookies(response)
    return response


@router.get("/limits")
def limits(request: Request, _: str = Depends(require_auth)) -> dict:
    """Diagnostic: current login budget for this client."""
    identity = auth.client_identity(request)
    return {
        "identity": identity,
        "max_attempts": login_limiter.max_attempts,
        "window_seconds": login_limiter.window,
        "blocked_for": login_limiter.check(f"login:{identity}"),
    }