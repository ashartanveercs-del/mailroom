import hashlib

import pytest

from mailroom.documents import Expander
from mailroom.google.auth import AuthError
from mailroom.guard import BlockedRequest
from mailroom.http import HttpError, TransportError
from mailroom.models import Attachment, OutcomeKind
from mailroom.ports import DriveError
from mailroom.sim.faults import CrashPlan, SimulatedCrash
from mailroom.sim.files import make_png, make_zip
from mailroom.sim.memory import InMemoryDrive, InMemoryMailbox
from mailroom.sim.model import Scripted, SimulatedModel
from tests.support import msg, pdf

INVOICE_TEXT = """Blue Fern Hosting
INVOICE
Invoice No: BF-2041
Invoice date: 03/14/2026
Bill to: Larkspur Design Studio
Amount due: $120.00"""
BLUE_FERN = pdf(INVOICE_TEXT)
BF_REPLY = {"doc_type": "invoice", "confidence": 0.95,
            "fields": {"vendor": "Blue Fern Hosting", "number": "BF-2041",
                       "date": "2026-03-14"}}
INVOICE_PATH = "Invoices/2026/Blue Fern Hosting/2026-03-14 Blue Fern Hosting BF-2041.pdf"

RECEIPT = pdf("""Inkwell & Co. Printing
RECEIPT
Receipt No: Q-881
Date: March 20, 2026
Amount paid: $45.00
Thank you for your purchase""")
RECEIPT_REPLY = {"doc_type": "receipt", "confidence": 0.92,
                 "fields": {"vendor": "Inkwell & Co. Printing", "date": "2026-03-20"}}
RECEIPT_PATH = "Receipts/2026-03/2026-03-20 Inkwell & Co. Printing receipt.pdf"

NORTHGATE_TEXT = """Northgate Office Supply
INVOICE
Invoice No: NG-7710
Invoice date: 03/02/2026
Amount due: $310.50"""
NG_REPLY = {"doc_type": "invoice", "confidence": 0.9,
            "fields": {"vendor": "Northgate Office Supply", "number": "NG-7710",
                       "date": "2026-03-02"}}
NG_PATH = "Invoices/2026/Northgate Office Supply/2026-03-02 Northgate Office Supply NG-7710.pdf"

SCRIPT = [Scripted("BF-2041", BF_REPLY), Scripted("Q-881", RECEIPT_REPLY),
          Scripted("NG-7710", NG_REPLY)]


def md5(data):
    return hashlib.md5(data).hexdigest()


def kinds(report):
    return [(o.origin, o.kind.value, o.reason) for o in report.outcomes]


def test_files_a_confident_invoice_end_to_end(world_factory):
    w = world_factory([msg("m1", ("blue-fern.pdf", BLUE_FERN))], SCRIPT)
    report = w.mailroom.run()
    assert w.drive.tree() == {INVOICE_PATH: md5(BLUE_FERN)}
    [out] = report.outcomes
    assert (out.kind, out.path, out.doc_type, out.confidence) == (
        OutcomeKind.FILED, INVOICE_PATH, "invoice", 0.95)
    assert w.ledger.message_done("m1")
    assert report.counts() == {"filed": 1}


def test_low_confidence_goes_to_the_review_folder_with_the_reason(world_factory):
    script = [Scripted("BF-2041", {**BF_REPLY, "confidence": 0.5})]
    w = world_factory([msg("m1", ("blue-fern.pdf", BLUE_FERN))], script)
    [out] = w.mailroom.run().outcomes
    assert (out.kind, out.reason, out.path) == (
        OutcomeKind.REVIEW, "low_confidence", "Needs review/blue-fern.pdf")
    assert w.drive.tree() == {"Needs review/blue-fern.pdf": md5(BLUE_FERN)}
    assert "low_confidence" in w.drive.description(out.file_id)


def test_a_second_run_changes_nothing(world_factory):
    w = world_factory([msg("m1", ("blue-fern.pdf", BLUE_FERN), ("r.pdf", RECEIPT))], SCRIPT)
    w.mailroom.run()
    writes, calls = w.drive.writes, len(w.model.requests)
    again = w.mailroom.run()
    assert again.outcomes == [] and w.drive.writes == writes
    assert len(w.model.requests) == calls


