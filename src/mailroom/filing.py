"""Write-ahead, hash-verified filing.

Every create follows the same protocol:

  1. reserve an id from the drive (nothing is stored yet)
  2. commit a ledger row: id, folder, name, md5 + sha256 + size of the exact bytes  <- write-ahead
  3. create the file *with that id*; a 409 means an earlier attempt already landed
  4. read the stored copy back and compare folder, name, size and md5 with the row
  5. only then mark the row done: the document counts as filed

Because the id is chosen before the upload, a crash at any point leaves a row that says exactly
which file to look for. Recovery reads it back: if it is there and matches, the filing is rolled
forward; if it never landed, the same id is re-sent when the bytes are at hand (resume) or the
intent is rolled back so the next run files the document from the mailbox again. A stored copy
that does not match its row is a conflict for a person: nothing here can update, move or delete
a file, so nothing is ever overwritten.
"""

from __future__ import annotations

import contextlib
import hashlib
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from .guard import BlockedRequest
from .ledger import Ledger, WriteRow
from .ports import AlreadyExists, Drive, DriveError, StoredFile


class FilingConflict(Exception):
    """The stored copy does not match the write-ahead record. A person needs to look."""


@dataclass(frozen=True)
class Filed:
    file_id: str | None
    name: str
    parent_id: str
    md5: str


@dataclass
class Recovery:
    completed: list[str] = field(default_factory=list)    # landed and verified: rolled forward
    resumed: list[str] = field(default_factory=list)      # folder creates re-sent with their id
    rolled_back: list[str] = field(default_factory=list)  # never landed; the next run refiles
    conflicts: list[str] = field(default_factory=list)    # landed but does not match
    orphans: list[str] = field(default_factory=list)      # landed after being rolled back

    @property
    def clean(self) -> bool:
        return not (self.conflicts or self.orphans)


def _hashes(data: bytes) -> tuple[str, str]:
    return hashlib.md5(data).hexdigest(), hashlib.sha256(data).hexdigest()


def _mismatch(meta: StoredFile | None, row: WriteRow) -> str | None:
    if meta is None:
        return "the stored copy is missing"
    if meta.trashed:
        return "the stored copy is in the trash"
    if row.parent_id not in meta.parents:
        return "the stored copy is in a different folder"
    if meta.name != row.name:
        return "the stored copy has a different name"
    if row.kind == "folder":
        return None if meta.is_folder else "the stored item is not a folder"
    if row.size is not None and meta.size is not None and meta.size != row.size:
        return "the stored copy has a different size"
    if meta.md5 != row.md5:
        return "the stored copy's content hash does not match"
    return None


def _candidates(name: str, limit: int):
    path = PurePosixPath(name)
    stem, suffix = (path.stem, path.suffix) if path.suffix else (name, "")
    yield name
    for n in range(2, limit + 1):
        yield f"{stem}_{n}{suffix}"


