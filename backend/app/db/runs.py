"""The run index: metadata and an inventory, not the artifacts themselves.

Artifact *contents* stay on the filesystem. ``reconstruct_state`` reads
``spec.yaml``, ``code/``, ``ui/`` and ``summary.json`` off disk to implement
resume, and ``RunStore.rehydrate`` rebuilds the run list by scanning
directories. That contract is load-bearing -- it is what makes "resume after a
crash" work at all -- so moving code blobs into Postgres would mean rewriting
resume, restart and rehydrate for no benefit.

What this module adds on top of the filesystem:

``runs``
    the list query the UI renders, so it does not require a directory scan on
    every page load
``run_events``
    durable event log, replacing an in-memory ``deque(maxlen=500)`` where a
    restart loses the history and a reconnecting SSE client sees nothing. In a
    table, replay survives a restart and works behind ``--workers > 1``.
``run_files``
    ``path``, ``bytes``, ``sha256`` per file: an inventory, so "what did this
    run produce" is one indexed query instead of a walk, and so a truncated or
    tampered artifact is detectable.

:func:`reconcile` is the safety net. The index is derived state; if it and the
filesystem disagree, the filesystem wins and the index is rebuilt from it.
That is the same operation ``rehydrate`` already performs, so there is no new
failure mode -- only a faster one, and a durable copy of the answer.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from app.db.engine import Database, get_db

log = logging.getLogger("app.runs.db")

#: Files that are the platform's own bookkeeping rather than a produced
#: artifact. Indexed anyway -- they are small and they are what a resume reads --
#: but this is the classification used by ``kind``.
_KIND_BY_SUFFIX = {
    ".py": "code",
    ".js": "code",
    ".html": "ui",
    ".css": "ui",
    ".json": "data",
    ".yaml": "spec",
    ".yml": "spec",
    ".md": "doc",
    ".txt": "log",
}


def classify(rel: str) -> str:
    top = rel.split("/", 1)[0]
    if top in {"code", "tests", "ui"}:
        return {"code": "code", "tests": "tests", "ui": "ui"}[top]
    suffix = Path(rel).suffix.lower()
    return _KIND_BY_SUFFIX.get(suffix, "other")


def file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


class RunIndex:
    """Read/write access to the run tables. Every method no-ops without a database."""

    def __init__(self, db: Database | None = None) -> None:
        self._db = db or get_db()

    @property
    def enabled(self) -> bool:
        return self._db.configured

    # ------------------------------------------------------------------ runs --
    def upsert_run(
        self,
        run_id: str,
        requirement: str,
        status: str,
        options: dict | None = None,
        summary: dict | None = None,
        error: str | None = None,
        user_id: int | None = None,
    ) -> bool:
        if not self.enabled:
            return False
        try:
            with self._db.connect() as conn:
                conn.execute(
                    """
                    INSERT INTO runs (id, user_id, requirement, status, options, summary, error)
                    VALUES (:id, :uid, :req, :st, CAST(:opt AS jsonb),
                            CAST(:sum AS jsonb), :err)
                    ON CONFLICT (id) DO UPDATE SET
                        status = EXCLUDED.status,
                        options = EXCLUDED.options,
                        summary = EXCLUDED.summary,
                        error = EXCLUDED.error,
                        updated_at = now(),
                        finished_at = CASE WHEN EXCLUDED.summary IS NOT NULL
                                          THEN now() ELSE runs.finished_at END
                    """,
                    {
                        "id": run_id,
                        "uid": user_id,
                        "req": requirement,
                        "st": status,
                        # jsonb parameters go through CAST: psycopg sends a
                        # dict as a composite type otherwise, and the error
                        # names neither the column nor the cause.
                        "opt": json.dumps(options or {}),
                        "sum": json.dumps(summary) if summary is not None else None,
                        "err": error,
                    },
                )
            return True
        except Exception as exc:
            log.warning("could not index run %s: %s", run_id, exc)
            return False

    def touch_run(self, run_id: str, status: str) -> None:
        if not self.enabled:
            return
        try:
            with self._db.connect() as conn:
                conn.execute(
                    "UPDATE runs SET status = :st, updated_at = now() WHERE id = :id",
                    {"st": status, "id": run_id},
                )
        except Exception as exc:
            log.warning("could not update run %s: %s", run_id, exc)

    def get_run(self, run_id: str) -> dict | None:
        if not self.enabled:
            return None
        try:
            with self._db.connect() as conn:
                row = conn.execute(
                    "SELECT id, requirement, status, created_at, updated_at, summary "
                    "FROM runs WHERE id = :id",
                    {"id": run_id},
                ).fetchone()
                if row is None:
                    return None
                return {
                    "id": row[0],
                    "requirement": row[1],
                    "status": row[2],
                    "created_at": row[3],
                    "updated_at": row[4],
                    "summary": row[5] if len(row) > 5 else None,
                }
        except Exception as exc:
            log.warning("could not read run %s: %s", run_id, exc)
            return None

    def delete_run(self, run_id: str) -> bool:
        if not self.enabled:
            return False
        try:
            with self._db.connect() as conn:
                conn.execute("DELETE FROM runs WHERE id = :id", {"id": run_id})
            return True
        except Exception as exc:
            log.warning("could not delete run %s: %s", run_id, exc)
            return False

    # ---------------------------------------------------------------- events --
    def append_event(self, run_id: str, seq: int, node: str, phase: str, payload: dict) -> bool:
        """Append one pipeline event.

        Deliberately best-effort and non-blocking in effect: the in-memory deque
        still exists, because an SSE stream must not stutter because a database
        write is slow. The table is the durable copy that survives a restart.
        """
        if not self.enabled:
            return False
        try:
            with self._db.connect() as conn:
                conn.execute(
                    """
                    INSERT INTO run_events (run_id, seq, node, phase, payload)
                    VALUES (:r, :s, :n, :p, CAST(:pay AS jsonb))
                    ON CONFLICT (run_id, seq) DO NOTHING
                    """,
                    {
                        "r": run_id,
                        "s": int(seq),
                        "n": node,
                        "p": phase,
                        "pay": json.dumps(payload or {}),
                    },
                )
            return True
        except Exception as exc:
            log.debug("could not persist an event for %s: %s", run_id, exc)
            return False

    def events(self, run_id: str, since_seq: int = -1, limit: int = 500) -> list[dict]:
        """Events with ``seq > since_seq``. The default is -1, not 0.

        Sequence numbers start at 0, so a default of 0 silently drops the very
        first event of every run from a replay -- which is exactly the event a
        reconnecting client needs, since it is usually the one that says what
        the run started doing.
        """

        if not self.enabled:
            return []
        try:
            with self._db.connect() as conn:
                rows = conn.execute(
                    """
                    SELECT seq, node, phase, payload, ts FROM run_events
                    WHERE run_id = :r AND seq > :s
                    ORDER BY seq LIMIT :lim
                    """,
                    {"r": run_id, "s": int(since_seq), "lim": int(limit)},
                ).fetchall()
            out = []
            for row in rows:
                payload = row[3]
                if isinstance(payload, str):
                    try:
                        payload = json.loads(payload)
                    except json.JSONDecodeError:
                        payload = {}
                out.append(
                    {
                        "seq": row[0],
                        "node": row[1],
                        "phase": row[2],
                        "payload": payload or {},
                        "ts": row[4],
                    }
                )
            return out
        except Exception as exc:
            log.warning("could not replay events for %s: %s", run_id, exc)
            return []

    def max_seq(self, run_id: str) -> int:
        if not self.enabled:
            return 0
        try:
            with self._db.connect() as conn:
                row = conn.execute(
                    "SELECT COALESCE(max(seq), 0) FROM run_events WHERE run_id = :r",
                    {"r": run_id},
                ).fetchone()
                return int(row[0] if row else 0)
        except Exception:
            return 0

    # ----------------------------------------------------------------- files --
    def index_files(self, run_id: str, run_dir: Path) -> int:
        """(Re)build ``run_files`` for one run by walking its directory."""
        if not self.enabled:
            return 0
        indexed = 0
        try:
            rows = []
            for path in sorted(run_dir.rglob("*")):
                if not path.is_file() or "__pycache__" in path.parts:
                    continue
                rel = str(path.relative_to(run_dir))
                try:
                    size = path.stat().st_size
                except OSError:
                    continue
                rows.append((run_id, rel, classify(rel), size))
            with self._db.connect() as conn:
                conn.execute("DELETE FROM run_files WHERE run_id = :r", {"r": run_id})
                for row in rows:
                    # Hashing is the expensive part and it is what makes the row
                    # worth having, so it is computed outside the transaction.
                    digest = None
                    with contextlib.suppress(OSError):
                        digest = file_digest(run_dir / row[1])
                    conn.execute(
                        """
                        INSERT INTO run_files (run_id, path, kind, bytes, sha256)
                        VALUES (:r, :p, :k, :b, :h)
                        ON CONFLICT (run_id, path) DO UPDATE SET
                            kind = EXCLUDED.kind, bytes = EXCLUDED.bytes, sha256 = EXCLUDED.sha256
                        """,
                        {"r": row[0], "p": row[1], "k": row[2], "b": row[3], "h": digest},
                    )
                    indexed += 1
        except Exception as exc:
            log.warning("could not index files for %s: %s", run_id, exc)
            return indexed
        return indexed

    def files(self, run_id: str) -> list[dict]:
        if not self.enabled:
            return []
        try:
            with self._db.connect() as conn:
                rows = conn.execute(
                    "SELECT path, kind, bytes, sha256 FROM run_files "
                    "WHERE run_id = :r ORDER BY path",
                    {"r": run_id},
                ).fetchall()
            return [{"path": r[0], "kind": r[1], "bytes": r[2], "sha256": r[3]} for r in rows]
        except Exception as exc:
            log.warning("could not list indexed files for %s: %s", run_id, exc)
            return []

    # -------------------------------------------------------------- preview --
    def record_preview(self, run_id: str, **fields: Any) -> None:
        if not self.enabled:
            return
        columns = {"container_id", "image", "port", "state", "ever_booted", "started_at", "error"}
        keys = [k for k in fields if k in columns]
        if not keys:
            return
        try:
            with self._db.connect() as conn:
                conn.execute(
                    """
                    INSERT INTO preview_sessions (run_id, container_id, image, port,
                                                 state, ever_booted, started_at, error,
                                                 last_hit_at)
                    VALUES (:run_id, :container_id, :image, :port, :state,
                            :ever_booted, to_timestamp(:started_at), :error, now())
                    ON CONFLICT (run_id) DO UPDATE SET
                        -- COALESCE, not EXCLUDED: a caller reporting only
                        -- `state` must not erase the container id and port it
                        -- recorded a moment ago.
                        container_id = COALESCE(EXCLUDED.container_id, preview_sessions.container_id),
                        image = COALESCE(EXCLUDED.image, preview_sessions.image),
                        port = COALESCE(EXCLUDED.port, preview_sessions.port),
                        state = EXCLUDED.state,
                        -- Sticky, and deliberately so: "did this app ever boot"
                        -- is a fact about the run, not about one container's
                        -- lifetime. Overwriting it with EXCLUDED would make a
                        -- stopped preview look like one that never worked.
                        ever_booted = preview_sessions.ever_booted OR EXCLUDED.ever_booted,
                        error = EXCLUDED.error,
                        last_hit_at = now()
                    """,
                    {
                        "run_id": run_id,
                        "container_id": fields.get("container_id"),
                        "image": fields.get("image"),
                        "port": fields.get("port"),
                        "state": fields.get("state") or "stopped",
                        "ever_booted": bool(fields.get("ever_booted")),
                        "started_at": fields.get("started_at"),
                        "error": fields.get("error"),
                    },
                )
        except Exception as exc:
            log.debug("could not record the preview state for %s: %s", run_id, exc)

    def requirement(self, run_id: str) -> str | None:
        """The stored requirement text for a run, or ``None``.

        A last-resort source for `rehydrate`, for directories written before
        `run.json` existed. The index is derived from the filesystem, so this
        can only ever be as good as what was indexed -- never better.
        """
        if not self.enabled:
            return None
        try:
            with self._db.connect() as conn:
                row = conn.execute(
                    "SELECT requirement FROM runs WHERE id = :i", {"i": run_id}
                ).fetchone()
            return str(row[0]) if row and row[0] is not None else None
        except Exception as exc:
            log.debug("could not read the requirement for %s: %s", run_id, exc)
            return None

    # ---------------------------------------------------------- reconcile --
    def reconcile(self, run_dirs: Iterable[tuple[str, Path]]) -> dict[str, int]:
        """Make the index agree with the filesystem, or drop what it cannot verify.

        The filesystem is authoritative. A run present on disk but absent from
        the index is re-indexed from its ``summary.json``; a run in the index
        with no directory is removed, because its artifacts are gone and a row
        pointing at nothing is worse than no row.

        This is what makes the database safe to treat as derived state: it can
        be rebuilt from the artifact store at any time, with nothing lost.
        """
        stats = {"indexed": 0, "removed": 0, "skipped": 0}
        if not self.enabled:
            return stats
        on_disk = dict(run_dirs)
        try:
            with self._db.connect() as conn:
                known = {
                    row[0] for row in conn.execute("SELECT id FROM runs").fetchall()
                }
                for run_id in known - set(on_disk):
                    conn.execute("DELETE FROM runs WHERE id = :r", {"r": run_id})
                    stats["removed"] += 1
        except Exception as exc:
            log.warning("reconcile could not prune: %s", exc)
            return stats

        for run_id, path in on_disk.items():
            try:
                summary = json.loads((path / "summary.json").read_text())
            except (OSError, json.JSONDecodeError):
                stats["skipped"] += 1
                continue
            self.upsert_run(
                run_id,
                summary.get("requirement", ""),
                summary.get("status", "unknown"),
                summary=summary,
                error=summary.get("error"),
            )
            self.index_files(run_id, path)
            stats["indexed"] += 1
        return stats


_index: RunIndex | None = None


def get_index() -> RunIndex:
    global _index
    if _index is None:
        _index = RunIndex()
    return _index


def reset_index() -> None:
    """Test hook."""
    global _index
    _index = None
