"""The ledger: mailroom's durable memory, in one SQLite file.

It holds three things:
  * the write-ahead log of drive creates (`writes`): a row is committed *before* the request
    that creates the file or folder is sent, with the reserved id, the target folder and name
    and the content hashes; it becomes `done` only after the stored copy has been read back
    and verified. A crash anywhere leaves a row that recovery can settle.
  * one outcome per document (`documents`), so re-runs and restarts never handle a document
    twice, and duplicates and revisions can be recognised.
  * which messages are finished (`messages`), how many runs in a row the classifier failed on
    a document (`attempts`) or a message or a filing failed (`failures`), and an append-only
    audit trail (`events`).

Every method commits before it returns (autocommit, WAL, synchronous=FULL by default).
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .models import Outcome, OutcomeKind

SCHEMA = """
CREATE TABLE IF NOT EXISTS writes (
    seq        INTEGER PRIMARY KEY AUTOINCREMENT,
    file_id    TEXT NOT NULL UNIQUE,
    kind       TEXT NOT NULL CHECK (kind IN ('file', 'folder')),
    parent_id  TEXT NOT NULL,
    name       TEXT NOT NULL,
    status     TEXT NOT NULL CHECK (status IN
                   ('intent', 'done', 'rolled_back', 'conflict', 'orphan')),
    doc_key    TEXT,
    md5        TEXT,
    sha256     TEXT,
    size       INTEGER,
    adopted    INTEGER NOT NULL DEFAULT 0,
    note       TEXT,
    updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS writes_one_name_per_folder
    ON writes (parent_id, name) WHERE status IN ('intent', 'done', 'conflict');
CREATE INDEX IF NOT EXISTS writes_by_doc ON writes (doc_key);

CREATE TABLE IF NOT EXISTS documents (
    key        TEXT PRIMARY KEY,
    message_id TEXT NOT NULL,
    origin     TEXT NOT NULL,
    kind       TEXT NOT NULL,
    reason     TEXT NOT NULL,
    folder     TEXT NOT NULL,
    name       TEXT NOT NULL,
    file_id    TEXT,
    doc_type   TEXT NOT NULL,
    confidence REAL,
    sha256     TEXT NOT NULL,
    dry_run    INTEGER NOT NULL,
    identity   TEXT,
    final      INTEGER NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS documents_by_sha ON documents (sha256);
CREATE INDEX IF NOT EXISTS documents_by_identity ON documents (identity);

CREATE TABLE IF NOT EXISTS attempts (key TEXT PRIMARY KEY, count INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS failures (
    scope      TEXT NOT NULL CHECK (scope IN ('message', 'filing')),
    key        TEXT NOT NULL,
    count      INTEGER NOT NULL,
    last_error TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (scope, key)
);
CREATE TABLE IF NOT EXISTS messages (id TEXT PRIMARY KEY, done_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, kind TEXT NOT NULL,
    detail TEXT NOT NULL
);
"""

LIVE_NAME_STATUSES = ("intent", "done", "conflict", "orphan")


class NameTaken(Exception):
    """Another write already holds this name in this folder."""


class UnmanagedFolder(Exception):
    """The folder is neither the configured root nor one mailroom made or adopted under it."""


@dataclass(frozen=True)
class WriteRow:
    file_id: str
    kind: str
    parent_id: str
    name: str
    status: str
    doc_key: str | None
    md5: str | None
    sha256: str | None
    size: int | None


class Ledger:
    def __init__(self, path: str | Path = ":memory:", *, root_id: str, durable: bool = True,
                 clock: Callable[[], datetime] = lambda: datetime.now(UTC),
                 _conn: sqlite3.Connection | None = None) -> None:
        self.root_id = root_id
        self.clock = clock
        self.path = str(path)
        self.conn = _conn or sqlite3.connect(self.path, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        if _conn is None and self.path != ":memory:":
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute(f"PRAGMA synchronous={'FULL' if durable else 'OFF'}")
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    def snapshot(self) -> Ledger:
        """An independent in-memory copy (used by preview runs, which must not change state)."""
        copy = sqlite3.connect(":memory:", isolation_level=None)
        self.conn.backup(copy)
        return Ledger(root_id=self.root_id, clock=self.clock, _conn=copy)

    def _now(self) -> str:
        return self.clock().isoformat()

    # -- write-ahead log -------------------------------------------------------------------

    def begin_write(self, file_id: str, kind: str, parent_id: str, name: str, *,
                    doc_key: str | None = None, md5: str | None = None,
                    sha256: str | None = None, size: int | None = None) -> WriteRow:
        if not self.managed_folder(parent_id):
            raise UnmanagedFolder(parent_id)
        try:
            self.conn.execute(
                "INSERT INTO writes (file_id, kind, parent_id, name, status, doc_key, md5,"
                " sha256, size, updated_at) VALUES (?, ?, ?, ?, 'intent', ?, ?, ?, ?, ?)",
                (file_id, kind, parent_id, name, doc_key, md5, sha256, size, self._now()))
        except sqlite3.IntegrityError as err:
            raise NameTaken(f"{name!r} is already reserved in {parent_id}") from err
        return self.write(file_id)  # type: ignore[return-value]

    def _set_status(self, file_id: str, status: str, note: str | None = None) -> None:
        cur = self.conn.execute(
            "UPDATE writes SET status = ?, note = coalesce(?, note), updated_at = ?"
            " WHERE file_id = ?", (status, note, self._now(), file_id))
        if cur.rowcount != 1:
            raise KeyError(file_id)

    def finish_write(self, file_id: str) -> None:
        self._set_status(file_id, "done")

    def roll_back_write(self, file_id: str, reason: str) -> None:
        self._set_status(file_id, "rolled_back", reason)

    def mark_conflict(self, file_id: str, reason: str) -> None:
        self._set_status(file_id, "conflict", reason)

    def mark_orphan(self, file_id: str) -> None:
        self._set_status(file_id, "orphan", "landed after its intent was rolled back")

    def write(self, file_id: str) -> WriteRow | None:
        row = self.conn.execute("SELECT * FROM writes WHERE file_id = ?", (file_id,)).fetchone()
        return _write_row(row) if row else None

    def _writes(self, where: str, *args: Any) -> list[WriteRow]:
        rows = self.conn.execute(f"SELECT * FROM writes WHERE {where} ORDER BY seq", args)
        return [_write_row(r) for r in rows]

    def pending_writes(self) -> list[WriteRow]:
        return self._writes("status = 'intent'")

    def rolled_back_writes(self) -> list[WriteRow]:
        return self._writes("status = 'rolled_back'")

    def write_for_doc(self, doc_key: str) -> WriteRow | None:
        rows = self._writes("doc_key = ? AND status IN ('intent', 'done', 'conflict')", doc_key)
        return rows[-1] if rows else None

    def reserved_names(self, parent_id: str) -> set[str]:
        marks = ",".join("?" * len(LIVE_NAME_STATUSES))
        rows = self.conn.execute(
            f"SELECT name FROM writes WHERE parent_id = ? AND status IN ({marks})",
            (parent_id, *LIVE_NAME_STATUSES))
        return {r["name"] for r in rows}

    def managed_folder(self, folder_id: str) -> bool:
        if folder_id == self.root_id:
            return True
        row = self.conn.execute(
            "SELECT 1 FROM writes WHERE file_id = ? AND kind = 'folder' AND status = 'done'",
            (folder_id,)).fetchone()
        return row is not None

    def folder_for(self, parent_id: str, name: str) -> str | None:
        row = self.conn.execute(
            "SELECT file_id FROM writes WHERE parent_id = ? AND name = ? AND kind = 'folder'"
            " AND status = 'done' ORDER BY seq LIMIT 1", (parent_id, name)).fetchone()
        return row["file_id"] if row else None

    def adopt_folder(self, folder_id: str, parent_id: str, name: str) -> None:
        """Record a folder found under a managed folder as managed. A folder the ledger
        already knows (say, one mailroom made, stopped using when it was trashed, and now
        finds restored) is taken back under its current place and name."""
        if not self.managed_folder(parent_id):
            raise UnmanagedFolder(parent_id)
        try:
            self.conn.execute(
                "INSERT INTO writes (file_id, kind, parent_id, name, status, adopted,"
                " updated_at) VALUES (?, 'folder', ?, ?, 'done', 1, ?)"
                " ON CONFLICT (file_id) DO UPDATE SET status = 'done', adopted = 1,"
                " parent_id = excluded.parent_id, name = excluded.name,"
                " updated_at = excluded.updated_at WHERE kind = 'folder'",
                (folder_id, parent_id, name, self._now()))
        except sqlite3.IntegrityError as err:  # another live row holds this name here
            raise NameTaken(f"{name!r} is already reserved in {parent_id}") from err
        if not self.managed_folder(folder_id):  # the id is one the ledger knows as a file
            raise UnmanagedFolder(folder_id)

    # -- documents -------------------------------------------------------------------------

    def plan(self, outcome: Outcome, identity: str | None = None) -> None:
        """Record a decision before acting on it. A re-run after a crash finishes this plan
        instead of asking the classifier again (it might answer differently)."""
        self._store(outcome, identity, final=False)

    def record(self, outcome: Outcome, identity: str | None = None) -> None:
        self._store(outcome, identity, final=True)

    def _store(self, outcome: Outcome, identity: str | None, final: bool) -> None:
        if identity is None:  # keep the identity recorded with the plan
            row = self.conn.execute("SELECT identity FROM documents WHERE key = ?",
                                    (outcome.key,)).fetchone()
            identity = row["identity"] if row else None
        self.conn.execute(
            "INSERT OR REPLACE INTO documents (key, message_id, origin, kind, reason, folder,"
            " name, file_id, doc_type, confidence, sha256, dry_run, identity, final, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (outcome.key, outcome.message_id, outcome.origin, outcome.kind.value,
             outcome.reason, json.dumps(list(outcome.folder)), outcome.name, outcome.file_id,
             outcome.doc_type, outcome.confidence, outcome.sha256, int(outcome.dry_run),
             identity, int(final), self._now()))

    def _document(self, key: str, final: bool) -> Outcome | None:
        row = self.conn.execute("SELECT * FROM documents WHERE key = ? AND final = ?",
                                (key, int(final))).fetchone()
        return _outcome(row) if row else None

    def outcome(self, key: str) -> Outcome | None:
        return self._document(key, final=True)

    def planned(self, key: str) -> Outcome | None:
        return self._document(key, final=False)

    def outcomes(self) -> list[Outcome]:
        rows = self.conn.execute("SELECT * FROM documents WHERE final = 1 ORDER BY key")
        return [_outcome(r) for r in rows]

    def handled_with_sha(self, sha256: str) -> Outcome | None:
        """An earlier document with these exact bytes that was (or is being) filed or sent to
        review."""
        row = self.conn.execute(
            "SELECT * FROM documents WHERE sha256 = ? AND kind IN ('filed', 'review')"
            " ORDER BY updated_at, key LIMIT 1", (sha256,)).fetchone()
        return _outcome(row) if row else None

    def filed_with_identity(self, identity: str) -> Outcome | None:
        row = self.conn.execute(
            "SELECT * FROM documents WHERE identity = ? AND kind = 'filed'"
            " ORDER BY updated_at, key LIMIT 1", (identity,)).fetchone()
        return _outcome(row) if row else None

    def bump_attempts(self, key: str) -> int:
        self.conn.execute("INSERT INTO attempts (key, count) VALUES (?, 1) ON CONFLICT (key)"
                          " DO UPDATE SET count = count + 1", (key,))
        return self.conn.execute("SELECT count FROM attempts WHERE key = ?",
                                 (key,)).fetchone()["count"]

    def record_failure(self, scope: str, key: str, error: str) -> int:
        """Count one more failed run for a message or a filing (`scope`) and keep the latest
        error. Returns the count so far."""
        self.conn.execute(
            "INSERT INTO failures (scope, key, count, last_error, updated_at)"
            " VALUES (?, ?, 1, ?, ?) ON CONFLICT (scope, key) DO UPDATE SET"
            " count = count + 1, last_error = excluded.last_error,"
            " updated_at = excluded.updated_at", (scope, key, error, self._now()))
        return self.conn.execute("SELECT count FROM failures WHERE scope = ? AND key = ?",
                                 (scope, key)).fetchone()["count"]

    # -- messages and audit ----------------------------------------------------------------

    def message_done(self, message_id: str) -> bool:
        return self.conn.execute("SELECT 1 FROM messages WHERE id = ?",
                                 (message_id,)).fetchone() is not None

    def mark_message_done(self, message_id: str) -> None:
        self.conn.execute("INSERT OR IGNORE INTO messages (id, done_at) VALUES (?, ?)",
                          (message_id, self._now()))

    def log(self, event: str, /, **detail: Any) -> None:
        self.conn.execute("INSERT INTO events (at, kind, detail) VALUES (?, ?, ?)",
                          (self._now(), event, json.dumps(detail, sort_keys=True, default=str)))

    def events(self) -> list[tuple[str, dict[str, Any]]]:
        rows = self.conn.execute("SELECT kind, detail FROM events ORDER BY seq")
        return [(r["kind"], json.loads(r["detail"])) for r in rows]


def _write_row(row: sqlite3.Row) -> WriteRow:
    return WriteRow(row["file_id"], row["kind"], row["parent_id"], row["name"], row["status"],
                    row["doc_key"], row["md5"], row["sha256"], row["size"])


def _outcome(row: sqlite3.Row) -> Outcome:
    return Outcome(key=row["key"], message_id=row["message_id"], origin=row["origin"],
                   kind=OutcomeKind(row["kind"]), reason=row["reason"],
                   folder=tuple(json.loads(row["folder"])), name=row["name"],
                   file_id=row["file_id"], doc_type=row["doc_type"],
                   confidence=row["confidence"], sha256=row["sha256"],
                   dry_run=bool(row["dry_run"]))
