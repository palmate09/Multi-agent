"""Authentication: password hashing, signed session cookies, rate limiting.

Design notes:

* Passwords use stdlib ``hashlib.scrypt`` with a per-password random salt.
  No native dependency (bcrypt/argon2) is needed, and scrypt is memory-hard so
  a stolen hash is expensive to attack offline.
* Sessions are stateless signed cookies (``itsdangerous``) rather than a server
  side store, so the API stays a single process. Logout therefore cannot
  revoke an already-issued cookie before it expires; rotate ``AUTH_SECRET`` to
  invalidate every session at once.
* An optional API key allows headless access (``X-API-Key``) for scripting.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import secrets
import threading
import time
from collections import defaultdict, deque

from fastapi import HTTPException, Request, status
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

log = logging.getLogger("app.auth")

SESSION_COOKIE = "mat_session"
SESSION_MAX_AGE = 60 * 60 * 12  # 12 hours
CSRF_HEADER = "X-CSRF-Token"
_CSRF_COOKIE = "mat_csrf"

_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_DKLEN = 32


# --------------------------------------------------------------------------
# password hashing
# --------------------------------------------------------------------------
def hash_password(password: str) -> str:
    """Return ``scrypt$n$r$p$<salt>$<digest>`` with a fresh random salt."""
    if len(password) < 8:
        raise ValueError("password must be at least 8 characters")
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode(),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=_SCRYPT_DKLEN,
    )
    return "scrypt${}${}${}${}${}".format(
        _SCRYPT_N,
        _SCRYPT_R,
        _SCRYPT_P,
        base64.b64encode(salt).decode(),
        base64.b64encode(digest).decode(),
    )


def verify_password(password: str, stored: str) -> bool:
    """Constant-time verification. Never raises on malformed input."""
    try:
        scheme, n, r, p, salt_b64, digest_b64 = stored.split("$")
        if scheme != "scrypt":
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(digest_b64)
        candidate = hashlib.scrypt(
            password.encode(),
            salt=salt,
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(expected),
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(candidate, expected)


def new_api_key() -> str:
    return "mat_" + secrets.token_urlsafe(32)


# --------------------------------------------------------------------------
# sessions
# --------------------------------------------------------------------------
class AuthConfig:
    """Resolved auth configuration for one app instance."""

    def __init__(self) -> None:
        self.enabled = False
        self.username = os.getenv("AUTH_USERNAME", "admin")
        self.secret = os.getenv("AUTH_SECRET", "")
        self.password_hash = os.getenv("AUTH_PASSWORD_HASH", "")
        self.api_key = os.getenv("AUTH_API_KEY", "")
        self.cookie_secure = os.getenv("AUTH_COOKIE_SECURE", "0") == "1"
        self.max_age = int(os.getenv("AUTH_SESSION_HOURS", "12")) * 3600
        self.require_csrf = os.getenv("AUTH_CSRF", "1") == "1"

        if not self.password_hash:
            # Dev convenience: allow a plaintext password and hash it on load.
            plain = os.getenv("AUTH_PASSWORD", "")
            if plain:
                self.password_hash = hash_password(plain)

        if self.password_hash or self.api_key:
            if not self.secret:
                # Derive a per-process secret so a missing AUTH_SECRET fails
                # closed (sessions die on restart) instead of running unsigned.
                log.warning(
                    "AUTH_SECRET is not set; deriving an ephemeral one. "
                    "Sessions will not survive a restart. Set AUTH_SECRET in production."
                )
                self.secret = secrets.token_urlsafe(48)
            self.enabled = True

    @property
    def username_ok(self) -> bool:
        return bool(self.username)


_config: AuthConfig | None = None


def get_auth_config() -> AuthConfig:
    global _config
    if _config is None:
        _config = AuthConfig()
    return _config


def reset_auth_config() -> None:
    """Test hook: force re-reading the environment."""
    global _config
    _config = None


def _serializer() -> URLSafeTimedSerializer:
    cfg = get_auth_config()
    return URLSafeTimedSerializer(cfg.secret, salt="mat-session-v1")


def issue_session(username: str) -> tuple[str, str]:
    """Return ``(session_token, csrf_token)``."""
    cfg = get_auth_config()
    token = _serializer().dumps({"u": username, "n": secrets.token_urlsafe(8)})
    csrf = secrets.token_urlsafe(32)
    return token, csrf


def read_session(token: str) -> dict | None:
    cfg = get_auth_config()
    try:
        return _serializer().loads(token, max_age=cfg.max_age)
    except SignatureExpired:
        log.info("session expired")
        return None
    except BadSignature:
        return None
    except Exception:  # noqa: BLE001 - never leak internals to the client
        log.exception("session decode failed")
        return None


# --------------------------------------------------------------------------
# login rate limiting
# --------------------------------------------------------------------------
class RateLimiter:
    """Sliding-window limiter keyed by (scope, identity)."""

    def __init__(self, max_attempts: int = 5, window: int = 300, block: int = 900):
        self.max_attempts = max_attempts
        self.window = window
        self.block = block
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._blocked: dict[str, float] = {}
        self._lock = threading.Lock()

    def check(self, key: str) -> int:
        """Return seconds remaining on a block, or 0 if allowed."""
        now = time.time()
        with self._lock:
            until = self._blocked.get(key, 0)
            if until > now:
                return int(until - now)
            if until:
                self._blocked.pop(key, None)
                self._hits.pop(key, None)
            hits = self._hits[key]
            while hits and now - hits[0] > self.window:
                hits.popleft()
            if len(hits) >= self.max_attempts:
                self._blocked[key] = now + self.block
                self._hits.pop(key, None)
                return self.block
            return 0

    def record_failure(self, key: str) -> None:
        with self._lock:
            self._hits[key].append(time.time())

    def record_success(self, key: str) -> None:
        with self._lock:
            self._hits.pop(key, None)
            self._blocked.pop(key, None)

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()
            self._blocked.clear()


login_limiter = RateLimiter()


# --------------------------------------------------------------------------
# request authentication
# --------------------------------------------------------------------------
def _api_key_ok(candidate: str | None) -> bool:
    cfg = get_auth_config()
    if not candidate or not cfg.api_key:
        return False
    return hmac.compare_digest(candidate.strip(), cfg.api_key)


def client_identity(request: Request) -> str:
    """Best-effort client IP, honouring one hop of reverse proxy."""
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def current_user(request: Request) -> str | None:
    """Return the authenticated username, or None."""
    cfg = get_auth_config()
    if not cfg.enabled:
        return "anonymous"
    if _api_key_ok(request.headers.get("x-api-key")):
        return "api-key"
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    data = read_session(token)
    if not data:
        return None
    return data.get("u") or None


def require_auth(request: Request) -> str:
    """Auth-only guard for handlers that need the identity.

    CSRF is enforced centrally in the app middleware, not here, so it applies
    uniformly to every mutating endpoint.
    """
    cfg = get_auth_config()
    if not cfg.enabled:
        return "anonymous"
    user = current_user(request)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="authentication required",
            headers={"WWW-Authenticate": "Cookie"},
        )
    return user


def set_session_cookies(response, token: str, csrf: str) -> None:
    cfg = get_auth_config()
    secure = cfg.cookie_secure
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=cfg.max_age,
        httponly=True,   # not readable from JS: reduces XSS impact
        secure=secure,   # requires HTTPS
        samesite="strict",  # not sent on cross-site requests
        path="/",
    )
    # Readable by JS on purpose: the SPA echoes it back in the CSRF header.
    response.set_cookie(
        _CSRF_COOKIE,
        csrf,
        max_age=cfg.max_age,
        httponly=False,
        secure=secure,
        samesite="strict",
        path="/",
    )


def clear_session_cookies(response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/")
    response.delete_cookie(_CSRF_COOKIE, path="/")