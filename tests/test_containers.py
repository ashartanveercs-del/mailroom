import io
import zipfile
from email.message import EmailMessage

import pytest

from mailroom.containers import Limits, docx_text, sniff, unpack
from mailroom.sim.files import make_docx, make_png, make_zip
from mailroom.sim.pdfgen import make_pdf

PDF = make_pdf([["Invoice No: INV-1"]])


# --- sniffing ------------------------------------------------------------------------------

@pytest.mark.parametrize("data,filename,mime,expected", [
    (PDF, "invoice.pdf", "application/pdf", "pdf"),
    (PDF, "photo.jpg", "image/jpeg", "pdf"),            # bytes win over the sender's label
    (make_png(), "scan.png", "image/png", "image"),
    (b"\xff\xd8\xff\xe0" + b"\0" * 20, "x.bin", "", "image"),
    (make_zip({"a.pdf": PDF}), "files.zip", "application/zip", "zip"),
    (make_docx(["Hello"]), "letter.docx", "application/octet-stream", "docx"),
    (make_zip({"[Content_Types].xml": b"<x/>", "xl/workbook.xml": b"<x/>"}), "s.xlsx", "",
     "office"),
    (b"From: a@example.com\r\nSubject: hi\r\n\r\nbody", "fwd.eml", "message/rfc822", "eml"),
    (b"From: a@example.com\r\n\r\nbody", "noname", "message/rfc822", "eml"),
    (b"plain words", "notes.txt", "text/plain", "text"),
    (b"<html><body>Hi</body></html>", "page.html", "text/html", "html"),
    (b"\x00\x01\x02binary", "blob.dat", "application/octet-stream", "binary"),
])
def test_sniff_prefers_content_over_names(data, filename, mime, expected):
    assert sniff(data, filename, mime) == expected


def test_docx_text_reads_paragraphs():
    assert docx_text(make_docx(["Service Agreement", "between two parties"])) == (
        "Service Agreement\nbetween two parties")


# --- plain files ---------------------------------------------------------------------------

def test_a_plain_attachment_is_one_item():
    out = unpack("invoice.pdf", "application/pdf", PDF)
    assert [(i.origin, i.filename, i.kind) for i in out.items] == [
        ("invoice.pdf", "invoice.pdf", "pdf")]
    assert out.skipped == []


@pytest.mark.parametrize("filename,mime,data,reason", [
    ("smime.p7s", "application/pkcs7-signature", b"sig", "signature_file"),
    ("key.asc", "application/pgp-signature", b"sig", "signature_file"),
    ("empty.pdf", "application/pdf", b"", "empty_file"),
])
def test_things_that_are_not_documents_are_skipped_with_a_reason(filename, mime, data, reason):
    out = unpack(filename, mime, data)
    assert out.items == []
    assert out.skipped == [(filename, reason)]


def test_small_inline_images_are_logos_not_documents():
    out = unpack("logo.png", "image/png", make_png(), inline=True)
    assert out.skipped == [("logo.png", "inline_image")]
    big = make_png(padding=40 * 1024)
    assert unpack("photo.png", "image/png", big, inline=True).items  # a pasted photo of a receipt


# --- zip -----------------------------------------------------------------------------------

def test_zip_members_become_items_and_the_archive_itself_is_not_filed():
    data = make_zip({"2026/march.pdf": PDF, "april.pdf": PDF, "__MACOSX/._april.pdf": b"x",
                     ".DS_Store": b"x", "empty-dir/": b""})
    out = unpack("receipts.zip", "application/zip", data)
    assert [(i.origin, i.filename) for i in out.items] == [
        ("receipts.zip/2026/march.pdf", "march.pdf"), ("receipts.zip/april.pdf", "april.pdf")]


def test_zip_member_paths_cannot_escape_or_collide():
    data = make_zip({"../../etc/passwd.pdf": PDF, "a/x.pdf": PDF, "b/x.pdf": PDF})
    origins = [i.origin for i in unpack("in.zip", "application/zip", data).items]
    assert origins == ["in.zip/etc/passwd.pdf", "in.zip/a/x.pdf", "in.zip/b/x.pdf"]
    assert len(set(origins)) == 3


def test_identical_member_paths_get_distinct_origins():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("x.pdf", PDF)
        with pytest.warns(UserWarning):
            zf.writestr("x.pdf", PDF + b" ")
    origins = [i.origin for i in unpack("in.zip", "", buf.getvalue()).items]
    assert origins == ["in.zip/x.pdf", "in.zip/x.pdf#2"]


