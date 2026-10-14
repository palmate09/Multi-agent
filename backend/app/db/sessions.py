"""Server-side sessions and durable login rate limiting.

Both replace something that was per-process and therefore wrong in a way that
only showed up after a restart or a second replica:

*sessions*
    were stateless signed cookies, so a copied cookie stayed valid for its full
    12 hours and **logout could not revoke it** -- the README said so. They are
    now rows. Only a SHA-256 of the token is stored, so a database dump is not a
    set of live sessions.

*login_attempts*
    were an in-memory ``defaultdict`` of timestamps, which resets on restart
    and does nothing across replicas. They are now rows, which makes the limit
    a property of the deployment rather than of one process.

Both fall back to the previous in-memory behaviour when no database is
configured, so the offline test suite and a laptop with no Postgres keep
working exactly as before. The fallback is honest about what it is: a
single-process development convenience, not the deployed behaviour.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass

from app.auth import get_auth_config
from app.db.engine import Database, get_db, token_hash

log = logging.getLogger("app.sessions")

#: How long a login block lasts once the attempt threshold is crossed. Same
#: default as the in-memory limiter, so behaviour does not shift when the
#: database is switched on.
DEFAULT_BLOCK_SECONDS = 900
DEFAULT_WINDOW_SECONDS = 300
DEFAULT_MAX_ATTEMPTS = 5


@dataclass
class IssuedSession:
    """What a successful login hands back."""

    token: str
    csrf: str
    user_id: int | None


# --------------------------------------------------------------------------
# sessions
# --------------------------------------------------------------------------


class SessionStore:
    """Revocable sessions, backed by the ``sessions`` table.

    The in-memory map is the fallback and is keyed by the same hash, so
    switching the database on does not change the token format or the cookie
    name -- which is what lets stale cookies fail closed rather than
    half-work during a migration.
    """

    def __init__(self, db: Database | None = None) -> None:
        self._db = db or get_db()
        self._lock = threading.Lock()
        # token_hash -> (username, csrf_hash, expires_at, username_id)
        self._memory: dict[str, tuple[str, str, float, int | None]] = {}

    @property
    def persistent(self) -> bool:
        return self._db.configured

    # -- write ----------------------------------------------------------
    def issue(
        self, username: str, csrf: str, ip: str = "", user_agent: str = ""
    ) -> IssuedSession:
        from app.auth import new_raw_session_token

        cfg = get_auth_config()
        token = new_raw_session_token()
        user_id = self._ensure_user(username)
        expires_at = time.time() + cfg.max_age
        if self.persistent:
            try:
                with self._db.connect() as conn:
                    conn.execute(
                        """
                        INSERT INTO sessions
                            (user_id, token_hash, csrf_hash, expires_at, ip, user_agent)
                        VALUES (:uid, :tok, :csrf, to_timestamp(:exp), :ip, :ua)
                        """,
                        {
                            "uid": user_id,
                            "tok": token_hash(token),
                            "csrf": token_hash(csrf),
                            "exp": expires_at,
                            "ip": ip,
                            "ua": user_agent[:400],
                        },
                    )
            except Exception as exc:
                # A session that cannot be stored must not be handed out: it
                # would be a credential nothing can revoke.
                log.error("could not persist a session, refusing to issue one: %s", exc)
                raise
        else:
            with self._lock:
                self._memory[token_hash(token)] = (
                    username,
                    token_hash(csrf),
                    expires_at,
                    user_id,
                )
        return IssuedSession(token=token, csrf=csrf, user_id=user_id)

    def revoke(self, token: str) -> bool:
        """Delete one session. This is what makes logout real."""
        if self.persistent:
            try:
                with self._db.connect() as conn:
                    result = conn.execute(
                        "DELETE FROM sessions WHERE token_hash = :t",
                        {"t": token_hash(token)},
                    )
                    return bool(getattr(result, "rowcount", 0))
            except Exception as exc:
                log.error("could not revoke a session: %s", exc)
                return False
        with self._lock:
            return self._memory.pop(token_hash(token), None) is not None

    def revoke_all(self, user_id: int) -> int:
        """Every session for a user -- "sign out everywhere"."""
        if self.persistent:
            try:
                with self._db.connect() as conn:
                    conn.execute("DELETE FROM sessions WHERE user_id = :u", {"u": user_id})
                    return 1
            except Exception as exc:
                log.error("could not revoke sessions for user %s: %s", user_id, exc)
                return 0
        with self._lock:
            gone = [k for k, v in self._memory.items() if v[3] == user_id]
            for k in gone:
                del self._memory[k]
            return len(gone)

    # -- read -----------------------------------------------------------
    def resolve(self, token: str) -> tuple[str, int | None] | None:
        """``(username, user_id)`` for a live session, or None.

        Expiry, a deleted row and an unknown token are all the same answer, and
        all of them mean the caller is not authenticated. That is the point of
        storing sessions server-side: the cookie is now a reference, and
        deleting the reference is enough.
        """
        digest = token_hash(token)
        if self.persistent:
            try:
                with self._db.connect() as conn:
                    row = conn.execute(
                        """
                        UPDATE sessions SET last_seen_at = now()
                        WHERE token_hash = :t AND expires_at > now()
                        RETURNING user_id
                        """,
                        {"t": digest},
                    ).fetchone()
                    if row is None:
                        return None
                    uid = row[0] if not hasattr(row, "_mapping") else row._mapping["user_id"]
                    user = conn.execute(
                        "SELECT username FROM users WHERE id = :u AND disabled_at IS NULL",
                        {"u": uid},
                    ).fetchone()
                    if user is None:
                        return None
                    name = user[0] if not hasattr(user, "_mapping") else user._mapping["username"]
                    return str(name), int(uid)
            except Exception as exc:
                log.warning("could not resolve a session, treating it as invalid: %s", exc)
                return None
        now = time.time()
        with self._lock:
            entry = self._memory.get(digest)
            if entry is None:
                return None
            username, _csrf, expires_at, uid = entry
            if expires_at <= now:
                del self._memory[digest]
                return None
            return username, uid

    def purge_expired(self) -> int:
        if self.persistent:
            try:
                with self._db.connect() as conn:
                    conn.execute("DELETE FROM sessions WHERE expires_at <= now()")
                    return 1
            except Exception as exc:
                log.warning("could not purge expired sessions: %s", exc)
                return 0
        now = time.time()
        with self._lock:
            gone = [k for k, v in self._memory.items() if v[2] <= now]
            for k in gone:
                del self._memory[k]
            return len(gone)

    def _ensure_user(self, username: str) -> int:
        """The users row for a username, created on first login if needed.

        Users exist in the environment today (``AUTH_USERNAME`` /
        ``AUTH_PASSWORD_HASH``) and that stays the credential source; this row
        exists so sessions and runs can be attributed to somebody. Kept
        deliberately dumb -- no password is ever stored here from this path.
        """
        cfg = get_auth_config()
        if not self.persistent:
            return 0
        try:
            with self._db.connect() as conn:
                row = conn.execute(
                    "SELECT id FROM users WHERE username = :u", {"u": username}
                ).fetchone()
                if row is not None:
                    uid = row[0] if not hasattr(row, "_mapping") else row._mapping["id"]
                    return int(uid)
                conn.execute(
                    """
                    INSERT INTO users (username, password_hash, is_admin)
                    VALUES (:u, :h, TRUE)
                    ON CONFLICT (username) DO UPDATE SET updated_at = now()
                    RETURNING id
                    """,
                    {"u": username, "h": cfg.password_hash or "unmanaged"},
                )
                row = conn.execute(
                    "SELECT id FROM users WHERE username = :u", {"u": username}
                ).fetchone()
                return int(row[0] if not hasattr(row, "_mapping") else row._mapping["id"])
        except Exception as exc:
            log.warning("could not resolve the user row for %r: %s", username, exc)
            return 0


# --------------------------------------------------------------------------
# login rate limiting
# --------------------------------------------------------------------------


class AttemptLimiter:
    """Sliding-window login limiter backed by ``login_attempts``.

    The same policy as the in-memory :class:`~app.auth.RateLimiter` it replaces
    -- ``max_attempts`` inside ``window``, then a ``block`` -- so switching it on
    does not change who can log in, only where the evidence is kept.

    The fallback is :data:`app.auth.login_limiter` itself rather than a private
    instance. Two limiters means two sets of counters: resetting one leaves the
    other throttling, which is exactly the kind of state nobody can reason about
    when a login mysteriously 429s.
    """

    def __init__(
        self,
        db: Database | None = None,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        window: int = DEFAULT_WINDOW_SECONDS,
        block: int = DEFAULT_BLOCK_SECONDS,
    ) -> None:
        self._db = db or get_db()
        self.max_attempts = max_attempts
        self.window = window
        self.block = block

    @property
    def _fallback(self):
        from app.auth import login_limiter

        return login_limiter

    @property
    def persistent(self) -> bool:
        return self._db.configured

    def check(self, ip: str, username: str = "") -> int:
        """Seconds left on a block, or 0 when the attempt is allowed."""
        if not self.persistent:
            return self._fallback.check(self._key(ip, username))
        try:
            with self._db.connect() as conn:
                row = conn.execute(
                    """
                    SELECT count(*) FROM login_attempts
                    WHERE ip = :ip AND ok = FALSE AND at > now() - make_interval(secs => :win)
                    """,
                    {"ip": ip, "win": float(self.window)},
                ).fetchone()
                failures = int(row[0] if row else 0)
                if failures < self.max_attempts:
                    return 0
                # Already over the threshold: hold the block open from the most
                # recent failure rather than from the first, so hammering the
                # endpoint keeps extending it.
                latest = conn.execute(
                    """
                    SELECT at FROM login_attempts
                    WHERE ip = :ip AND ok = FALSE
                    ORDER BY at DESC LIMIT 1
                    """,
                    {"ip": ip},
                ).fetchone()
                if latest is None:
                    return 0
                until = latest[0].timestamp() + self.block
                remaining = until - time.time()
                return int(remaining) if remaining > 0 else 0
        except Exception as exc:
            log.warning("login limiter falling back to memory: %s", exc)
            return self._fallback.check(self._key(ip, username))

    def record_failure(self, ip: str, username: str = "") -> None:
        self._record(ip, username, ok=False)

    def record_success(self, ip: str, username: str = "") -> None:
        self._record(ip, username, ok=True)

    def _record(self, ip: str, username: str, ok: bool) -> None:
        if not self.persistent:
            if ok:
                self._fallback.record_success(self._key(ip, username))
            else:
                self._fallback.record_failure(self._key(ip, username))
            return
        try:
            with self._db.connect() as conn:
                conn.execute(
                    "INSERT INTO login_attempts (ip, username, ok) VALUES (:ip, :u, :ok)",
                    {"ip": ip or "", "u": username or "", "ok": ok},
                )
                if ok:
                    # A success clears the record, exactly as the in-memory
                    # limiter's record_success did.
                    conn.execute(
                        "DELETE FROM login_attempts WHERE ip = :ip AND ok = FALSE",
                        {"ip": ip or ""},
                    )
        except Exception as exc:
            log.warning("could not record a login attempt: %s", exc)
            if not ok:
                self._fallback.record_failure(self._key(ip, username))

    @staticmethod
    def _key(ip: str, username: str) -> str:
        return f"{ip}|{username}".lower()


_sessions = SessionStore()
_attempts = AttemptLimiter()


def get_sessions() -> SessionStore:
    return _sessions


def get_attempts() -> AttemptLimiter:
    return _attempts


def reset_stores() -> None:
    """Test hook."""
    global _sessions, _attempts
    _sessions = SessionStore()
    _attempts = AttemptLimiter()
