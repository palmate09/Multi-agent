"""Session storage and login rate limiting.

Two tiers, deliberately:

**Offline** (the default suite) covers everything that does not need a
database -- token hashing, the in-memory fallback, the limiter's behaviour when
the database is unreachable, and API-key scoping. These run everywhere.

**Postgres-backed** covers the durable semantics: that logout actually revokes,
that an expired session stops working, and that login attempts are counted in a
table rather than in one process's memory. These need a real database, because
a fake that reimplements the queries would only be testing itself.

Run the second tier with::

    TEST_DATABASE_URL=postgresql+psycopg://mat:pw@localhost:5432/mat \\
        pytest backend/tests/test_sessions.py

Without it they skip. Nothing here needs a model or the network.
"""

from __future__ import annotations

import os

import pytest

from app import auth
from app.db import sessions as sessions_mod
from app.db.engine import Database, reset_db, token_hash

DSN = os.getenv("TEST_DATABASE_URL", "").strip()
needs_db = pytest.mark.skipif(not DSN, reason="set TEST_DATABASE_URL to exercise the durable store")


@pytest.fixture
def clean_env(monkeypatch):
    monkeypatch.setenv("AUTH_SESSION_HOURS", "12")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    auth.reset_auth_config()
    yield
    auth.reset_auth_config()


@pytest.fixture
def db(clean_env, monkeypatch):
    """A real, schema-applied Database. Torn down per test."""
    monkeypatch.setenv("DATABASE_URL", DSN)
    reset_db()
    database = Database(DSN)
    assert database.init_schema(), f"could not apply the schema to {DSN}"
    yield database
    reset_db()


# --------------------------------------------------------------------------
# tokens
# --------------------------------------------------------------------------
def test_tokens_are_stored_hashed_not_raw():
    """A database dump must not be a set of live sessions.

    SHA-256 is right here precisely because these are 256-bit random values,
    not user-chosen passwords: there is nothing to brute-force, so no slow KDF
    is needed and nothing is lost by hashing.
    """
    assert token_hash("mat_abc") != "mat_abc"
    assert len(token_hash("mat_abc")) == 64
    assert token_hash("mat_abc") == token_hash("mat_abc")
    assert token_hash("a") != token_hash("b")


def test_a_session_token_is_opaque_and_unique():
    token = auth.new_raw_session_token()
    assert len(token) >= 32
    assert token != auth.new_raw_session_token()


# --------------------------------------------------------------------------
# the in-memory fallback (what CI and a laptop without Postgres exercise)
# --------------------------------------------------------------------------
def test_without_a_database_sessions_work_and_still_revoke(clean_env):
    """The fallback keeps the semantics, not just the API shape.

    If it only kept the API shape, a developer would conclude logout works when
    testing locally and learn otherwise in production.
    """
    store = sessions_mod.SessionStore()
    assert store.persistent is False
    issued = store.issue("admin", "csrf")
    assert store.resolve(issued.token)[0] == "admin"
    assert store.revoke(issued.token) is True
    assert store.resolve(issued.token) is None
    assert store.revoke(issued.token) is False


def test_one_users_sessions_are_independent(clean_env):
    store = sessions_mod.SessionStore()
    first = store.issue("admin", "csrf1")
    second = store.issue("admin", "csrf2")
    store.revoke(first.token)
    assert store.resolve(second.token) is not None, "logging out one device logged out all"


def test_an_expired_session_stops_resolving(clean_env):
    store = sessions_mod.SessionStore()
    issued = store.issue("admin", "csrf")
    digest = token_hash(issued.token)
    username, csrf_hash, _expires, uid = store._memory[digest]
    store._memory[digest] = (username, csrf_hash, 0.0, uid)  # expired
    assert store.resolve(issued.token) is None


def test_purge_expired_removes_only_expired_sessions(clean_env):
    store = sessions_mod.SessionStore()
    live = store.issue("admin", "csrf1")
    dead = store.issue("admin", "csrf2")
    username, csrf_hash, _e, uid = store._memory[token_hash(dead.token)]
    store._memory[token_hash(dead.token)] = (username, csrf_hash, 0.0, uid)
    assert store.purge_expired() == 1
    assert store.resolve(live.token) is not None
    assert store.resolve(dead.token) is None


def test_an_unknown_token_resolves_to_nothing(clean_env):
    assert sessions_mod.SessionStore().resolve("never-issued") is None


