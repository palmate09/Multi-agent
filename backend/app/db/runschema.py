"""Per-run database schemas for generated apps.

Generated apps default to PostgreSQL, and the default must be a **per-run
schema**. A single shared database would let one run read another's tables,
which breaks the independence the rest of the system depends on -- a tester
that passes because it read the developer's data is a false green.

So each run gets its own schema inside one database, and the container is given
credentials scoped to it. Creating and dropping are both here because they are
one lifecycle: a schema that outlives its container is a credential that
outlives it too.

``search_path`` is set on the connection URL rather than trusted to the
application, so a generated app that connects to its own database sees its own
tables without having to configure anything, and cannot see another's.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from urllib.parse import quote, urlsplit, urlunsplit

log = logging.getLogger("app.db.runschema")

#: Run ids are `[A-Za-z0-9][A-Za-z0-9._-]{0,63}` (app.services.runs._SAFE_ID).
#: A schema name additionally cannot contain anything but identifier characters,
#: so the whole thing is validated rather than quoted-and-hoped-for.
_SCHEMA_PREFIX = "run_"
_VALID = re.compile(r"^[a-z0-9_]+$")


class SchemaRefused(ValueError):
    """A run id that cannot safely become a schema name."""


@dataclass(frozen=True)
class RunSchema:
    """One run's schema and the URL a container should be started with."""

    run_id: str
    schema: str
    username: str
    password: str

    def dsn(self, host: str, port: int, database: str, db_user: str, db_password: str) -> str:
        return build_url(
            host=host,
            port=port,
            database=database,
            user=self.username,
            password=self.password,
            schema=self.schema,
        )


def schema_name(run_id: str) -> str:
    """The schema for a run, or refuse.

    Sanitising alone is not enough and would be a serious bug: ``a-b``, ``a.b``
    and ``a_b`` all reduce to the same characters, so two different runs would
    share one schema and one run could read another's tables -- precisely the
    failure the per-run schema exists to prevent, arriving silently. A short
    digest of the *original* id is therefore part of the name, which makes the
    mapping injective in practice.

    PostgreSQL truncates identifiers at 63 bytes, so the readable part is
    bounded and the digest goes last: a truncation would otherwise silently
    collide two long run ids.
    """
    raw = (run_id or "").strip()
    if not raw:
        raise SchemaRefused("run id is empty")
    digest = hashlib.sha256(raw.encode()).hexdigest()[:10]
    readable = re.sub(r"[^A-Za-z0-9_]", "_", raw).lower().strip("_") or "run"
    readable = readable[: 63 - len(_SCHEMA_PREFIX) - len(digest) - 1]
    candidate = f"{_SCHEMA_PREFIX}{readable}_{digest}"
    if not _VALID.match(candidate) or len(candidate.encode()) > 63:
        raise SchemaRefused(f"run id {run_id!r} cannot become a schema name")
    return candidate


def build_url(
    host: str,
    port: int,
    database: str,
    user: str,
    password: str,
    schema: str,
) -> str:
    """A SQLAlchemy/psycopg URL pinned to one schema.

    ``options=-csearch_path%3D<schema>`` is the part that matters: it is set on
    the connection itself, so the scoping holds for every connection the
    application opens rather than for the first one the driver happened to
    create.
    """
    query = quote(f"-csearch_path={schema}", safe="")
    return f"postgresql+psycopg://{quote(user, safe='')}:{quote(password, safe='')}@{host}:{port}/{database}?options={query}"


def with_schema(dsn: str, schema: str) -> str:
    """Rewrite any Postgres URL to point at ``schema``.

    Used by the sandbox sidecar, which is handed a plain ``DATABASE_URL`` and
    must attach the per-run scoping itself rather than trusting a caller to
    have done it.
    """
    parts = urlsplit(dsn)
    if not parts.scheme.startswith("postgres"):
        return dsn
    query = quote(f"-csearch_path={schema}", safe="")
    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, f"options={query}", parts.fragment)
    )
