"""Optional PostgreSQL backing store.

The database is an **index**, not the artifact store. Run artifacts stay on the
filesystem: ``reconstruct_state`` reads ``spec.yaml``, ``code/``, ``ui/`` and
``summary.json`` off disk to implement resume, and ``RunStore.rehydrate``
rebuilds the run list by scanning directories. That contract is load-bearing, so
moving artifact *contents* into Postgres would mean rewriting resume, restart
and rehydrate for no benefit. What lives in the database is metadata plus a
``run_files`` index of what is on disk, and :func:`reconcile` rebuilds that
index from the filesystem -- which is what rehydrate already does.

Everything here degrades to a no-op when ``DATABASE_URL`` is unset. That is not
a fallback bolted on for convenience: the offline test suite, ``cli.py`` and
local development all run without a database, and a store that must be up is a
store that makes the whole application unavailable when it is down.

Twelve-Factor: configured by environment only, no config files.
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import os
import threading
from pathlib import Path
from typing import Any

log = logging.getLogger("app.db")

SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"

#: The platform database. Distinct from ``SANDBOX_DATABASE_URL``, which is what
#: a *generated* app is given and which is per-run.
PLATFORM_DB_ENV = "DATABASE_URL"


def platform_dsn() -> str:
    return (os.getenv(PLATFORM_DB_ENV, "") or "").strip()


def token_hash(token: str) -> str:
    """SHA-256 of a bearer token, hex.

    Stored instead of the token so a database dump is not a set of live
    sessions and API keys. SHA-256 is right here and nowhere else: these are
    256-bit random values, not user-chosen passwords, so there is nothing to
    brute-force and nothing to slow down.
    """
    return hashlib.sha256(token.encode()).hexdigest()


class Database:
    """A lazily-created SQLAlchemy engine, or ``None`` when unconfigured."""

    def __init__(self, dsn: str | None = None) -> None:
        self._dsn = dsn if dsn is not None else platform_dsn()
        self._engine: Any = None
        self._lock = threading.Lock()
        self._ready = False

    @property
    def configured(self) -> bool:
        return bool(self._dsn)

    def engine(self):
        """The engine, or None. Never raises for "no database configured"."""
        if not self._dsn:
            return None
        with self._lock:
            if self._engine is not None:
                return self._engine
            try:
                from sqlalchemy import create_engine
            except ImportError:  # pragma: no cover - driver missing entirely
                log.warning("SQLAlchemy is unavailable; the database stays off")
                return None
            try:
                self._engine = create_engine(
                    self._dsn,
                    # A run's request is a handful of indexed point queries, and
                    # the pool's job is to survive a Postgres restart rather
                    # than to multiplex a lot of traffic.
                    pool_pre_ping=True,
                    pool_size=5,
                    max_overflow=5,
                    pool_recycle=1800,
                    future=True,
                )
            except Exception as exc:
                log.warning("could not create the database engine: %s", exc)
                return None
            return self._engine

    def connect(self):
        """A context manager yielding a connection, or None if unavailable.

        Yields None rather than raising when the database cannot be reached:
        every caller treats the database as an index that may be missing, and a
        database outage must not take the pipeline down with it.
        """
        engine = self.engine()
        if engine is None:
            return _NullConnection()
        return _Connection(engine)

    def available(self) -> bool:
        engine = self.engine()
        if engine is None:
            return False
        try:
            with engine.connect() as conn:
                conn.exec_driver_sql("SELECT 1")
            return True
        except Exception as exc:
            log.warning("database unreachable: %s", exc)
            return False

    def init_schema(self) -> bool:
        """Apply ``schema.sql``. Idempotent; False when there is no database."""
        engine = self.engine()
        if engine is None:
            return False
        try:
            sql = SCHEMA_PATH.read_text()
        except OSError as exc:
            log.warning("could not read %s: %s", SCHEMA_PATH, exc)
            return False
        try:
            with self._lock:
                with engine.begin() as conn:
                    conn.exec_driver_sql(sql)
                self._ready = True
        except Exception as exc:
            # An existing database with a different shape is a migration, not
            # something CREATE TABLE IF NOT EXISTS can paper over. Say so
            # loudly rather than silently running with a half-applied schema.
            log.error("could not apply the database schema: %s", exc)
            return False
        return True

    def dispose(self) -> None:
        with self._lock:
            if self._engine is not None:
                self._engine.dispose()
                self._engine = None


class _NullConnection:
    """Stands in for a connection when there is no database.

    Every statement method is a no-op returning an empty result, so a caller
    written against SQL works unchanged with the database switched off.
    """

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, *a, **k):
        return _NullResult()

    def exec_driver_sql(self, *a, **k):
        return _NullResult()

    def commit(self):
        return None

    def rollback(self):
        return None

    def close(self):
        return None


class _NullResult:
    """An empty stand-in for a SQLAlchemy result."""

    def fetchone(self):
        return None

    def fetchall(self):
        return []

    def scalars(self):
        return _NullScalars()

    def scalar(self):
        return None

    def scalar_one_or_none(self):
        return None

    def scalar_one(self):
        return None

    def first(self):
        return None

    def all(self):
        return []

    def one_or_none(self):
        return None


class _NullScalars:
    def all(self):
        return []

    def first(self):
        return None

    def one_or_none(self):
        return None


class _Statement:
    """Wraps a connection so call sites can pass plain SQL strings.

    ``Connection.execute`` refuses a bare ``str`` in SQLAlchemy 2.x: the named
    ``:param`` style needs ``sqlalchemy.text()``. Wrapping it here keeps every
    repository in this package readable as SQL rather than as a string builder,
    which is worth a small adapter.

    ``jsonb`` columns are the other reason: psycopg sends a Python dict as a
    composite type and the driver error names neither the column nor the cause,
    so parameters bound for those columns are serialised and the statement
    carries an explicit ``CAST(... AS jsonb)``.
    """

    def __init__(self, conn) -> None:
        self._conn = conn

    @staticmethod
    def _prepare(statement):
        if isinstance(statement, str):
            from sqlalchemy import text

            return text(statement)
        return statement

    def execute(self, statement, parameters=None):
        return self._conn.execute(self._prepare(statement), parameters or {})

    def exec_driver_sql(self, statement, parameters=None):
        return self._conn.exec_driver_sql(statement, parameters or {})

    def scalar(self, statement, parameters=None):
        return self.execute(statement, parameters).scalar()

    def scalar_one_or_none(self, statement, parameters=None):
        return self.execute(statement, parameters).scalar_one_or_none()

    def __getattr__(self, name):
        return getattr(self._conn, name)


class _Connection:
    """A transaction that yields a statement-wrapping session.

    SQLAlchemy 2.x splits the connection from the transaction: ``execute`` and
    ``scalar`` live on one, ``commit``/``rollback`` on the other. Yielding the
    transaction -- the obvious ``return engine.connect().begin()`` -- produces an
    object with no ``execute`` at all, and the first query fails with an
    AttributeError that says nothing about SQL.
    """

    def __init__(self, engine) -> None:
        self._engine = engine
        self._conn = None
        self._txn = None

    def __enter__(self) -> _Statement:
        self._conn = self._engine.connect()
        self._txn = self._conn.begin()
        return _Statement(self._conn)

    def __exit__(self, exc_type, exc, tb):
        try:
            if self._txn is not None:
                if exc_type is None:
                    self._txn.commit()
                else:
                    self._txn.rollback()
        except Exception:
            log.exception("database transaction failed")
            # An index write must never propagate into the request path.
            return True
        finally:
            with contextlib.suppress(Exception):
                if self._conn is not None:
                    self._conn.close()
        return False



_db: Database | None = None
_db_lock = threading.Lock()


def get_db() -> Database:
    """Process-wide handle. ``DATABASE_URL`` empty means "off"."""
    global _db
    with _db_lock:
        if _db is None:
            _db = Database()
        return _db


def reset_db() -> None:
    """Test hook."""
    global _db
    with _db_lock:
        if _db is not None:
            _db.dispose()
        _db = None