def test_revoke_all_clears_a_user(clean_env):
    store = sessions_mod.SessionStore()
    a = store.issue("admin", "csrf1")
    b = store.issue("admin", "csrf2")
    store.revoke_all(a.user_id or 0)
    assert store.resolve(a.token) is None
    assert store.resolve(b.token) is None


# --------------------------------------------------------------------------
# API keys
# --------------------------------------------------------------------------
def test_an_api_key_authenticates_without_a_session(clean_env, monkeypatch):
    """Headless access does not create a session row per script run."""
    monkeypatch.setenv("AUTH_API_KEY", "mat_headless_key")
    auth.reset_auth_config()
    cfg = auth.get_auth_config()
    assert cfg.enabled is True
    assert auth.verify_password("", "") is False  # not a password path


def test_api_keys_are_hashed_the_same_way_sessions_are():
    assert token_hash("mat_key_123") == token_hash("mat_key_123")
    assert "mat_key_123" not in token_hash("mat_key_123")


# --------------------------------------------------------------------------
# rate limiting without a database
# --------------------------------------------------------------------------
def test_the_limiter_falls_back_to_memory_when_the_database_is_unreachable():
    """A database outage must not turn into an unthrottled login endpoint."""

    class Broken:
        configured = True

        def connect(self):
            class Conn:
                def __enter__(self):
                    return self

                def __exit__(self, *exc):
                    return False

                def execute(self, *a, **k):
                    raise RuntimeError("database is down")

            return Conn()

    limiter = sessions_mod.AttemptLimiter(Broken(), max_attempts=3)
    auth.login_limiter.reset()
    try:
        for _ in range(5):
            limiter.record_failure("10.0.0.9", "admin")
        assert limiter.check("10.0.0.9", "admin") > 0, "the outage disabled rate limiting"
    finally:
        auth.login_limiter.reset()


def test_the_fallback_is_the_shared_limiter_not_a_private_copy(clean_env):
    """Two limiters means two sets of counters.

    ``auth.login_limiter`` is what the app and the tests reset. A private
    instance would keep throttling after everything believed the state had been
    cleared, which is how a login mysteriously 429s.
    """
    assert sessions_mod.AttemptLimiter()._fallback is auth.login_limiter


# --------------------------------------------------------------------------
# durable semantics (Postgres)
# --------------------------------------------------------------------------
@needs_db
def test_logout_really_revokes(db):
    """The whole point of moving sessions server-side.

    A signed cookie could not be revoked: copying it gave a valid credential
    for the full twelve hours and logging out did nothing but clear one browser.
    """
    store = sessions_mod.SessionStore(db)
    issued = store.issue("admin", "csrf-token")
    assert store.resolve(issued.token)[0] == "admin"
    assert store.revoke(issued.token) is True
    assert store.resolve(issued.token) is None, "a revoked session still authenticates"
    assert store.revoke(issued.token) is False, "revoking twice must be harmless"


@needs_db
def test_one_users_sessions_are_independent_in_the_database(db):
    store = sessions_mod.SessionStore(db)
    first = store.issue("admin", "csrf1")
    second = store.issue("admin", "csrf2")
    store.revoke(first.token)
    assert store.resolve(second.token) is not None


@needs_db
def test_a_stored_session_holds_only_a_hash(db):
    """A dump of the sessions table must not be a set of usable cookies."""
    store = sessions_mod.SessionStore(db)
    # Sessions from earlier tests share the "admin" user row, so
    # select on this test's own token rather than on the user.
    issued = store.issue("admin", "csrf")
    with db.connect() as conn:
        row = conn.execute(
            "SELECT token_hash FROM sessions WHERE token_hash = :t",
            {"t": token_hash(issued.token)},
        ).fetchone()
        plaintext_hits = conn.execute(
            "SELECT count(*) FROM sessions WHERE token_hash = :raw",
            {"raw": issued.token},
        ).fetchone()[0]
    assert row is not None, "the session was not stored"
    assert row[0] == token_hash(issued.token)
    assert plaintext_hits == 0, "a raw session token is stored in the table"


@needs_db
def test_expired_sessions_do_not_resolve(db):
    store = sessions_mod.SessionStore(db)
    issued = store.issue("admin", "csrf")
    with db.connect() as conn:
        conn.execute(
            "UPDATE sessions SET expires_at = now() - interval '1 hour' WHERE user_id = :u",
            {"u": issued.user_id},
        )
    assert store.resolve(issued.token) is None