def test_the_same_bytes_in_a_later_message_are_a_duplicate(world_factory):
    w = world_factory([msg("m1", ("a.pdf", BLUE_FERN)),
                       msg("m2", ("a-resent.pdf", BLUE_FERN), minutes=5)], SCRIPT)
    report = w.mailroom.run()
    assert kinds(report) == [("a.pdf", "filed", ""),
                             ("a-resent.pdf", "duplicate", "same content as m1/a.pdf")]
    assert list(w.drive.tree()) == [INVOICE_PATH]


def test_a_corrected_copy_of_a_filed_document_goes_to_review(world_factory):
    corrected = pdf(INVOICE_TEXT.replace("$120.00", "$150.00"))
    w = world_factory([msg("m1", ("a.pdf", BLUE_FERN)),
                       msg("m2", ("a-corrected.pdf", corrected), minutes=5)], SCRIPT)
    report = w.mailroom.run()
    assert kinds(report)[1] == ("a-corrected.pdf", "review", "possible_revision")
    assert w.drive.tree() == {INVOICE_PATH: md5(BLUE_FERN),
                              "Needs review/a-corrected.pdf": md5(corrected)}


@pytest.mark.parametrize("label", ["SENT", "DRAFT", "SPAM", "TRASH"])
def test_sent_drafts_spam_and_trash_are_never_taken_in(world_factory, label):
    # the in-memory mailbox ignores the search query: the label check must hold on its own
    w = world_factory([msg("m1", ("a.pdf", BLUE_FERN), labels=[label])], SCRIPT)
    report = w.mailroom.run()
    assert report.outcomes == [] and w.drive.tree() == {}
    assert report.messages_ignored == [("m1", f"label:{label}")]
    assert w.model.requests == []


def test_only_allowed_senders_are_taken_in(world_factory):
    w = world_factory([msg("m1", ("a.pdf", BLUE_FERN), sender="Bills <bills@example.com>"),
                       msg("m2", ("r.pdf", RECEIPT), sender="x@mail.example.com")],
                      SCRIPT, allowed_senders=("*@example.com",))
    report = w.mailroom.run()
    assert [o.origin for o in report.outcomes] == ["a.pdf"]
    assert report.messages_ignored == [("m2", "sender_not_allowed")]


def test_a_classifier_outage_defers_then_goes_to_review_after_the_attempt_limit(world_factory):
    w = world_factory([msg("m1", ("a.pdf", BLUE_FERN))],
                      [Scripted("BF-2041", BF_REPLY, status=503)], max_attempts=3)
    for _ in range(2):
        assert kinds(w.mailroom.run()) == [("a.pdf", "deferred", "classifier_unavailable")]
        assert not w.ledger.message_done("m1") and w.drive.tree() == {}
    assert kinds(w.mailroom.run()) == [("a.pdf", "review", "classifier_unavailable")]
    assert list(w.drive.tree()) == ["Needs review/a.pdf"]
    assert w.ledger.message_done("m1")


def test_preview_makes_the_same_decisions_without_writing_anything(world_factory):
    messages = [msg("m1", ("a.pdf", BLUE_FERN), ("r.pdf", RECEIPT)),
                msg("m2", ("a-again.pdf", BLUE_FERN), minutes=1)]
    w = world_factory(messages, SCRIPT)
    preview = w.mailroom.run(preview=True)
    assert w.drive.writes == 0 and w.ledger.outcomes() == []
    assert not w.ledger.message_done("m1")
    assert all(o.dry_run and o.file_id is None for o in preview.outcomes)
    live = w.mailroom.run()
    assert [(o.kind, o.path) for o in preview.outcomes] == [(o.kind, o.path)
                                                            for o in live.outcomes]


