import hashlib

import pytest

from mailroom.filing import DryRunFiler, Filer, FilingConflict
from mailroom.ledger import Ledger, UnmanagedFolder
from mailroom.ports import AlreadyExists, DriveError
from mailroom.sim.faults import CrashPlan, SimulatedCrash
from mailroom.sim.memory import InMemoryDrive

ROOT = "root"
PDF = "application/pdf"


def md5(data):
    return hashlib.md5(data).hexdigest()


@pytest.fixture
def world(tmp_path):
    drive = InMemoryDrive(ROOT)
    path = tmp_path / "ledger.db"
    ledger = Ledger(path, root_id=ROOT)
    yield drive, ledger, path
    ledger.close()


def restart(ledger, path):
    """Simulate the process dying and starting again: a fresh ledger over the same file."""
    ledger.close()
    return Ledger(path, root_id=ROOT)


# --- the in-memory drive fake behaves like the real thing where it matters -------------------

def test_fake_drive_refuses_a_second_create_with_the_same_id():
    drive = InMemoryDrive(ROOT)
    [fid] = drive.reserve_ids(1)
    drive.create_file(fid, "a.pdf", ROOT, b"1", PDF)
    with pytest.raises(AlreadyExists):
        drive.create_file(fid, "b.pdf", ROOT, b"2", PDF)
    assert drive.data(fid) == b"1"


def test_fake_drive_requires_an_existing_live_parent_folder():
    drive = InMemoryDrive(ROOT)
    with pytest.raises(DriveError):
        drive.create_file("x", "a.pdf", "nope", b"1", PDF)
    folder = drive.add_folder("Old", ROOT)
    drive.trash(folder)
    with pytest.raises(DriveError):
        drive.create_file("y", "a.pdf", folder, b"1", PDF)


# --- filing --------------------------------------------------------------------------------

def test_files_into_a_new_folder_path_and_verifies_the_stored_copy(world):
    drive, ledger, _ = world
    filer = Filer(drive, ledger)
    folder = filer.folder(("Invoices", "2026"))
    filed = filer.file("m1/a.pdf", b"invoice bytes", "a.pdf", folder, PDF)
    assert drive.tree() == {"Invoices/2026/a.pdf": md5(b"invoice bytes")}
    assert filed.md5 == md5(b"invoice bytes") and filed.name == "a.pdf"
    assert ledger.write(filed.file_id).status == "done"
    assert ledger.pending_writes() == []


def test_filing_the_same_document_again_is_a_no_op(world):
    drive, ledger, _ = world
    filer = Filer(drive, ledger)
    first = filer.file("m1/a.pdf", b"x", "a.pdf", ROOT, PDF)
    writes = drive.writes
    again = filer.file("m1/a.pdf", b"x", "a.pdf", ROOT, PDF)
    assert again == first and drive.writes == writes


def test_an_existing_file_is_never_overwritten(world):
    drive, ledger, _ = world
    theirs = drive.add_file("a.pdf", ROOT, b"the owner's own file")
    filed = Filer(drive, ledger).file("m1/a.pdf", b"new", "a.pdf", ROOT, PDF)
    assert filed.name == "a_2.pdf"
    assert drive.data(theirs) == b"the owner's own file"
    assert drive.tree() == {"a.pdf": md5(b"the owner's own file"), "a_2.pdf": md5(b"new")}


def test_names_reserved_by_unfinished_writes_are_avoided_too(world):
    drive, ledger, _ = world
    ledger.begin_write("pending", "file", ROOT, "a.pdf", doc_key="other")
    filed = Filer(drive, ledger).file("m1/a.pdf", b"new", "a.pdf", ROOT, PDF)
    assert filed.name == "a_2.pdf"


def test_an_existing_folder_with_the_same_name_is_adopted_not_duplicated(world):
    drive, ledger, _ = world
    theirs = drive.add_folder("Invoices", ROOT)
    assert Filer(drive, ledger).folder(("Invoices",)) == theirs
    assert ledger.managed_folder(theirs)
    assert drive.writes == 0


