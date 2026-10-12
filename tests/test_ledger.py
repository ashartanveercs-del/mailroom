import pytest

from mailroom.ledger import Ledger, NameTaken, UnmanagedFolder
from mailroom.models import Outcome, OutcomeKind

ROOT = "root-1"


@pytest.fixture
def ledger():
    led = Ledger(":memory:", root_id=ROOT)
    yield led
    led.close()


def test_write_ahead_row_survives_a_restart(tmp_path):
    path = tmp_path / "ledger.db"
    first = Ledger(path, root_id=ROOT)
    first.begin_write("f1", "file", ROOT, "a.pdf", doc_key="m1/a.pdf", md5="m", sha256="s",
                      size=3)
    first.close()                                   # the process dies here

    second = Ledger(path, root_id=ROOT)
    [row] = second.pending_writes()
    assert (row.file_id, row.status, row.name, row.md5, row.sha256, row.size, row.doc_key) == (
        "f1", "intent", "a.pdf", "m", "s", 3, "m1/a.pdf")
    second.finish_write("f1")
    assert second.pending_writes() == []
    assert second.write("f1").status == "done"
    second.close()


def test_a_name_is_reserved_once_per_folder_until_rolled_back(ledger):
    ledger.begin_write("f1", "file", ROOT, "a.pdf")
    with pytest.raises(NameTaken):
        ledger.begin_write("f2", "file", ROOT, "a.pdf")
    ledger.roll_back_write("f1", "never landed")
    ledger.begin_write("f3", "file", ROOT, "a.pdf")
    assert ledger.reserved_names(ROOT) == {"a.pdf"}


def test_writes_only_go_under_the_root_or_folders_mailroom_made_there(ledger):
    with pytest.raises(UnmanagedFolder):
        ledger.begin_write("f1", "file", "someone-elses-folder", "a.pdf")
    ledger.begin_write("d1", "folder", ROOT, "Invoices")
    assert not ledger.managed_folder("d1")           # not until it is confirmed
    ledger.finish_write("d1")
    assert ledger.managed_folder("d1")
    assert ledger.folder_for(ROOT, "Invoices") == "d1"
    ledger.begin_write("f2", "file", "d1", "a.pdf")


def test_adopted_folders_are_managed(ledger):
    ledger.adopt_folder("d9", ROOT, "Receipts")
    assert ledger.managed_folder("d9")
    assert ledger.folder_for(ROOT, "Receipts") == "d9"
    with pytest.raises(UnmanagedFolder):
        ledger.adopt_folder("d10", "not-managed", "X")


def test_write_for_doc_returns_the_live_attempt(ledger):
    ledger.begin_write("f1", "file", ROOT, "a.pdf", doc_key="k")
    ledger.roll_back_write("f1", "never landed")
    assert ledger.write_for_doc("k") is None
    ledger.begin_write("f2", "file", ROOT, "a.pdf", doc_key="k")
    assert ledger.write_for_doc("k").file_id == "f2"


def test_conflicts_and_orphans_are_recorded(ledger):
    ledger.begin_write("f1", "file", ROOT, "a.pdf", doc_key="k")
    ledger.mark_conflict("f1", "md5 mismatch")
    assert ledger.write("f1").status == "conflict"
    assert ledger.reserved_names(ROOT) == {"a.pdf"}  # the name stays taken on the drive
    ledger.begin_write("f2", "file", ROOT, "b.pdf", doc_key="j")
    ledger.roll_back_write("f2", "never landed")
    assert [r.file_id for r in ledger.rolled_back_writes()] == ["f2"]
    ledger.mark_orphan("f2")
    assert ledger.write("f2").status == "orphan"
    assert ledger.reserved_names(ROOT) == {"a.pdf", "b.pdf"}


def outcome(key, kind=OutcomeKind.FILED, sha="s1", **kw):
    return Outcome(key=key, message_id="m1", origin=key, kind=kind, sha256=sha, **kw)


def test_outcomes_round_trip(ledger):
    out = outcome("m1/a.pdf", folder=("Invoices", "2026"), name="a.pdf", file_id="f1",
                  doc_type="invoice", confidence=0.93, reason="")
    ledger.record(out, identity="invoice|acme|inv-1")
    assert ledger.outcome("m1/a.pdf") == out


def test_a_planned_decision_is_not_an_outcome_until_it_is_recorded(ledger):
    plan = outcome("m1/a.pdf", folder=("Invoices",), name="a.pdf", doc_type="invoice")
    ledger.plan(plan, identity="invoice|acme|1")
    assert ledger.outcome("m1/a.pdf") is None
    assert ledger.planned("m1/a.pdf") == plan
    # a plan already counts for duplicate and revision checks
    assert ledger.handled_with_sha("s1").key == "m1/a.pdf"
    assert ledger.filed_with_identity("invoice|acme|1").key == "m1/a.pdf"
    final = outcome("m1/a.pdf", folder=("Invoices",), name="a_2.pdf", file_id="f1")
    ledger.record(final, identity="invoice|acme|1")
    assert ledger.outcome("m1/a.pdf") == final
    assert ledger.planned("m1/a.pdf") is None


def test_duplicate_lookup_only_considers_documents_that_were_actually_handled(ledger):
    ledger.record(outcome("k1", OutcomeKind.DEFERRED, sha="s"))
    ledger.record(outcome("k2", OutcomeKind.SKIPPED, sha="s"))
    assert ledger.handled_with_sha("s") is None
    ledger.record(outcome("k3", OutcomeKind.REVIEW, sha="s"))
    assert ledger.handled_with_sha("s").key == "k3"


def test_identity_lookup_finds_an_earlier_filing_with_different_content(ledger):
    ledger.record(outcome("k1", sha="s1"), identity="invoice|acme|inv-1")
    found = ledger.filed_with_identity("invoice|acme|inv-1")
    assert found.key == "k1" and found.sha256 == "s1"
    assert ledger.filed_with_identity("invoice|acme|inv-2") is None


def test_messages_and_attempts(ledger):
    assert not ledger.message_done("m1")
    ledger.mark_message_done("m1")
    assert ledger.message_done("m1")
    assert ledger.bump_attempts("k") == 1
    assert ledger.bump_attempts("k") == 2


def test_events_form_an_audit_trail(ledger):
    ledger.log("filed", key="k", file_id="f1")
    assert ledger.events() == [("filed", {"key": "k", "file_id": "f1"})]


def test_snapshot_is_an_independent_copy(ledger):
    ledger.mark_message_done("m1")
    copy = ledger.snapshot()
    copy.mark_message_done("m2")
    copy.begin_write("f1", "file", ROOT, "a.pdf")
    assert copy.message_done("m1") and copy.message_done("m2")
    assert not ledger.message_done("m2")
    assert ledger.reserved_names(ROOT) == set()