def test_a_crash_after_an_upload_landed_is_finished_by_the_next_run(world_factory, tmp_path):
    messages = [msg("m1", ("a.pdf", BLUE_FERN)), msg("m2", ("r.pdf", RECEIPT), minutes=1)]
    w = world_factory(messages, SCRIPT, ledger_path=tmp_path / "ledger.db")
    w.drive.crash = CrashPlan(at=3, when="after")  # 3 folders, then the invoice upload
    with pytest.raises(SimulatedCrash):
        w.mailroom.run()
    w.drive.crash = None
    report = w.restart().run()
    assert len(report.recovery.completed) == 1
    assert w.drive.tree() == {INVOICE_PATH: md5(BLUE_FERN), RECEIPT_PATH: md5(RECEIPT)}
    assert w.drive.file_count() == 2


def test_a_decision_made_before_a_crash_is_kept_even_if_the_model_changes_its_mind(
        world_factory, tmp_path):
    w = world_factory([msg("m1", ("a.pdf", BLUE_FERN))], SCRIPT,
                      ledger_path=tmp_path / "ledger.db")
    w.drive.crash = CrashPlan(at=3, when="before")  # decided and planned; upload never sent
    with pytest.raises(SimulatedCrash):
        w.mailroom.run()
    w.drive.crash = None
    w.model.script = [Scripted("BF-2041", {**BF_REPLY, "confidence": 0.3})]
    calls = len(w.model.requests)
    report = w.restart().run()
    assert kinds(report) == [("a.pdf", "filed", "")]
    assert len(w.model.requests) == calls             # the model was not asked again
    assert w.drive.tree() == {INVOICE_PATH: md5(BLUE_FERN)}


def test_things_that_are_not_documents_are_recorded_as_skipped(world_factory):
    logo = Attachment("logo.png", "image/png", make_png(), inline=True)
    w = world_factory([msg("m1", ("a.pdf", BLUE_FERN), logo)], SCRIPT)
    assert kinds(w.mailroom.run()) == [("logo.png", "skipped", "inline_image"),
                                       ("a.pdf", "filed", "")]
    assert w.ledger.message_done("m1")


def test_documents_over_the_upload_limit_are_skipped(world_factory):
    w = world_factory([msg("m1", ("a.pdf", BLUE_FERN))], SCRIPT, max_upload_bytes=100)
    assert kinds(w.mailroom.run()) == [("a.pdf", "skipped", "too_large")]


def test_an_image_whose_label_contradicts_its_own_transcript_goes_to_review(world_factory):
    png = make_png(padding=40_000, seed=11)
    transcript = ("Kestrel Couriers\nRECEIPT\nReceipt No: KC-5730\nDate: 03/26/2026\n"
                  "Amount paid: $9.00\nThank you for your purchase")
    reply = {"doc_type": "invoice", "confidence": 0.97, "transcript": transcript,
             "fields": {"vendor": "Kestrel Couriers", "number": "KC-5730",
                        "date": "2026-03-26"}}
    w = world_factory([msg("m1", Attachment("IMG_7.png", "image/png", png))],
                      [Scripted(SimulatedModel.image_key(png), reply)])
    assert kinds(w.mailroom.run()) == [("IMG_7.png", "review", "classifiers_disagree")]


class FlakyDrive(InMemoryDrive):
    def __init__(self, failures):
        super().__init__("root")
        self.failures = failures

    def create_file(self, *args, **kwargs):
        if self.failures:
            self.failures -= 1
            raise TransportError("connection reset")
        return super().create_file(*args, **kwargs)


def test_a_drive_outage_defers_the_document_and_keeps_its_decision(world_factory):
    w = world_factory([msg("m1", ("a.pdf", BLUE_FERN))], SCRIPT, drive=FlakyDrive(1))
    assert kinds(w.mailroom.run()) == [("a.pdf", "deferred", "drive_unavailable")]
    assert w.ledger.planned("m1/a.pdf") is not None and not w.ledger.message_done("m1")
    assert kinds(w.mailroom.run()) == [("a.pdf", "filed", "")]
    assert w.drive.tree() == {INVOICE_PATH: md5(BLUE_FERN)}


