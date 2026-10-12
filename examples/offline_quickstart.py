"""File a small, made-up mailbox with your own document type and classifier, fully offline.

    python examples/offline_quickstart.py

Everything external is an in-memory fake here; swap in `mailroom.app.build_app` (or the Gmail
and Drive adapters) for the real thing. The classifier is any object with
`classify(document) -> Classification`: a model client, a rules engine, or this.
"""

from __future__ import annotations

from datetime import UTC, datetime

from mailroom.documents import Expander
from mailroom.ledger import Ledger
from mailroom.models import Attachment, Classification, Document, Message
from mailroom.pipeline import Mailroom
from mailroom.routing import DocType, Policy
from mailroom.sim.memory import InMemoryDrive, InMemoryMailbox
from mailroom.sim.pdfgen import make_pdf

STATEMENT = DocType(
    name="statement",
    description="A monthly bank statement",
    folder="Bank statements/{date:%Y}",
    filename="{date:%Y-%m} {vendor} statement",
    keywords=("statement", "opening balance", "closing balance"),
    min_confidence=0.8,
)


class TinyClassifier:
    """Stands in for a model. Whatever it says is still checked: the date and the bank's name
    must appear in the document, or it goes to review."""

    def classify(self, doc: Document) -> Classification:
        if "closing balance" in doc.text.lower():
            return Classification("statement", 0.9,
                                  {"vendor": "Example Credit Union", "date": "2026-03-31"},
                                  source="example")
        return Classification("other", 0.1, source="example")


def main() -> None:
    statement = make_pdf([["Example Credit Union", "Monthly statement",
                           "Statement date: 03/31/2026", "Opening balance: $1,200.00",
                           "Closing balance: $1,350.00"]])
    menu = make_pdf([["Lunch menu", "Soup of the day"]])
    mailbox = InMemoryMailbox([
        Message("msg-1", "statements@example.com", "Your March statement",
                datetime(2026, 4, 1, 9, 0, tzinfo=UTC),
                attachments=(Attachment("march.pdf", "application/pdf", statement),)),
        Message("msg-2", "office@example.com", "Friday lunch",
                datetime(2026, 4, 1, 10, 0, tzinfo=UTC),
                attachments=(Attachment("lunch-menu.pdf", "application/pdf", menu),)),
    ])
    drive = InMemoryDrive("root")
    ledger = Ledger(":memory:", root_id="root")
    mailroom = Mailroom(mailbox=mailbox, drive=drive, ledger=ledger,
                        classifier=TinyClassifier(),
                        policy=Policy(types={"statement": STATEMENT}), expander=Expander())
    report = mailroom.run()
    for outcome in report.outcomes:
        reason = f" ({outcome.reason})" if outcome.reason else ""
        print(f"{outcome.kind.value:<7} {outcome.path}{reason}")
    print("drive:", sorted(drive.tree()))
    ledger.close()


if __name__ == "__main__":
    main()
