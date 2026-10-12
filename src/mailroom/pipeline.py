"""One run: recover, read new mail, decide every document, file it.

For each message the mailbox lists (oldest first), and not yet finished in the ledger:

  1. ignore it if it carries SENT/DRAFT/SPAM/TRASH, or comes from a sender not allowed
  2. expand it into documents (containers opened, batch PDFs split)
  3. for each document, unless the ledger already has its final outcome:
       duplicate bytes?          -> duplicate (never filed twice)
       classify, cross-check, ground, threshold (routing.decide)
       same type+issuer+number as a filed document with other bytes -> review
       plan the decision in the ledger, then file it write-ahead (filing.Filer)
  4. mark the message finished only when every document reached a final outcome

Preview runs the same code against a throwaway copy of the ledger with a filer that never
writes, so what preview reports is what a live run would do.
"""

from __future__ import annotations

import fnmatch
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from email.utils import parseaddr
from typing import Any

from .classify import Classifier, ClassifierError, RuleClassifier
from .documents import Expander
from .filing import DryRunFiler, Filer, FilingConflict, Recovery
from .http import HttpError, TransportError
from .ledger import Ledger
from .models import TERMINAL, Classification, Document, Message, Outcome, OutcomeKind
from .ports import Drive, DriveError, Mailbox
from .routing import Policy, decide, identity_of, review_name

DEFAULT_QUERY = "has:attachment -in:sent -in:drafts -in:spam -in:trash"
NEVER_LABELS = ("SENT", "DRAFT", "SPAM", "TRASH")
TRANSIENT = (TransportError, HttpError, DriveError)


