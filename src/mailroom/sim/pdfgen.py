"""Synthetic text PDFs for the test corpora, the examples and the demo.

pypdf, the runtime dependency that already reads and cuts PDFs, assembles the file. This
module only describes the pages: US Letter, one of the standard fonts every reader has, and
a content stream that sets each line at a fixed baseline. A PDF reader extracts exactly those
lines again. The bytes depend only on the input, so content hashes are stable between runs.
"""

from __future__ import annotations

import io
from collections.abc import Sequence

from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

LETTER_POINTS = (612, 792)
POINT_SIZE = 11
BASELINE_STEP = 15                  # points from one line's baseline to the next
LEFT_EDGE, TOP_BASELINE = 72, 720   # an inch in from the left; the first line an inch down
FONT_KEY = "/Body"


def _standard_font() -> DictionaryObject:
    # Helvetica is one of the 14 standard fonts: nothing is embedded
    return DictionaryObject({
        NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type1"),
        NameObject("/BaseFont"): NameObject("/Helvetica"),
        NameObject("/Encoding"): NameObject("/WinAnsiEncoding"),
    })


def _set_line(text: str, baseline: int) -> bytes:
    # A hex string needs no escaping, whatever characters the line holds. WinAnsiEncoding is
    # Windows-1252; a character outside it becomes "?".
    glyphs = text.encode("cp1252", errors="replace").hex().upper().encode("ascii")
    return b"1 0 0 1 %d %d Tm <%s> Tj" % (LEFT_EDGE, baseline, glyphs)


def _page_content(lines: Sequence[str]) -> DecodedStreamObject:
    operators = [b"BT", FONT_KEY.encode("ascii") + b" %d Tf" % POINT_SIZE]
    operators += [_set_line(text, TOP_BASELINE - row * BASELINE_STEP)
                  for row, text in enumerate(lines)]
    operators.append(b"ET")
    content = DecodedStreamObject()
    content.set_data(b"\n".join(operators) + b"\n")
    return content


def make_pdf(pages: Sequence[Sequence[str]]) -> bytes:
    """A PDF with one page per entry of `pages`; an entry is that page's lines, top down."""
    if not pages:
        raise ValueError("a PDF needs at least one page")
    writer = PdfWriter()
    for lines in pages:
        page = writer.add_blank_page(*LETTER_POINTS)
        page[NameObject("/Resources")] = DictionaryObject({
            NameObject("/Font"): DictionaryObject({NameObject(FONT_KEY): _standard_font()})})
        page.replace_contents(_page_content(lines))
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()
