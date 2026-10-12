"""PDF page text and page-range cutting, on top of pypdf (the one runtime dependency).

Splitting a PDF means walking its page tree, cross-reference tables and object streams and
writing a new, valid file per part; that is a parser's job, not something to hand-roll.
"""

from __future__ import annotations

import io
import logging
from collections.abc import Sequence

import pypdf

logging.getLogger("pypdf").setLevel(logging.ERROR)


class PdfUnreadable(Exception):
    def __init__(self, message: str, *, encrypted: bool = False) -> None:
        super().__init__(message)
        self.encrypted = encrypted


def _reader(data: bytes) -> pypdf.PdfReader:
    try:
        reader = pypdf.PdfReader(io.BytesIO(data))
    except Exception as err:
        raise PdfUnreadable(f"not a readable PDF: {err}") from err
    if reader.is_encrypted:
        raise PdfUnreadable("the PDF is password protected", encrypted=True)
    return reader


def page_texts(data: bytes) -> list[str]:
    """The text of every page, in order ("" for a page with no text layer, e.g. a scan)."""
    reader = _reader(data)
    try:
        return [(page.extract_text() or "").strip() for page in reader.pages]
    except Exception as err:
        raise PdfUnreadable(f"could not read the PDF's pages: {err}") from err


def cut(data: bytes, ranges: Sequence[tuple[int, int]]) -> list[bytes]:
    """One new PDF per inclusive, 1-based page range. Output is deterministic for the same
    input, so a part's hash (and therefore its identity in the ledger) is stable."""
    reader = _reader(data)
    count = len(reader.pages)
    parts = []
    for first, last in ranges:
        if not 1 <= first <= last <= count:
            raise ValueError(f"page range {first}-{last} is outside a {count}-page document")
        writer = pypdf.PdfWriter()
        for index in range(first - 1, last):
            writer.add_page(reader.pages[index])
        writer.add_metadata({"/Producer": "mailroom"})
        buf = io.BytesIO()
        writer.write(buf)
        parts.append(buf.getvalue())
    return parts
