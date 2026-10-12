import hashlib

import pytest

from mailroom.app import google_stack
from mailroom.filing import Filer
from mailroom.google.drive import GoogleDrive
from mailroom.guard import BlockedRequest
from mailroom.ledger import Ledger
from mailroom.ports import AlreadyExists, DriveError
from mailroom.sim.faults import CrashPlan, SimulatedCrash
from mailroom.sim.google_api import Faults, SimulatedGoogle

PDF = "application/pdf"


def md5(data):
    return hashlib.md5(data).hexdigest()


@pytest.fixture
def setup():
    sim = SimulatedGoogle()
    ledger = Ledger(":memory:", root_id=sim.root_id)
    transport, _ = google_stack(sim, ledger.managed_folder, credentials=sim.credentials,
                                sleep=lambda s: None)
    yield sim, ledger, GoogleDrive(transport, page_size=2)
    ledger.close()


def test_create_and_read_back(setup):
    sim, _, drive = setup
    [fid] = drive.reserve_ids(1)
    stored = drive.create_file(fid, "a.pdf", sim.root_id, b"bytes", PDF, "a note")
    assert (stored.id, stored.name, stored.parents, stored.md5, stored.size) == (
        fid, "a.pdf", (sim.root_id,), md5(b"bytes"), 5)
    assert drive.get(fid) == stored
    assert sim.drive.data(fid) == b"bytes" and sim.drive.description(fid) == "a note"
    assert drive.get("does-not-exist") is None


def test_a_second_create_with_the_same_id_is_already_exists(setup):
    sim, _, drive = setup
    [fid] = drive.reserve_ids(1)
    drive.create_file(fid, "a.pdf", sim.root_id, b"1", PDF)
    with pytest.raises(AlreadyExists):
        drive.create_file(fid, "a.pdf", sim.root_id, b"2", PDF)


def test_a_missing_parent_is_a_drive_error(setup):
    sim, ledger, drive = setup
    ledger.adopt_folder("gone", sim.root_id, "Gone")  # managed, but not on the drive
    with pytest.raises(DriveError):
        drive.create_file(drive.reserve_ids(1)[0], "a.pdf", "gone", b"1", PDF)


def test_find_children_pages_and_escapes_names(setup):
    sim, _, drive = setup
    for name in ["O'Brien \\ Sons", "x' or name contains '", "other", "O'Brien \\ Sons"]:
        sim.drive.add_file(name, sim.root_id, name.encode())
    found = drive.find_children(sim.root_id, "O'Brien \\ Sons")
    assert [f.name for f in found] == ["O'Brien \\ Sons"] * 2
    assert drive.find_children(sim.root_id, "x' or name contains '")[0].name == (
        "x' or name contains '")
    folder = sim.drive.add_folder("Invoices", sim.root_id)
    assert [f.id for f in drive.find_children(sim.root_id, "Invoices", folders_only=True)] == [
        folder]


def test_trash_state_is_reported(setup):
    sim, _, drive = setup
    fid = sim.drive.add_file("a.pdf", sim.root_id, b"1")
    sim.drive.trash(fid)
    assert drive.get(fid).trashed


def test_the_guard_blocks_uploads_outside_the_managed_tree_even_without_the_filer(setup):
    sim, _, drive = setup
    elsewhere = sim.drive.add_folder("Someone else's", None)
    with pytest.raises(BlockedRequest, match="not managed"):
        drive.create_file(drive.reserve_ids(1)[0], "a.pdf", elsewhere, b"1", PDF)
    assert sim.drive.writes == 0


def test_filing_end_to_end_over_the_rest_api(setup):
    sim, ledger, drive = setup
    filer = Filer(drive, ledger)
    folder = filer.folder(("Invoices", "2026"))
    filed = filer.file("m1/a.pdf", b"invoice", "a.pdf", folder, PDF)
    again = Filer(drive, ledger).file("m1/a.pdf", b"invoice", "a.pdf", folder, PDF)
    assert again == filed
    assert sim.drive.tree() == {"Invoices/2026/a.pdf": md5(b"invoice")}


def test_lost_responses_and_throttling_are_absorbed_without_duplicates(setup):
    sim, ledger, drive = setup
    sim.faults = Faults(transient_every=3, lose_write_response_every=1)
    filer = Filer(drive, ledger)
    folder = filer.folder(("Receipts",))
    for i in range(3):
        filer.file(f"m{i}/r.pdf", f"receipt {i}".encode(), "r.pdf", folder, PDF)
    assert sim.drive.tree() == {"Receipts/r.pdf": md5(b"receipt 0"),
                                "Receipts/r_2.pdf": md5(b"receipt 1"),
                                "Receipts/r_3.pdf": md5(b"receipt 2")}
    assert sim.faults.injected > 0


def test_an_expired_access_token_is_refreshed_transparently(setup):
    sim, _, drive = setup
    drive.reserve_ids(1)
    sim.expire_tokens()
    drive.reserve_ids(1)
    assert sim.tokens_issued == 2


def test_a_crash_inside_the_api_call_propagates_like_a_dead_process(setup):
    sim, ledger, drive = setup
    sim.drive.crash = CrashPlan(at=0, when="after")
    with pytest.raises(SimulatedCrash):
        Filer(drive, ledger).file("m1/a.pdf", b"x", "a.pdf", sim.root_id, PDF)
    sim.drive.crash = None
    report = Filer(drive, ledger).recover()
    assert len(report.completed) == 1
    assert sim.drive.tree() == {"a.pdf": md5(b"x")}