def test_encrypted_zip_is_kept_whole_and_flagged():
    data = make_zip({"secret.pdf": PDF}, encrypt=True)
    out = unpack("secret.zip", "application/zip", data)
    assert len(out.items) == 1
    assert out.items[0].origin == "secret.zip" and out.items[0].flags == ("encrypted_archive",)


def test_zip_bomb_is_kept_whole_and_flagged_without_inflating_it():
    data = make_zip({"bomb.txt": b"0" * 5_000_000}, compress=True)
    out = unpack("bomb.zip", "application/zip", data, limits=Limits(max_ratio=100))
    assert [i.flags for i in out.items] == [("archive_limits",)]


def test_member_size_limit_uses_the_declared_size():
    data = make_zip({"big.txt": b"x" * 4096}, compress=False)
    out = unpack("a.zip", "", data, limits=Limits(max_member_bytes=1024))
    assert [i.flags for i in out.items] == [("archive_limits",)]


def test_a_header_that_understates_the_size_never_yields_partial_or_oversized_data():
    data = make_zip({"big.txt": b"x" * 4096}, compress=False, declare_size=10)
    out = unpack("a.zip", "", data, limits=Limits(max_member_bytes=1024))
    assert [i.flags for i in out.items] == [("unreadable_archive",)]


def test_too_many_members_is_flagged():
    data = make_zip({f"f{i}.txt": b"x" for i in range(5)})
    out = unpack("many.zip", "", data, limits=Limits(max_members=4))
    assert [i.flags for i in out.items] == [("archive_limits",)]


def test_corrupt_zip_is_flagged():
    data = make_zip({"a.pdf": PDF})[:40]
    out = unpack("broken.zip", "application/zip", data)
    assert [i.flags for i in out.items] == [("unreadable_archive",)]


def test_containers_nest_only_to_the_depth_limit():
    inner = make_zip({"deep.pdf": PDF})
    middle = make_zip({"inner.zip": inner, "shallow.pdf": PDF})
    outer = make_zip({"middle.zip": middle})
    out = unpack("outer.zip", "", outer, limits=Limits(max_depth=2))
    assert [(i.origin, i.flags) for i in out.items] == [
        ("outer.zip/middle.zip/inner.zip", ("archive_too_deep",)),
        ("outer.zip/middle.zip/shallow.pdf", ()),
    ]


def test_office_files_are_documents_not_archives():
    out = unpack("contract.docx", "", make_docx(["Agreement"]))
    assert [(i.kind, i.origin) for i in out.items] == [("docx", "contract.docx")]


# --- attached emails -----------------------------------------------------------------------

def _eml(attachments, nested=None):
    msg = EmailMessage()
    msg["From"] = "vendor@example.com"
    msg["Subject"] = "Fwd: invoice"
    msg.set_content("see attached")
    for name, data, maintype, subtype in attachments:
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=name)
    if nested is not None:
        msg.add_attachment(nested)
    return msg


def test_attached_email_yields_its_attachments():
    eml = _eml([("inv.pdf", PDF, "application", "pdf"), ("note.txt", b"hi", "text", "plain")])
    out = unpack("fwd.eml", "message/rfc822", eml.as_bytes())
    assert [(i.origin, i.kind) for i in out.items] == [
        ("fwd.eml/inv.pdf", "pdf"), ("fwd.eml/note.txt", "text")]


def test_email_attached_inside_an_email_is_opened_within_the_depth_limit():
    inner = _eml([("deep.pdf", PDF, "application", "pdf")])
    outer = _eml([], nested=inner)
    out = unpack("outer.eml", "message/rfc822", outer.as_bytes(), limits=Limits(max_depth=2))
    assert [i.origin for i in out.items] == ["outer.eml/attached-1.eml/deep.pdf"]


def test_attached_email_without_attachments_is_skipped():
    out = unpack("chat.eml", "message/rfc822", _eml([]).as_bytes())
    assert out.items == []
    assert out.skipped == [("chat.eml", "no_attachments")]


def test_zip_inside_attached_email():
    eml = _eml([("bundle.zip", make_zip({"r1.pdf": PDF}), "application", "zip")])
    out = unpack("fwd.eml", "message/rfc822", eml.as_bytes())
    assert [i.origin for i in out.items] == ["fwd.eml/bundle.zip/r1.pdf"]
