"""Plain data that flows through a run: messages in, documents in the middle, outcomes out."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from functools import cached_property


@dataclass(frozen=True)
class Attachment:
    filename: str
    mime_type: str
    data: bytes
    part_id: str = ""
    inline: bool = False


@dataclass(frozen=True)
class Message:
    id: str
    sender: str
    subject: str
    received_at: datetime
    labels: frozenset[str] = frozenset()
    attachments: tuple[Attachment, ...] = ()
    # parts the mailbox adapter chose not to download, with the reason (e.g. too large)
    skipped: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class Document:
    """One thing to file: an attachment, a file found inside a container, or one part of a PDF
    that held several documents. `key` is stable across runs and crashes."""

    key: str
    message_id: str
    origin: str
    filename: str
    mime_type: str
    data: bytes
    text: str = ""
    kind: str = "binary"
    flags: tuple[str, ...] = ()

    @cached_property
    def sha256(self) -> str:
        return hashlib.sha256(self.data).hexdigest()


@dataclass(frozen=True)
class Classification:
    doc_type: str
    confidence: float
    fields: Mapping[str, str] = field(default_factory=dict)
    source: str = ""
    # text a vision model read off an image; the only text an image-only document has
    transcript: str = ""


@dataclass(frozen=True)
class Destination:
    folder: tuple[str, ...]
    name: str
    review: bool = False
    reason: str = ""


class OutcomeKind(StrEnum):
    FILED = "filed"          # confident: filed under its type's folder
    REVIEW = "review"        # filed into the review folder, with a reason
    DUPLICATE = "duplicate"  # same bytes as a document already handled; not filed again
    SKIPPED = "skipped"      # not a document (inline logo, signature file) or cannot be filed
    DEFERRED = "deferred"    # a transient failure; retried on the next run
    CONFLICT = "conflict"    # a stored copy does not match its record; a person must look


TERMINAL = frozenset({OutcomeKind.FILED, OutcomeKind.REVIEW, OutcomeKind.DUPLICATE,
                      OutcomeKind.SKIPPED, OutcomeKind.CONFLICT})


@dataclass(frozen=True)
class Outcome:
    key: str
    message_id: str
    origin: str
    kind: OutcomeKind
    reason: str = ""
    folder: tuple[str, ...] = ()
    name: str = ""
    file_id: str | None = None
    doc_type: str = ""
    confidence: float | None = None
    sha256: str = ""
    dry_run: bool = False

    @property
    def path(self) -> str:
        return "/".join((*self.folder, self.name)) if self.name else ""
