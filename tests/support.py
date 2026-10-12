"""Test builders: an offline mailroom over in-memory fakes and a scripted model."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from mailroom.classify import LLMClassifier, RuleClassifier, llm_allowlist
from mailroom.defaults import DEFAULT_TYPES, number_patterns
from mailroom.documents import Expander
from mailroom.guard import GuardedTransport
from mailroom.http import RetryingTransport
from mailroom.ledger import Ledger
from mailroom.models import Attachment, Message
from mailroom.pipeline import Mailroom
from mailroom.routing import Policy
from mailroom.sim.memory import InMemoryDrive, InMemoryMailbox
from mailroom.sim.model import SimulatedModel
from mailroom.sim.pdfgen import make_pdf
from mailroom.split import NumberSplitter

ENDPOINT = "https://llm.example.com/v1/chat/completions"
VENDORS = ("Blue Fern Hosting", "Inkwell & Co. Printing", "Northgate Office Supply")
T0 = datetime(2026, 3, 1, 9, 0, tzinfo=UTC)


def pdf(*pages):
    return make_pdf([p.splitlines() for p in pages])


def msg(mid, *attachments, minutes=0, sender="Accounts <accounts@example.com>", labels=()):
    atts = tuple(a if isinstance(a, Attachment) else Attachment(a[0], "application/pdf", a[1])
                 for a in attachments)
    return Message(id=mid, sender=sender, subject=f"Documents {mid}",
                   received_at=T0 + timedelta(minutes=minutes), labels=frozenset(labels),
                   attachments=atts)


class World:
    def __init__(self, messages, script=(), *, ledger_path=":memory:", drive=None, **options):
        self.mailbox = InMemoryMailbox(messages)
        self.drive = drive or InMemoryDrive("root")
        self.model = SimulatedModel(script)
        self.ledger_path = ledger_path
        self.ledger = Ledger(ledger_path, root_id="root")
        self.options = options
        self.mailroom = self.build()

    def build(self) -> Mailroom:
        types = list(DEFAULT_TYPES.values())
        transport = RetryingTransport(GuardedTransport(self.model, llm_allowlist(ENDPOINT)),
                                      sleep=lambda s: None)
        classifier = LLMClassifier(transport, endpoint=ENDPOINT, model="test-model",
                                   api_key="sk-test", types=types)
        policy = Policy(types=DEFAULT_TYPES, **self.options.pop("policy", {}))
        return Mailroom(
            mailbox=self.mailbox, drive=self.drive, ledger=self.ledger,
            classifier=classifier, policy=policy,
            expander=Expander(splitter=NumberSplitter(number_patterns(DEFAULT_TYPES))),
            rules=RuleClassifier(types, vendors=VENDORS), **self.options)

    def restart(self) -> Mailroom:
        """The process died: reopen the same ledger file and build a fresh mailroom."""
        self.ledger.close()
        self.ledger = Ledger(self.ledger_path, root_id="root")
        self.mailroom = self.build()
        return self.mailroom