def test_a_trashed_folder_is_replaced_not_written_into(world):
    drive, ledger, _ = world
    filer = Filer(drive, ledger)
    old = filer.folder(("Invoices",))
    drive.trash(old)
    new = Filer(drive, ledger).folder(("Invoices",))
    assert new != old
    Filer(drive, ledger).file("m1/a.pdf", b"x", "a.pdf", new, PDF)
    assert drive.tree() == {"Invoices/a.pdf": md5(b"x")}


def test_the_filer_refuses_folders_outside_the_managed_tree(world):
    drive, ledger, _ = world
    elsewhere = drive.add_folder("Not ours", None)
    with pytest.raises(UnmanagedFolder):
        Filer(drive, ledger).file("m1/a.pdf", b"x", "a.pdf", elsewhere, PDF)
    assert drive.writes == 0


# --- crashes -------------------------------------------------------------------------------

def crash_on_first_upload(drive, when):
    drive.crash = CrashPlan(at=0, when=when)


def test_crash_before_the_upload_lands_then_recovery_rolls_the_intent_back(world):
    drive, ledger, path = world
    crash_on_first_upload(drive, "before")
    with pytest.raises(SimulatedCrash):
        Filer(drive, ledger).file("m1/a.pdf", b"x", "a.pdf", ROOT, PDF)
    assert len(ledger.pending_writes()) == 1 and drive.tree() == {}

    ledger = restart(ledger, path)
    drive.crash = None
    report = Filer(drive, ledger).recover()
    assert len(report.rolled_back) == 1 and report.completed == []
    # the next run files the document normally, exactly once
    Filer(drive, ledger).file("m1/a.pdf", b"x", "a.pdf", ROOT, PDF)
    assert drive.tree() == {"a.pdf": md5(b"x")}
    ledger.close()


def test_crash_before_the_upload_lands_then_retry_resumes_with_the_same_id(world):
    drive, ledger, path = world
    crash_on_first_upload(drive, "before")
    with pytest.raises(SimulatedCrash):
        Filer(drive, ledger).file("m1/a.pdf", b"x", "a.pdf", ROOT, PDF)
    [intent] = ledger.pending_writes()

    ledger = restart(ledger, path)
    drive.crash = None
    filed = Filer(drive, ledger).file("m1/a.pdf", b"x", "a.pdf", ROOT, PDF)
    assert filed.file_id == intent.file_id
    assert drive.tree() == {"a.pdf": md5(b"x")}
    ledger.close()


def test_crash_after_the_upload_landed_then_recovery_rolls_forward(world):
    drive, ledger, path = world
    crash_on_first_upload(drive, "after")
    with pytest.raises(SimulatedCrash):
        Filer(drive, ledger).file("m1/a.pdf", b"x", "a.pdf", ROOT, PDF)
    assert drive.tree() == {"a.pdf": md5(b"x")}         # landed, but the run never knew

    ledger = restart(ledger, path)
    drive.crash = None
    report = Filer(drive, ledger).recover()
    assert len(report.completed) == 1 and report.rolled_back == []
    writes = drive.writes
    Filer(drive, ledger).file("m1/a.pdf", b"x", "a.pdf", ROOT, PDF)  # the re-run
    assert drive.writes == writes and drive.tree() == {"a.pdf": md5(b"x")}
    ledger.close()


def test_crash_after_the_upload_landed_then_retry_finds_the_copy_instead_of_uploading(world):
    drive, ledger, path = world
    crash_on_first_upload(drive, "after")
    with pytest.raises(SimulatedCrash):
        Filer(drive, ledger).file("m1/a.pdf", b"x", "a.pdf", ROOT, PDF)
    ledger = restart(ledger, path)
    drive.crash = None
    writes = drive.writes
    Filer(drive, ledger).file("m1/a.pdf", b"x", "a.pdf", ROOT, PDF)
    assert drive.writes == writes and drive.tree() == {"a.pdf": md5(b"x")}
    ledger.close()


