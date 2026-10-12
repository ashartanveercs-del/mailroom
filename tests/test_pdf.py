import io

import pypdf
import pytest

from mailroom.pdf import PdfUnreadable, cut, page_texts
from mailroom.sim.pdfgen import make_pdf


def test_generated_pdf_has_one_page_per_entry_and_readable_text():
    data = make_pdf([["INVOICE", "Invoice No: INV-1001"], ["Page 2 of 2", "Total due: $40.00"]])
    assert data.startswith(b"%PDF-")
    assert len(pypdf.PdfReader(io.BytesIO(data)).pages) == 2
    texts = page_texts(data)
    assert "INV-1001" in texts[0]
    assert "Total due: $40.00" in texts[1]


def test_generated_pdf_escapes_pdf_string_syntax():
    data = make_pdf([["Paid (in full) \\ thanks"]])
    assert "Paid (in full) \\ thanks" in page_texts(data)[0]


def test_generated_pdf_is_deterministic():
    pages = [["Receipt R-77", "Thank you"]]
    assert make_pdf(pages) == make_pdf(pages)


def test_cut_produces_one_pdf_per_page_range():
    data = make_pdf([["doc A p1"], ["doc A p2"], ["doc B p1"]])
    parts = cut(data, [(1, 2), (3, 3)])
    assert [len(page_texts(p)) for p in parts] == [2, 1]
    assert "doc B p1" in page_texts(parts[1])[0]


def test_cut_is_deterministic_so_part_hashes_are_stable_across_runs():
    data = make_pdf([["one"], ["two"]])
    assert cut(data, [(1, 1), (2, 2)]) == cut(data, [(1, 1), (2, 2)])


def test_cut_rejects_ranges_outside_the_document():
    data = make_pdf([["one"], ["two"]])
    with pytest.raises(ValueError):
        cut(data, [(2, 3)])


def test_garbage_is_unreadable():
    with pytest.raises(PdfUnreadable):
        page_texts(b"%PDF-1.4 this is not really a pdf")


def test_encrypted_pdf_is_reported_as_encrypted():
    writer = pypdf.PdfWriter(clone_from=pypdf.PdfReader(io.BytesIO(make_pdf([["secret"]]))))
    writer.encrypt(user_password="pw", owner_password="pw", algorithm="RC4-128")
    buf = io.BytesIO()
    writer.write(buf)
    with pytest.raises(PdfUnreadable) as err:
        page_texts(buf.getvalue())
    assert err.value.encrypted
