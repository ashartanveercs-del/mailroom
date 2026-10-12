"""Plug in your own splitter for PDFs that hold several documents.

    python examples/custom_splitter.py

Scanners can print a separator sheet between documents. This splitter cuts at those sheets
(and leaves them out). Return `confident=False` whenever you are not sure: the file is then
kept whole and sent to review, instead of being cut in the wrong place.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

from mailroom.documents import Expander
from mailroom.models import Attachment, Message
from mailroom.sim.pdfgen import make_pdf
from mailroom.split import Part, SplitPlan

SEPARATOR = "--- SEPARATOR SHEET ---"


class SeparatorSplitter:
    def plan(self, page_texts: Sequence[str]) -> SplitPlan:
        parts, start = [], None
        for number, text in enumerate(page_texts, start=1):
            if SEPARATOR in text:
                if start is not None:
                    parts.append(Part(start, number - 1))
                start = None
            elif start is None:
                start = number
        if start is not None:
            parts.append(Part(start, len(page_texts)))
        blank = any(not text.strip() for text in page_texts)
        return SplitPlan(tuple(parts), confident=bool(parts) and not blank,
                         reason="page_without_text" if blank else "")


def main() -> None:
    binder = make_pdf([["Lease, page 1"], ["Lease, page 2"], [SEPARATOR], ["Insurance"]])
    message = Message("msg-1", "office@example.com", "Scans",
                      datetime(2026, 4, 2, tzinfo=UTC),
                      attachments=(Attachment("binder.pdf", "application/pdf", binder),))
    documents, _ = Expander(splitter=SeparatorSplitter()).documents(message)
    for doc in documents:
        print(doc.key, "->", doc.text.replace("\n", " / "))


if __name__ == "__main__":
    main()
