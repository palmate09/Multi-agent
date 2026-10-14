-- Platform database schema.
--
-- What this database is: an **index**. What it is not: the artifact store.
-- Generated code, specs, UI and reports stay on the filesystem, because
-- reconstruct_state() reads them from there to implement resume, and
-- RunStore.rehydrate() rebuilds the run list by scanning directories. Moving
-- multi-megabyte code blobs into Postgres would mean rewriting both and would
-- make "resume after a crash" depend on the database being up.
--
-- So: rows here carry metadata and an index of what is on disk
-- (run_files.path/bytes/sha256). Losing this database loses metadata, not work;
-- reconcile() rebuilds it from disk.
--
-- Applied by app.db.engine.init_schema(). Idempotent.

CREATE EXTENSION IF NOT EXISTS citext;

-- ---------------------------------------------------------------- identity --

CREATE TABLE IF NOT EXISTS users (
    id            BIGSERIAL PRIMARY KEY,
    username      CITEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    is_admin      BOOLEAN NOT NULL DEFAULT FALSE,
    disabled_at   TIMESTAMPTZ,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Server-side sessions. Replaces stateless signed cookies: today a copied
-- cookie stays valid for its full 12 hours and logout cannot revoke it. Only a
-- SHA-256 of the token is stored, so a database dump does not hand over live
-- sessions.
CREATE TABLE IF NOT EXISTS sessions (
    id            BIGSERIAL PRIMARY KEY,
    user_id       BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_hash    TEXT NOT NULL UNIQUE,
    csrf_hash     TEXT NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at    TIMESTAMPTZ NOT NULL,
    last_seen_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    ip            TEXT,
    user_agent    TEXT
);
CREATE INDEX IF NOT EXISTS sessions_user_idx  ON sessions (user_id);
CREATE INDEX IF NOT EXISTS sessions_expiry_idx ON sessions (expires_at);

CREATE TABLE IF NOT EXISTS api_keys (
    id           BIGSERIAL PRIMARY KEY,
    user_id      BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name         TEXT NOT NULL,
    -- The visible prefix, so a key can be identified in a list without storing
    -- or showing the secret.
    prefix       TEXT NOT NULL,
    key_hash     TEXT NOT NULL UNIQUE,
    scopes       TEXT[] NOT NULL DEFAULT '{}',
    last_used_at TIMESTAMPTZ,
    revoked_at   TIMESTAMPTZ,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS api_keys_user_idx ON api_keys (user_id);

-- Durable rate limiting. The in-memory limiter it replaces is a per-process
-- defaultdict: it resets on restart and does nothing at all across replicas,
-- which matters the moment there is more than one backend container.
CREATE TABLE IF NOT EXISTS login_attempts (
    id        BIGSERIAL PRIMARY KEY,
    ip        TEXT NOT NULL DEFAULT '',
    username  TEXT NOT NULL DEFAULT '',
    at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    ok        BOOLEAN NOT NULL
);
-- The rate-limiter's only query is "how many failures from this ip recently",
-- so this is the index that has to exist for the login path not to degrade.
CREATE INDEX IF NOT EXISTS login_attempts_ip_at_idx ON login_attempts (ip, at DESC);
CREATE INDEX IF NOT EXISTS login_attempts_username_at_idx ON login_attempts (username, at DESC);

-- -------------------------------------------------------------------- runs --

CREATE TABLE IF NOT EXISTS runs (
    id           TEXT PRIMARY KEY,
    user_id      BIGINT REFERENCES users(id) ON DELETE SET NULL,
    requirement  TEXT NOT NULL,
    status       TEXT NOT NULL,
    options      JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at  TIMESTAMPTZ,
    error        TEXT,
    summary      JSONB
);
CREATE INDEX IF NOT EXISTS runs_created_idx ON runs (created_at DESC);
CREATE INDEX IF NOT EXISTS runs_user_idx    ON runs (user_id, created_at DESC);

-- Durable event log. Replaces an in-memory deque(maxlen=500): today a backend
-- restart loses the history entirely and a reconnecting SSE client sees
-- nothing. In a table, replay survives a restart and works behind
-- uvicorn --workers > 1, where an in-process buffer belongs to one worker.
CREATE TABLE IF NOT EXISTS run_events (
    run_id  TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    seq     INTEGER NOT NULL,
    node    TEXT NOT NULL,
    phase   TEXT NOT NULL,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    ts      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (run_id, seq)
);

-- An index of what is on disk, not the contents. reconcile() compares this
-- against the filesystem and rebuilds it, which is exactly what rehydrate()
-- already does without a database.
CREATE TABLE IF NOT EXISTS run_files (
    run_id     TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    path       TEXT NOT NULL,
    kind       TEXT NOT NULL DEFAULT 'other',
    bytes      BIGINT NOT NULL DEFAULT 0,
    sha256     TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (run_id, path)
);

CREATE TABLE IF NOT EXISTS preview_sessions (
    run_id       TEXT PRIMARY KEY REFERENCES runs(id) ON DELETE CASCADE,
    container_id TEXT,
    image        TEXT,
    port         INTEGER,
    state        TEXT NOT NULL DEFAULT 'stopped',
    ever_booted  BOOLEAN NOT NULL DEFAULT FALSE,
    started_at   TIMESTAMPTZ,
    last_hit_at  TIMESTAMPTZ,
    error        TEXT
);

-- -------------------------------------------------------------------- chat --

-- Shipped now so nothing has to be redesigned later, deliberately unused.
-- Chat is requirement-in / run-out today; adding a conversation loop is its
-- own phase and is not implied by these tables existing.
CREATE TABLE IF NOT EXISTS conversations (
    id         BIGSERIAL PRIMARY KEY,
    user_id    BIGINT REFERENCES users(id) ON DELETE CASCADE,
    title      TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS messages (
    id              BIGSERIAL PRIMARY KEY,
    conversation_id BIGINT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    role            TEXT NOT NULL,
    content         TEXT NOT NULL,
    run_id          TEXT,
    model           TEXT,
    tokens_in       INTEGER,
    tokens_out      INTEGER,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS messages_conversation_idx ON messages (conversation_id, id);

CREATE TABLE IF NOT EXISTS audit_log (
    id      BIGSERIAL PRIMARY KEY,
    user_id BIGINT REFERENCES users(id) ON DELETE SET NULL,
    action  TEXT NOT NULL,
    target  TEXT,
    at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    meta    JSONB NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS audit_log_at_idx ON audit_log (at DESC);
