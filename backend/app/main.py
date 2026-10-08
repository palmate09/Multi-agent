"""FastAPI application factory for the multi-agent team backend."""

from __future__ import annotations

import hmac
import logging
import logging.config
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app import auth
from app.config import get_settings
from app.routers import auth as auth_router
from app.routers import evals, health, runs
from app.services.runs import RunStore

LOG_FORMAT = "%(asctime)s %(levelname)-8s %(name)s %(message)s"

# Paths that stay reachable without a session. /health must be public for the
# container healthcheck and any external uptime probe; /api/auth/* is how you
# obtain a session in the first place.
PUBLIC_PATHS = frozenset(
    {
        "/health",
        "/api/auth/login",
        "/api/auth/logout",
        "/api/auth/me",
    }
)


def configure_logging(level: str) -> None:
    logging.config.dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": {"default": {"format": LOG_FORMAT}},
            "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "default"}},
            "root": {"handlers": ["console"], "level": level},
        }
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    configure_logging("DEBUG" if settings.debug else "INFO")
    auth.reset_auth_config()
    cfg = auth.get_auth_config()
    store = RunStore(settings)
    restored = store.rehydrate()
    app.state.settings = settings
    app.state.store = store
    logging.getLogger("app").info(
        "started env=%s runs_dir=%s restored=%d auth=%s user=%s",
        settings.environment,
        settings.runs_dir,
        restored,
        "on" if cfg.enabled else "OFF",
        cfg.username if cfg.enabled else "-",
    )
    if not cfg.enabled:
        logging.getLogger("app").warning(
            "AUTH IS DISABLED: set AUTH_PASSWORD_HASH and AUTH_SECRET to require login"
        )
    try:
        yield
    finally:
        store.shutdown()
        logging.getLogger("app").info("shutdown complete")


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title=f"{settings.app_name} API",
        version=settings.version,
        lifespan=lifespan,
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
        redoc_url=None,
    )

    origins = settings.cors_origin_list
    if origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=origins,
            # Credentials must be allowed or the session cookie is dropped.
            allow_credentials=True,
            allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
            allow_headers=["Content-Type", "Accept", auth.CSRF_HEADER, "X-API-Key"],
            expose_headers=["X-API-Key-Required"],
            max_age=600,
        )

    @app.middleware("http")
    async def auth_gate(request: Request, call_next):
        """Enforce authentication and CSRF before a handler runs.

        Middleware rather than per-router dependencies so that a route added
        later is protected by default instead of silently shipping open.
        """
        cfg = auth.get_auth_config()
        if not cfg.enabled:
            return await call_next(request)

        path = request.url.path
        if path in PUBLIC_PATHS:
            return await call_next(request)

        user = auth.current_user(request)
        if user is None:
            return JSONResponse(
                status_code=status.HTTP_401_UNAUTHORIZED,
                content={"detail": "authentication required"},
                headers={"WWW-Authenticate": "Cookie"},
            )

        # Double-submit CSRF check. Skipped for API-key callers, which are not
        # browsers and so cannot be ridden by a cross-site form.
        if (
            cfg.require_csrf
            and user != "api-key"
            and request.method in {"POST", "PUT", "PATCH", "DELETE"}
        ):
            header = request.headers.get(auth.CSRF_HEADER)
            cookie = request.cookies.get(auth.CSRF_COOKIE)
            if not header or not cookie or not hmac.compare_digest(header, cookie):
                return JSONResponse(
                    status_code=status.HTTP_403_FORBIDDEN,
                    content={"detail": "missing or invalid CSRF token"},
                )

        return await call_next(request)

    app.include_router(health.router)
    app.include_router(auth_router.router)
    app.include_router(runs.router)
    app.include_router(evals.router)

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception) -> JSONResponse:
        logging.getLogger("app").exception(
            "unhandled error on %s %s", request.method, request.url.path
        )
        if settings.debug:
            return JSONResponse(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                content={"detail": f"{type(exc).__name__}: {exc}"},
            )
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"detail": "internal server error"},
        )

    @app.get("/", include_in_schema=False)
    def root() -> dict:
        return {
            "name": settings.app_name,
            "version": settings.version,
            "docs": "/api/docs",
            "frontend": "served by nginx in the compose stack",
        }

    return app


app = create_app()