def test_no_document_is_lost_to_a_name_that_looks_like_a_generated_key(world_factory):
    w = world_factory([msg("m1", ("a.pdf", pdf("first document")),
                           ("a.pdf", pdf("second document")),
                           ("a.pdf#2", pdf("Totally different third document")))], SCRIPT)
    report = w.mailroom.run()
    assert len({o.key for o in report.outcomes}) == 3
    assert sorted(w.drive.tree()) == ["Needs review/a.pdf", "Needs review/a_2.pdf",
                                      "Needs review/a_3.pdf"]
    assert w.ledger.message_done("m1")


# --- one bad message or document never holds up the rest ----------------------------------

class CrashingClassifier:
    """The real classifier, except that it blows up on one document, as a buggy plug-in
    classifier would."""

    def __init__(self, inner, needle):
        self.inner, self.needle = inner, needle

    def classify(self, doc):
        if self.needle in doc.text:
            raise KeyError("fields")
        return self.inner.classify(doc)


def test_a_classifier_that_crashes_on_one_document_does_not_hold_up_the_rest(world_factory):
    w = world_factory([msg("m1", ("a.pdf", BLUE_FERN)), msg("m2", ("r.pdf", RECEIPT), minutes=1)],
                      SCRIPT)
    w.mailroom.classifier = CrashingClassifier(w.mailroom.classifier, "BF-2041")
    report = w.mailroom.run()
    assert kinds(report) == [("a.pdf", "deferred", "classifier_unavailable"),
                             ("r.pdf", "filed", "")]
    assert any("m1/a.pdf" in e and "KeyError" in e for e in report.errors)
    w.mailroom.run()
    assert kinds(w.mailroom.run()) == [("a.pdf", "review", "classifier_unavailable")]


class BrokenSplitter:
    def plan(self, page_texts):
        raise RuntimeError("splitter bug")


def test_a_message_that_keeps_failing_is_set_aside_and_never_holds_up_later_mail(world_factory):
    w = world_factory([msg("m1", ("batch.pdf", pdf(INVOICE_TEXT, "page two"))),
                       msg("m2", ("r.pdf", RECEIPT), minutes=1)], SCRIPT)
    w.mailroom.expander = Expander(splitter=BrokenSplitter())
    first = w.mailroom.run()
    assert kinds(first) == [("r.pdf", "filed", "")]
    [error] = first.errors
    assert error.startswith("m1:") and "splitter bug" in error
    assert not w.ledger.message_done("m1")
    w.mailroom.run()
    [aside] = w.mailroom.run().outcomes                  # the third failure in a row
    assert (aside.key, aside.kind) == ("m1", OutcomeKind.CONFLICT)
    assert "splitter bug" in aside.reason
    assert w.ledger.message_done("m1") and w.mailroom.run().outcomes == []


class RefusingDrive(InMemoryDrive):
    """The allowlist refuses one document's upload, as it would a request a bug built."""

    def __init__(self, refused):
        super().__init__("root")
        self.refused = refused

    def create_file(self, file_id, name, *args, **kwargs):
        if self.refused in name:
            raise BlockedRequest("an upload cannot do that")
        return super().create_file(file_id, name, *args, **kwargs)


def test_a_refused_upload_holds_up_only_its_own_document(world_factory):
    w = world_factory([msg("m1", ("a.pdf", BLUE_FERN)), msg("m2", ("r.pdf", RECEIPT), minutes=1)],
                      SCRIPT, drive=RefusingDrive("BF-2041"))
    report = w.mailroom.run()
    assert kinds(report) == [("a.pdf", "deferred", "drive_refused"), ("r.pdf", "filed", "")]
    assert w.ledger.pending_writes() == []      # refused before it was sent: nothing to settle
    w.mailroom.run()
    [out] = w.mailroom.run().outcomes
    assert out.kind is OutcomeKind.CONFLICT and "cannot do that" in out.reason
    assert w.ledger.message_done("m1") and list(w.drive.tree()) == [RECEIPT_PATH]