@dataclass
class RunReport:
    preview: bool
    recovery: Recovery
    outcomes: list[Outcome] = field(default_factory=list)
    messages_seen: int = 0
    messages_finished: int = 0
    messages_ignored: list[tuple[str, str]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        return dict(Counter(o.kind.value for o in self.outcomes))

    def to_dict(self) -> dict[str, Any]:
        return {
            "preview": self.preview,
            "counts": self.counts(),
            "messages": {"seen": self.messages_seen, "finished": self.messages_finished,
                         "ignored": [list(pair) for pair in self.messages_ignored]},
            "recovery": vars(self.recovery),
            "errors": self.errors,
            "outcomes": [{"key": o.key, "origin": o.origin, "kind": o.kind.value,
                          "reason": o.reason, "path": o.path, "doc_type": o.doc_type,
                          "confidence": o.confidence, "file_id": o.file_id}
                         for o in self.outcomes],
        }


class Mailroom:
    def __init__(self, *, mailbox: Mailbox, drive: Drive, ledger: Ledger,
                 classifier: Classifier, policy: Policy, expander: Expander | None = None,
                 rules: RuleClassifier | None = None, query: str = DEFAULT_QUERY,
                 allowed_senders: Sequence[str] = (), max_attempts: int = 3,
                 max_upload_bytes: int | None = None) -> None:
        self.mailbox = mailbox
        self.drive = drive
        self.ledger = ledger
        self.classifier = classifier
        self.policy = policy
        self.expander = expander or Expander()
        self.rules = rules
        self.query = query
        self.allowed_senders = [p.casefold() for p in allowed_senders]
        self.max_attempts = max_attempts
        self.max_upload_bytes = max_upload_bytes

    def recover(self) -> Recovery:
        return Filer(self.drive, self.ledger).recover()

    def run(self, *, preview: bool = False) -> RunReport:
        ledger = self.ledger.snapshot() if preview else self.ledger
        filer = DryRunFiler(self.drive, ledger) if preview else Filer(self.drive, ledger)
        report = RunReport(preview=preview, recovery=filer.recover())
        try:
            for message_id in self.mailbox.list_message_ids(self.query):
                if ledger.message_done(message_id):
                    continue
                report.messages_seen += 1
                try:
                    message = self.mailbox.get_message(message_id)
                except TRANSIENT as err:
                    report.errors.append(f"{message_id}: {err}")
                    continue
                reason = self._ignore_reason(message)
                if reason:
                    ledger.log("message_ignored", message_id=message_id, reason=reason)
                    ledger.mark_message_done(message_id)
                    report.messages_ignored.append((message_id, reason))
                    continue
                outcomes = self._message(message, ledger, filer, preview)
                report.outcomes += outcomes
                if all(o.kind in TERMINAL for o in outcomes):
                    ledger.mark_message_done(message_id)
                    report.messages_finished += 1
        finally:
            if preview:
                ledger.close()
        return report

    def _ignore_reason(self, message: Message) -> str | None:
        for label in NEVER_LABELS:
            if label in message.labels:
                return f"label:{label}"
        if self.allowed_senders:
            address = parseaddr(message.sender)[1].casefold()
            if not any(fnmatch.fnmatchcase(address, p) for p in self.allowed_senders):
                return "sender_not_allowed"
        return None

    def _message(self, message: Message, ledger: Ledger, filer: Filer,
                 preview: bool) -> list[Outcome]:
        docs, skipped = self.expander.documents(message)
        outcomes = []
        for origin, reason in skipped:
            outcome = Outcome(key=f"{message.id}/{origin}", message_id=message.id,
                              origin=origin, kind=OutcomeKind.SKIPPED, reason=reason,
                              dry_run=preview)
            ledger.record(outcome)
            outcomes.append(outcome)
        for doc in docs:
            outcomes.append(self._document(doc, ledger, filer, preview))
        return outcomes

    def _document(self, doc: Document, ledger: Ledger, filer: Filer,
                  preview: bool) -> Outcome:
        final = ledger.outcome(doc.key)
        if final is not None and final.kind in TERMINAL:
            return final
        plan = ledger.planned(doc.key)
        if plan is None:
            plan, identity = self._decide(doc, ledger, preview)
            if plan.kind not in (OutcomeKind.FILED, OutcomeKind.REVIEW):
                ledger.record(plan)
                return plan
            ledger.plan(plan, identity)
        description = f"Filed by mailroom from {doc.origin}"
        if plan.kind is OutcomeKind.REVIEW:
            description += f" - needs review: {plan.reason}"
        try:
            folder_id = filer.folder(plan.folder)
            filed = filer.file(doc.key, doc.data, plan.name, folder_id, doc.mime_type,
                               description)
        except FilingConflict as err:
            outcome = replace(plan, kind=OutcomeKind.CONFLICT, reason=str(err))
            ledger.record(outcome)
            return outcome
        except TRANSIENT:
            # keep the plan: the next run finishes this exact decision
            return replace(plan, kind=OutcomeKind.DEFERRED, reason="drive_unavailable")
        outcome = replace(plan, name=filed.name, file_id=filed.file_id, dry_run=preview)
        ledger.record(outcome)
        return outcome

    def _decide(self, doc: Document, ledger: Ledger,
                preview: bool) -> tuple[Outcome, str | None]:
        base = Outcome(key=doc.key, message_id=doc.message_id, origin=doc.origin,
                       kind=OutcomeKind.REVIEW, sha256=doc.sha256, dry_run=preview)
        earlier = ledger.handled_with_sha(doc.sha256)
        if earlier is not None and earlier.key != doc.key:
            return replace(base, kind=OutcomeKind.DUPLICATE,
                           reason=f"same content as {earlier.key}"), None
        if self.max_upload_bytes is not None and len(doc.data) > self.max_upload_bytes:
            return replace(base, kind=OutcomeKind.SKIPPED, reason="too_large"), None
        if doc.flags:
            verdict = Classification("other", 0.0, source="none")
        else:
            try:
                verdict = self.classifier.classify(doc)
            except ClassifierError:
                if ledger.bump_attempts(doc.key) < self.max_attempts:
                    return replace(base, kind=OutcomeKind.DEFERRED,
                                   reason="classifier_unavailable"), None
                return replace(base, reason="classifier_unavailable",
                               folder=(self.policy.review_folder,),
                               name=review_name(doc)), None
        opinion = None
        if self.rules is not None:
            # an image has no text of its own: hold the model's label to its own reading
            seen = doc if doc.text.strip() else replace(doc, text=verdict.transcript)
            opinion = self.rules.assess(seen)
        dest = decide(doc, verdict, opinion, self.policy)
        identity = None
        if not dest.review:
            identity = identity_of(verdict)
            earlier = ledger.filed_with_identity(identity) if identity else None
            if earlier is not None and earlier.key != doc.key:
                return replace(base, reason="possible_revision", doc_type=verdict.doc_type,
                               confidence=verdict.confidence,
                               folder=(self.policy.review_folder,),
                               name=review_name(doc)), None
        return replace(base, kind=OutcomeKind.REVIEW if dest.review else OutcomeKind.FILED,
                       reason=dest.reason, folder=dest.folder, name=dest.name,
                       doc_type=verdict.doc_type, confidence=verdict.confidence), identity
