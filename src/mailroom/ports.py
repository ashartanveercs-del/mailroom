"""The boundaries mailroom depends on. Production adapters live in `mailroom.google`; in-memory
fakes for tests and the evaluation gate live in `mailroom.sim`.

The mailbox port has no method that changes anything: read-only intake is a property of the
interface, and the HTTP allowlist enforces it again on the wire for the real adapter.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .models import Message


class RunCannotContinue(Exception):
    """Nothing else in this run can succeed either: the account's credentials were refused,
    say. Raised from anywhere, it stops the run, instead of being charged to one message or
    document the way every other failure is."""


class Mailbox(Protocol):
    def list_message_ids(self, query: str) -> list[str]:
        """Ids of messages matching `query`, oldest first. An adapter that cuts a very long
        listing short keeps the newest messages and sets a `listing_truncated` attribute,
        which the run reports."""
        ...

    def get_message(self, message_id: str) -> Message: ...


@dataclass(frozen=True)
class StoredFile:
    id: str
    name: str
    parents: tuple[str, ...]
    mime_type: str
    md5: str | None = None
    size: int | None = None
    trashed: bool = False

    @property
    def is_folder(self) -> bool:
        return self.mime_type == FOLDER_MIME


FOLDER_MIME = "application/vnd.google-apps.folder"


class AlreadyExists(Exception):
    """A create named an id that already exists (an earlier attempt landed)."""


class DriveError(Exception):
    """A drive operation failed in a way a retry will not fix (missing parent, refused)."""


class Drive(Protocol):
    """Create-only file storage. There is no update, move or delete: an existing file can
    never be overwritten because nothing here can write to an existing file."""

    def reserve_ids(self, count: int) -> list[str]: ...

    def create_folder(self, folder_id: str, name: str, parent_id: str) -> StoredFile: ...

    def create_file(self, file_id: str, name: str, parent_id: str, data: bytes,
                    mime_type: str, description: str = "") -> StoredFile: ...

    def get(self, file_id: str) -> StoredFile | None: ...

    def find_children(self, parent_id: str, name: str,
                      folders_only: bool = False) -> list[StoredFile]: ...