def test_a_folder_name_held_by_a_conflicted_folder_does_not_stop_the_run(world_factory):
    w = world_factory([msg("m1", ("a.pdf", BLUE_FERN)), msg("m2", ("r.pdf", RECEIPT), minutes=1)],
                      SCRIPT)
    w.ledger.begin_write("x1", "folder", "root", "Invoices")
    w.ledger.mark_conflict("x1", "the stored copy is in the trash")
    assert kinds(w.mailroom.run()) == [("a.pdf", "deferred", "drive_refused"),
                                       ("r.pdf", "filed", "")]


class QuotaDrive(InMemoryDrive):
    def reserve_ids(self, count):
        raise DriveError("storage quota exceeded")


def test_a_filing_the_drive_keeps_refusing_is_reported_and_becomes_a_conflict(world_factory):
    w = world_factory([msg("m1", ("a.pdf", BLUE_FERN))], SCRIPT, drive=QuotaDrive("root"))
    for _ in range(2):
        report = w.mailroom.run()
        assert kinds(report) == [("a.pdf", "deferred", "drive_refused")]
        assert any("storage quota exceeded" in e for e in report.errors)
    [out] = w.mailroom.run().outcomes
    assert out.kind is OutcomeKind.CONFLICT and "storage quota exceeded" in out.reason
    assert w.ledger.message_done("m1")


@pytest.mark.parametrize("make_error", [
    lambda: TransportError("connection reset"),
    lambda: HttpError(503, "backend error"),
    lambda: HttpError(403, "User Rate Limit Exceeded", reason="userRateLimitExceeded"),
], ids=["network", "server", "rate-limit"])
def test_a_drive_that_does_not_answer_is_retried_every_run_and_reported(world_factory,
                                                                       make_error):
    class DownDrive(InMemoryDrive):
        down = True

        def reserve_ids(self, count):
            if self.down:
                raise make_error()
            return super().reserve_ids(count)

    drive = DownDrive("root")
    w = world_factory([msg("m1", ("a.pdf", BLUE_FERN))], SCRIPT, drive=drive)
    for _ in range(5):
        report = w.mailroom.run()
        assert kinds(report) == [("a.pdf", "deferred", "drive_unavailable")]
        assert report.errors
    drive.down = False
    assert kinds(w.mailroom.run()) == [("a.pdf", "filed", "")]


class RevokedMailbox(InMemoryMailbox):
    def get_message(self, message_id):
        raise AuthError("Google refused the refresh token; reconnect the account")


def test_revoked_credentials_stop_the_run_instead_of_failing_message_by_message(world_factory):
    w = world_factory([msg("m1", ("a.pdf", BLUE_FERN))], SCRIPT)
    w.mailroom.mailbox = RevokedMailbox([msg("m1", ("a.pdf", BLUE_FERN))])
    for _ in range(4):
        with pytest.raises(AuthError):
            w.mailroom.run()
    assert not w.ledger.message_done("m1") and w.ledger.outcomes() == []


def test_a_mailbox_listing_cut_short_is_reported(world_factory):
    w = world_factory([msg("m1", ("a.pdf", BLUE_FERN))], SCRIPT)
    w.mailbox.listing_truncated = True
    report = w.mailroom.run()
    assert kinds(report) == [("a.pdf", "filed", "")]
    assert any("mailbox.query" in e for e in report.errors)


def test_documents_inside_zips_and_batch_pdfs_are_each_filed(world_factory):
    batch = pdf(INVOICE_TEXT, NORTHGATE_TEXT)
    bundle = make_zip({"receipts/r.pdf": RECEIPT})
    w = world_factory([msg("m1", ("batch.pdf", batch),
                           Attachment("bundle.zip", "application/zip", bundle))], SCRIPT)
    report = w.mailroom.run()
    assert [(o.origin, o.kind.value) for o in report.outcomes] == [
        ("batch.pdf [p1]", "filed"), ("batch.pdf [p2]", "filed"),
        ("bundle.zip/receipts/r.pdf", "filed")]
    assert sorted(w.drive.tree()) == sorted([INVOICE_PATH, NG_PATH, RECEIPT_PATH])