class Filer:
    def __init__(self, drive: Drive, ledger: Ledger, *, max_name_tries: int = 500) -> None:
        self.drive = drive
        self.ledger = ledger
        self.max_name_tries = max_name_tries
        self._folders: dict[tuple[str, str], str] = {}

    @property
    def root_id(self) -> str:
        return self.ledger.root_id

    # -- folders ---------------------------------------------------------------------------

    def folder(self, path: Sequence[str]) -> str:
        """The id of the folder at `path` under the root, created write-ahead if missing."""
        parent = self.root_id
        for name in path:
            parent = self._child_folder(parent, name)
        return parent

    def _child_folder(self, parent: str, name: str) -> str:
        cached = self._folders.get((parent, name))
        if cached:
            return cached
        known = self.ledger.folder_for(parent, name)
        if known:
            meta = self.drive.get(known)
            if meta and meta.is_folder and not meta.trashed and parent in meta.parents:
                self._folders[(parent, name)] = known
                return known
            # trashed or moved away by someone: stop using it, never write into it
            self.ledger.roll_back_write(known, "folder trashed or moved by someone else")
        existing = [f for f in self.drive.find_children(parent, name, folders_only=True)
                    if not f.trashed]
        if existing:
            folder_id = existing[0].id
            self.ledger.adopt_folder(folder_id, parent, name)
            self.ledger.log("folder_adopted", folder_id=folder_id, parent_id=parent, name=name)
        else:
            [folder_id] = self.drive.reserve_ids(1)
            row = self.ledger.begin_write(folder_id, "folder", parent, name)
            # AlreadyExists: an earlier attempt landed
            with self._refusal_rolls_back(row), contextlib.suppress(AlreadyExists):
                self.drive.create_folder(folder_id, name, parent)
            self._confirm(row)
        self._folders[(parent, name)] = folder_id
        return folder_id

    # -- files -----------------------------------------------------------------------------

    def file(self, doc_key: str, data: bytes, name: str, parent_id: str, mime_type: str,
             description: str = "") -> Filed:
        """File `data` once for `doc_key`. Safe to call again after any crash or failure."""
        row = self.ledger.write_for_doc(doc_key)
        if row is not None and row.status == "done":
            return Filed(row.file_id, row.name, row.parent_id, row.md5 or "")
        if row is not None and row.status == "conflict":
            raise FilingConflict(f"{doc_key}: an earlier copy does not match its record")
        if row is not None:  # an intent left by a crash
            settled = self._settle(row, data, mime_type, description)
            if settled is not None:
                return settled
        md5, sha256 = _hashes(data)
        self._usable_folder(parent_id)
        name = self._free_name(parent_id, name)
        [file_id] = self.drive.reserve_ids(1)
        row = self.ledger.begin_write(file_id, "file", parent_id, name, doc_key=doc_key,
                                      md5=md5, sha256=sha256, size=len(data))
        with self._refusal_rolls_back(row), contextlib.suppress(AlreadyExists):
            # AlreadyExists: a retried request whose first try landed
            self.drive.create_file(file_id, name, parent_id, data, mime_type, description)
        return self._confirm(row)

    @contextlib.contextmanager
    def _refusal_rolls_back(self, row: WriteRow) -> Iterator[None]:
        """The allowlist checks a request before it is sent, so a refused create never left
        the process: its intent can be rolled back now instead of by the next recovery."""
        try:
            yield
        except BlockedRequest:
            self.ledger.roll_back_write(row.file_id, "refused by the allowlist; never sent")
            self.ledger.log("write_refused", file_id=row.file_id, doc_key=row.doc_key)
            raise

    def _usable_folder(self, folder_id: str) -> None:
        meta = self.drive.get(folder_id)
        if meta is None or not meta.is_folder or meta.trashed:
            raise DriveError(f"destination folder {folder_id} is missing or in the trash")

    def _taken(self, parent_id: str, name: str) -> bool:
        return bool(self.drive.find_children(parent_id, name))

    def _free_name(self, parent_id: str, name: str, extra: set[str] | None = None) -> str:
        reserved = self.ledger.reserved_names(parent_id) | (extra or set())
        for candidate in _candidates(name, self.max_name_tries):
            if candidate not in reserved and not self._taken(parent_id, candidate):
                return candidate
        raise DriveError(f"no free name for {name!r} after {self.max_name_tries} tries")

    def _confirm(self, row: WriteRow) -> Filed:
        problem = _mismatch(self.drive.get(row.file_id), row)
        if problem:
            self.ledger.mark_conflict(row.file_id, problem)
            self.ledger.log("write_conflict", file_id=row.file_id, doc_key=row.doc_key,
                            problem=problem)
            raise FilingConflict(f"{row.name}: {problem}")
        self.ledger.finish_write(row.file_id)
        self.ledger.log("write_confirmed", file_id=row.file_id, kind=row.kind,
                        parent_id=row.parent_id, name=row.name, doc_key=row.doc_key)
        return Filed(row.file_id, row.name, row.parent_id, row.md5 or "")

    def _settle(self, row: WriteRow, data: bytes | None = None, mime_type: str = "",
                description: str = "") -> Filed | None:
        """Finish an intent left behind by a crash. Returns None if it was rolled back."""
        if self.drive.get(row.file_id) is not None:
            return self._confirm(row)  # it landed: verify and roll forward
        if row.kind == "folder":
            # AlreadyExists: a resent create whose reply was lost
            with self._refusal_rolls_back(row), contextlib.suppress(AlreadyExists):
                self.drive.create_folder(row.file_id, row.name, row.parent_id)
            return self._confirm(row)
        if data is not None and hashlib.sha256(data).hexdigest() == row.sha256:
            with self._refusal_rolls_back(row), contextlib.suppress(AlreadyExists):
                self.drive.create_file(row.file_id, row.name, row.parent_id, data, mime_type,
                                       description)
            return self._confirm(row)  # resumed with the same id
        self.ledger.roll_back_write(row.file_id, "the upload never landed")
        self.ledger.log("write_rolled_back", file_id=row.file_id, doc_key=row.doc_key)
        return None

    def recover(self) -> Recovery:
        """Settle every unfinished write. Run at startup, before any new work."""
        report = Recovery()
        for row in self.ledger.pending_writes():
            try:
                landed = self.drive.get(row.file_id) is not None
                settled = self._settle(row)
            except FilingConflict:
                report.conflicts.append(row.file_id)
                continue
            if settled is None:
                report.rolled_back.append(row.file_id)
            elif landed:
                report.completed.append(row.file_id)
            else:
                report.resumed.append(row.file_id)
        for row in self.ledger.rolled_back_writes():
            if row.kind == "file" and self.drive.get(row.file_id) is not None:
                self.ledger.mark_orphan(row.file_id)
                self.ledger.log("write_orphaned", file_id=row.file_id, doc_key=row.doc_key)
                report.orphans.append(row.file_id)
        return report


class DryRunFiler(Filer):
    """Preview: the same folder and name decisions, without a single write. Folders that do
    not exist yet get placeholder ids; names already used by this preview count as taken."""

    def __init__(self, drive: Drive, ledger: Ledger) -> None:
        super().__init__(drive, ledger)
        self._used: dict[str, set[str]] = {}

    def _child_folder(self, parent: str, name: str) -> str:
        if not parent.startswith("preview:"):
            known = self.ledger.folder_for(parent, name)
            if known:
                return known
            existing = [f for f in self.drive.find_children(parent, name, folders_only=True)
                        if not f.trashed]
            if existing:
                return existing[0].id
        return f"preview:{parent}/{name}"

    def _taken(self, parent_id: str, name: str) -> bool:
        return not parent_id.startswith("preview:") and super()._taken(parent_id, name)

    def file(self, doc_key: str, data: bytes, name: str, parent_id: str, mime_type: str,
             description: str = "") -> Filed:
        used = self._used.setdefault(parent_id, set())
        name = self._free_name(parent_id, name, extra=used)
        used.add(name)
        return Filed(None, name, parent_id, hashlib.md5(data).hexdigest())

    def recover(self) -> Recovery:
        return Recovery()
