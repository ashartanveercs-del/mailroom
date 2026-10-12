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

A failure is charged to the document or message it happened in, never to the run. Messages
are taken oldest first, so a run that stopped at a message that keeps failing would stop at
that same message every time and nothing behind it would ever be filed.

  * the classifier fails (an outage, a malformed reply, a bug in a plug-in classifier): the
    document is deferred, and goes to review after `max_attempts` runs
  * the drive does not answer (network, throttling, a server error): the document is
    deferred and retried every run for as long as that takes, and every run reports it
  * the drive or the allowlist refuses the filing (a 4xx, a full drive, a refused request, a
    folder name held by a conflict): deferred, then a conflict for a person after
    `max_failures` runs
  * anything else goes wrong while handling a message: the message is tried again next run,
    and set aside as a conflict after `max_failures` runs

Only what is no one message's failure stops a run: recovery or the mailbox listing failing, a
`RunCannotContinue` (credentials refused) from anywhere, or a broken ledger.

Preview runs the same code against a throwaway copy of the ledger with a filer that never
writes, so what preview reports is what a live run would do.
"""

from __future__ import annotations

import fnmatch
import sqlite3
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from email.utils import parseaddr
from typing import Any

from .classify import Classifier, RuleClassifier
from .documents import Expander
from .filing import DryRunFiler, Filer, FilingConflict, Recovery
from .guard import BlockedRequest
from .http import RETRY_STATUSES, HttpError, TransportError
from .ledger import Ledger, NameTaken, UnmanagedFolder
from .models import TERMINAL, Classification, Document, Message, Outcome, OutcomeKind
from .ports import Drive, DriveError, Mailbox, RunCannotContinue
from .routing import Policy, decide, identity_of, review_name

DEFAULT_QUERY = "has:attachment -in:sent -in:drafts -in:spam -in:trash"
NEVER_LABELS = ("SENT", "DRAFT", "SPAM", "TRASH")
SERVICE_ERRORS = (TransportError, HttpError, DriveError)
FILING_ERRORS = (*SERVICE_ERRORS, BlockedRequest, UnmanagedFolder, NameTaken)
RATE_LIMIT_REASONS = frozenset({"rateLimitExceeded", "userRateLimitExceeded"})
MAX_DETAIL = 300


def is_transient(err: Exception) -> bool:
    """Worth retrying unchanged, run after run: the network, throttling, a server error. A
    refusal (a 4xx, a full drive, a request the allowlist refused) gets the same answer
    until something changes."""
    if isinstance(err, TransportError):
        return True
    if isinstance(err, HttpError):
        return err.status in RETRY_STATUSES or err.reason in RATE_LIMIT_REASONS
    return False


def _detail(err: BaseException) -> str:
    return f"{type(err).__name__}: {err}"[:MAX_DETAIL]


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
                 max_failures: int = 3, max_upload_bytes: int | None = None) -> None:
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
        self.max_failures = max_failures
        self.max_upload_bytes = max_upload_bytes

    def recover(self) -> Recovery:
        return Filer(self.drive, self.ledger).recover()

    def run(self, *, preview: bool = False) -> RunReport:
        ledger = self.ledger.snapshot() if preview else self.ledger
        filer = DryRunFiler(self.drive, ledger) if preview else Filer(self.drive, ledger)
        report = RunReport(preview=preview, recovery=filer.recover())
        try:
            message_ids = self.mailbox.list_message_ids(self.query)
            if getattr(self.mailbox, "listing_truncated", False):
                report.errors.append(
                    "the mailbox listing was cut short at its page limit, so only the newest "
                    "messages were read: narrow mailbox.query (for example newer_than:30d)")
            for message_id in message_ids:
                if ledger.message_done(message_id):
                    continue
                report.messages_seen += 1
                try:
                    self._take(message_id, ledger, filer, report)
                except (RunCannotContinue, sqlite3.Error):
                    raise
                except Exception as err:  # charged to this message, never to the run
                    self._message_failed(message_id, err, ledger, report)
        finally:
            if preview:
                ledger.close()
        return report

    def _take(self, message_id: str, ledger: Ledger, filer: Filer, report: RunReport) -> None:
        try:
            message = self.mailbox.get_message(message_id)
        except SERVICE_ERRORS as err:
            if not is_transient(err):
                raise
            report.errors.append(f"{message_id}: {err}")
            return
        reason = self._ignore_reason(message)
        if reason:
            ledger.log("message_ignored", message_id=message_id, reason=reason)
            ledger.mark_message_done(message_id)
            report.messages_ignored.append((message_id, reason))
            return
        if self._message(message, ledger, filer, report):
            ledger.mark_message_done(message_id)
            report.messages_finished += 1

    def _message_failed(self, message_id: str, err: Exception, ledger: Ledger,
                        report: RunReport) -> None:
        detail = _detail(err)
        count = ledger.record_failure("message", message_id, detail)
        if count < self.max_failures:
            report.errors.append(f"{message_id}: {detail} (failed {count} of "
                                 f"{self.max_failures} runs)")
            return
        outcome = Outcome(key=message_id, message_id=message_id, origin="(whole message)",
                          kind=OutcomeKind.CONFLICT, dry_run=report.preview,
                          reason=f"set aside after {count} failed runs: {detail}")
        ledger.record(outcome)
        ledger.log("message_set_aside", message_id=message_id, error=detail)
        ledger.mark_message_done(message_id)
        report.outcomes.append(outcome)

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
                 report: RunReport) -> bool:
        """Handle every part of the message; True if every one reached a final outcome."""
        docs, skipped = self.expander.documents(message)
        finished = True
        for origin, reason in skipped:
            outcome = Outcome(key=f"{message.id}/{origin}", message_id=message.id,
                              origin=origin, kind=OutcomeKind.SKIPPED, reason=reason,
                              dry_run=report.preview)
            ledger.record(outcome)
            report.outcomes.append(outcome)
        for doc in docs:
            outcome = self._document(doc, ledger, filer, report)
            report.outcomes.append(outcome)
            finished = finished and outcome.kind in TERMINAL
        return finished

    def _document(self, doc: Document, ledger: Ledger, filer: Filer,
                  report: RunReport) -> Outcome:
        final = ledger.outcome(doc.key)
        if final is not None and final.kind in TERMINAL:
            return final
        plan = ledger.planned(doc.key)
        if plan is None:
            plan, identity = self._decide(doc, ledger, report)
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
        except FILING_ERRORS as err:
            return self._filing_failed(plan, err, ledger, report)
        outcome = replace(plan, name=filed.name, file_id=filed.file_id, dry_run=report.preview)
        ledger.record(outcome)
        return outcome

    def _filing_failed(self, plan: Outcome, err: Exception, ledger: Ledger,
                       report: RunReport) -> Outcome:
        """Keep the plan (a later run finishes this exact decision) unless the filing has been
        refused `max_failures` runs in a row: then it is a conflict for a person."""
        detail = _detail(err)
        if is_transient(err):
            report.errors.append(f"{plan.key}: not filed yet, the drive did not answer: {detail}")
            return replace(plan, kind=OutcomeKind.DEFERRED, reason="drive_unavailable")
        count = ledger.record_failure("filing", plan.key, detail)
        if count < self.max_failures:
            report.errors.append(f"{plan.key}: the filing was refused ({count} of "
                                 f"{self.max_failures} runs): {detail}")
            return replace(plan, kind=OutcomeKind.DEFERRED, reason="drive_refused")
        outcome = replace(plan, kind=OutcomeKind.CONFLICT,
                          reason=f"filing refused {count} runs in a row: {detail}")
        ledger.record(outcome)
        ledger.log("filing_given_up", doc_key=plan.key, error=detail)
        return outcome

    def _decide(self, doc: Document, ledger: Ledger,
                report: RunReport) -> tuple[Outcome, str | None]:
        base = Outcome(key=doc.key, message_id=doc.message_id, origin=doc.origin,
                       kind=OutcomeKind.REVIEW, sha256=doc.sha256, dry_run=report.preview)
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
            except RunCannotContinue:
                raise
            except Exception as err:  # an outage, a malformed reply, a plug-in's bug
                if ledger.bump_attempts(doc.key) < self.max_attempts:
                    report.errors.append(f"{doc.key}: the classifier failed: {_detail(err)}")
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
