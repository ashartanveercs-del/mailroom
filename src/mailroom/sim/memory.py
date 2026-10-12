"""In-memory implementations of the Mailbox and Drive ports.

The drive fake keeps the semantics the filing code relies on: ids are reserved up front, a
create with an existing id fails, files need a live parent folder, and nothing can be updated
or deleted. It also keeps an operation log and supports crash injection (see `faults.py`).
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace

from ..models import Message
from ..ports import FOLDER_MIME, AlreadyExists, DriveError, StoredFile
from .faults import CrashPlan


class InMemoryMailbox:
    def __init__(self, messages: Iterable[Message]) -> None:
        self._messages = {m.id: m for m in messages}
        self.calls: list[tuple[str, str]] = []

    def list_message_ids(self, query: str) -> list[str]:
        self.calls.append(("list", query))
        ordered = sorted(self._messages.values(), key=lambda m: (m.received_at, m.id))
        return [m.id for m in ordered]

    def get_message(self, message_id: str) -> Message:
        self.calls.append(("get", message_id))
        return self._messages[message_id]


@dataclass
class _Entry:
    meta: StoredFile
    data: bytes = b""
    description: str = ""


class InMemoryDrive:
    def __init__(self, root_id: str = "root", *, crash: CrashPlan | None = None) -> None:
        self.root_id = root_id
        self.crash = crash
        self._files: dict[str, _Entry] = {
            root_id: _Entry(StoredFile(root_id, "Filing", (), FOLDER_MIME))}
        self._counter = 0
        self._own_counter = 0
        self.write_attempts = 0
        self.writes = 0
        self.landed_attempts: list[int] = []  # attempt indices that stored something
        self.ops: list[tuple[str, str, str]] = []  # (operation, parent id, name)
        self.reads = 0

    # -- the port --------------------------------------------------------------------------

    def reserve_ids(self, count: int) -> list[str]:
        ids = []
        for _ in range(count):
            self._counter += 1
            ids.append(f"mem-{self._counter:05d}")
        return ids

    def create_folder(self, folder_id: str, name: str, parent_id: str) -> StoredFile:
        meta = StoredFile(folder_id, name, (parent_id,), FOLDER_MIME)
        return self._create("create_folder", _Entry(meta))

    def create_file(self, file_id: str, name: str, parent_id: str, data: bytes,
                    mime_type: str, description: str = "") -> StoredFile:
        meta = StoredFile(file_id, name, (parent_id,), mime_type,
                          md5=hashlib.md5(data).hexdigest(), size=len(data))
        return self._create("create_file", _Entry(meta, bytes(data), description))

    def get(self, file_id: str) -> StoredFile | None:
        self.reads += 1
        entry = self._files.get(file_id)
        if entry is None:
            return None
        return replace(entry.meta, trashed=self._trashed(file_id))

    def find_children(self, parent_id: str, name: str,
                      folders_only: bool = False) -> list[StoredFile]:
        self.reads += 1
        found = [e.meta for fid, e in self._files.items()
                 if parent_id in e.meta.parents and e.meta.name == name
                 and not self._trashed(fid) and (e.meta.is_folder or not folders_only)]
        return sorted(found, key=lambda m: m.id)

    def _create(self, op: str, entry: _Entry) -> StoredFile:
        index = self.write_attempts
        self.write_attempts += 1
        if self.crash:
            self.crash.check(index, "before")
        meta = entry.meta
        parent = self._files.get(meta.parents[0])
        if parent is None or not parent.meta.is_folder or self._trashed(parent.meta.id):
            raise DriveError(f"parent folder {meta.parents[0]} does not exist")
        if meta.id in self._files:
            raise AlreadyExists(meta.id)
        self._files[meta.id] = entry
        self.writes += 1
        self.landed_attempts.append(index)
        self.ops.append((op, meta.parents[0], meta.name))
        if self.crash:
            self.crash.check(index, "after")
        return meta

    def _trashed(self, file_id: str) -> bool:
        seen = set()
        while file_id and file_id not in seen:
            seen.add(file_id)
            entry = self._files.get(file_id)
            if entry is None:
                return False
            if entry.meta.trashed:
                return True
            file_id = entry.meta.parents[0] if entry.meta.parents else ""
        return False

    # -- the owner's side, and inspection helpers ------------------------------------------

    def _own_id(self) -> str:
        self._own_counter += 1
        return f"own-{self._own_counter:05d}"

    def add_folder(self, name: str, parent_id: str | None) -> str:
        fid = self._own_id()
        parents = (parent_id,) if parent_id else ()
        self._files[fid] = _Entry(StoredFile(fid, name, parents, FOLDER_MIME))
        return fid

    def add_file(self, name: str, parent_id: str, data: bytes) -> str:
        fid = self._own_id()
        self._files[fid] = _Entry(StoredFile(fid, name, (parent_id,), "application/octet-stream",
                                             md5=hashlib.md5(data).hexdigest(), size=len(data)),
                                  data)
        return fid

    def trash(self, file_id: str) -> None:
        entry = self._files[file_id]
        entry.meta = replace(entry.meta, trashed=True)

    def restore(self, file_id: str) -> None:
        """Take a file or folder back out of the trash, as its owner can."""
        entry = self._files[file_id]
        entry.meta = replace(entry.meta, trashed=False)

    def tamper(self, file_id: str, data: bytes) -> None:
        entry = self._files[file_id]
        entry.data = data
        entry.meta = replace(entry.meta, md5=hashlib.md5(data).hexdigest(), size=len(data))

    def data(self, file_id: str) -> bytes:
        return self._files[file_id].data

    def description(self, file_id: str) -> str:
        return self._files[file_id].description

    def path_of(self, file_id: str) -> str | None:
        parts: list[str] = []
        current = file_id
        while current != self.root_id:
            entry = self._files.get(current)
            if entry is None or not entry.meta.parents:
                return None
            parts.append(entry.meta.name)
            current = entry.meta.parents[0]
        return "/".join(reversed(parts))

    def _under_root(self, predicate: Callable[[StoredFile], bool]) -> dict[str, StoredFile]:
        out = {}
        for fid, entry in self._files.items():
            if fid == self.root_id or not predicate(entry.meta) or self._trashed(fid):
                continue
            path = self.path_of(fid)
            if path is not None:
                out[path] = entry.meta
        return out

    def tree(self) -> dict[str, str]:
        """{path under the root: md5} for every live file (folders are implied by paths)."""
        files = self._under_root(lambda m: not m.is_folder)
        return {path: meta.md5 or "" for path, meta in sorted(files.items())}

    def folder_paths(self) -> list[str]:
        return sorted(self._under_root(lambda m: m.is_folder))

    def file_count(self) -> int:
        return sum(1 for e in self._files.values() if not e.meta.is_folder)