def test_crash_while_creating_a_folder_never_leaves_two_folders(world):
    drive, ledger, path = world
    drive.crash = CrashPlan(at=0, when="after")
    with pytest.raises(SimulatedCrash):
        Filer(drive, ledger).folder(("Invoices",))
    ledger = restart(ledger, path)
    drive.crash = None
    report = Filer(drive, ledger).recover()
    assert len(report.completed) == 1
    Filer(drive, ledger).folder(("Invoices",))
    assert drive.folder_paths() == ["Invoices"]
    ledger.close()


def test_recovery_tolerates_a_resent_folder_create_whose_first_reply_was_lost(world):
    drive, ledger, path = world
    drive.crash = CrashPlan(at=0, when="before")
    with pytest.raises(SimulatedCrash):
        Filer(drive, ledger).folder(("Invoices",))
    [intent] = ledger.pending_writes()
    ledger = restart(ledger, path)
    drive.crash = None
    real = drive.create_folder

    def landed_but_reply_lost(folder_id, name, parent_id):
        real(folder_id, name, parent_id)
        raise AlreadyExists(folder_id)  # what the retry of a lost reply reports

    drive.create_folder = landed_but_reply_lost
    report = Filer(drive, ledger).recover()
    assert report.resumed == [intent.file_id]
    assert drive.folder_paths() == ["Invoices"]
    ledger.close()


def test_a_stored_copy_that_does_not_match_its_hash_is_a_conflict_never_overwritten(world):
    drive, ledger, path = world
    crash_on_first_upload(drive, "after")
    with pytest.raises(SimulatedCrash):
        Filer(drive, ledger).file("m1/a.pdf", b"x", "a.pdf", ROOT, PDF)
    [intent] = ledger.pending_writes()
    drive.tamper(intent.file_id, b"corrupted")
    ledger = restart(ledger, path)
    drive.crash = None
    report = Filer(drive, ledger).recover()
    assert report.conflicts == [intent.file_id]
    with pytest.raises(FilingConflict):
        Filer(drive, ledger).file("m1/a.pdf", b"x", "a.pdf", ROOT, PDF)
    assert drive.data(intent.file_id) == b"corrupted"
    ledger.close()


def test_a_copy_that_lands_after_its_intent_was_rolled_back_is_reported_as_an_orphan(world):
    drive, ledger, _ = world
    crash_on_first_upload(drive, "before")
    with pytest.raises(SimulatedCrash):
        Filer(drive, ledger).file("m1/a.pdf", b"x", "a.pdf", ROOT, PDF)
    [intent] = ledger.pending_writes()
    drive.crash = None
    Filer(drive, ledger).recover()                      # rolled back: nothing had landed
    drive.create_file(intent.file_id, "a.pdf", ROOT, b"x", PDF)  # a delayed request arrives
    report = Filer(drive, ledger).recover()
    assert report.orphans == [intent.file_id]
    assert ledger.write(intent.file_id).status == "orphan"


# --- preview -------------------------------------------------------------------------------

def test_dry_run_filer_decides_names_and_folders_without_writing(world):
    drive, ledger, _ = world
    drive.add_file("a.pdf", drive.add_folder("Invoices", ROOT), b"theirs")
    preview = DryRunFiler(drive, ledger.snapshot())
    folder = preview.folder(("Invoices",))
    first = preview.file("m1/a.pdf", b"x", "a.pdf", folder, PDF)
    second = preview.file("m2/a.pdf", b"y", "a.pdf", folder, PDF)
    new_folder = preview.folder(("Receipts", "2026"))
    third = preview.file("m3/r.pdf", b"z", "r.pdf", new_folder, PDF)
    assert (first.name, second.name, third.name) == ("a_2.pdf", "a_3.pdf", "r.pdf")
    assert first.file_id is None
    assert drive.writes == 0 and ledger.reserved_names(ROOT) == set()