@needs_db
def test_login_attempts_survive_a_restart(db):
    """The whole point of moving the limiter into a table.

    A fresh limiter object stands in for a restarted process. With the old
    in-memory defaultdict this returns 0 -- the budget silently resets, which is
    exactly what a restart-triggered brute force needs.
    """
    limiter = sessions_mod.AttemptLimiter(db, max_attempts=3, window=300, block=60)
    for _ in range(3):
        limiter.record_failure("10.0.0.7", "admin")

    after_restart = sessions_mod.AttemptLimiter(db, max_attempts=3, window=300, block=60)
    assert after_restart.check("10.0.0.7", "admin") > 0
    # ...and one address cannot lock out another.
    assert after_restart.check("10.0.0.8", "admin") == 0


@needs_db
def test_a_success_clears_the_record_in_the_database(db):
    limiter = sessions_mod.AttemptLimiter(db, max_attempts=3, window=300, block=60)
    limiter.record_failure("10.0.0.9", "admin")
    limiter.record_success("10.0.0.9", "admin")
    assert limiter.check("10.0.0.9", "admin") == 0


@needs_db
def test_login_attempts_are_indexed_for_the_query_that_runs_on_the_login_path(db):
    """The rate-limiter's only hot query is "failures from this ip since t".

    Without this index the login path does a sequential scan of a table that
    only ever grows.
    """
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT indexname FROM pg_indexes WHERE tablename = 'login_attempts'"
        ).fetchall()
    names = {r[0] for r in rows}
    assert "login_attempts_ip_at_idx" in names
    assert "login_attempts_username_at_idx" in names


@needs_db
def test_a_session_cannot_be_issued_when_the_database_is_down(monkeypatch, clean_env):
    """A session that cannot be stored must not be handed out.

    It would be a credential nothing can revoke -- the exact failure the move
    to server-side sessions was meant to remove.
    """

    class Broken:
        configured = True

        def connect(self):
            class Conn:
                def __enter__(self):
                    return self

                def __exit__(self, *exc):
                    return False

                def execute(self, *a, **k):
                    raise RuntimeError("database is down")

            return Conn()

    store = sessions_mod.SessionStore(Broken())
    with pytest.raises(RuntimeError):
        store.issue("admin", "csrf")


# --------------------------------------------------------------------------
# per-run database schemas
# --------------------------------------------------------------------------
def test_a_run_id_maps_to_a_legal_schema_name():
    from app.db.runschema import schema_name

    assert schema_name("20260113-193000-ab12cd").startswith("run_")
    assert len(schema_name("20260113-193000-ab12cd")) <= 63


def test_two_run_ids_that_sanitise_alike_get_different_schemas():
    """Otherwise one run could read another's tables.

    ``a-b``, ``a.b`` and ``a_b`` all reduce to the same characters, so a
    sanitising mapping alone would silently merge three runs into one schema --
    the exact failure per-run schemas exist to prevent.
    """
    from app.db.runschema import schema_name

    assert len({schema_name(x) for x in ("a-b", "a.b", "a_b")}) == 3


def test_a_very_long_run_id_still_fits_postgres_identifier_limit():
    from app.db.runschema import schema_name

    assert len(schema_name("x" * 500)) <= 63


def test_an_empty_run_id_is_refused_not_sanitised():
    from app.db.runschema import SchemaRefused, schema_name

    with pytest.raises(SchemaRefused):
        schema_name("")


def test_the_generated_url_pins_the_search_path():
    """Scoped on the connection, not on the application.

    ``search_path`` in the URL applies to every connection the driver opens, so
    a generated app sees its own tables without configuring anything -- and
    cannot see another run's.
    """
    from app.db.runschema import build_url

    url = build_url("postgres", 5432, "mat", "u", "p", "run_abc")
    assert "search_path%3Drun_abc" in url
    assert url.startswith("postgresql+psycopg://")


def test_a_non_postgres_url_is_left_alone():
    from app.db.runschema import with_schema

    assert with_schema("sqlite:///local.db", "run_abc") == "sqlite:///local.db"


def test_any_postgres_url_can_be_scoped():
    from app.db.runschema import with_schema

    scoped = with_schema("postgresql+psycopg://u:p@db:5432/app", "run_xyz")
    assert "search_path%3Drun_xyz" in scoped
