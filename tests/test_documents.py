from datetime import UTC, datetime

from mailroom.documents import Expander, html_text
from mailroom.models import Attachment, Message
from mailroom.pdf import page_texts
from mailroom.sim.files import make_docx, make_png, make_zip
from mailroom.sim.pdfgen import make_pdf
from mailroom.split import NumberSplitter

SPLITTER = NumberSplitter([r"Invoice No:\s*([A-Z0-9-]+)"])


def message(*attachments, skipped=()):
    return Message(id="m1", sender="Billing <billing@example.com>", subject="Docs",
                   received_at=datetime(2026, 3, 2, tzinfo=UTC),
                   attachments=tuple(attachments), skipped=tuple(skipped))


def pdf_att(name, *pages):
    return Attachment(name, "application/pdf", make_pdf([list(p) for p in pages]))


def test_a_pdf_attachment_becomes_one_document_with_its_text():
    docs, skipped = Expander().documents(message(pdf_att("inv.pdf", ["Invoice No: INV-1"])))
    assert [(d.key, d.origin, d.kind) for d in docs] == [("m1/inv.pdf", "inv.pdf", "pdf")]
    assert "INV-1" in docs[0].text
    assert skipped == []


def test_two_attachments_with_the_same_name_get_distinct_keys():
    docs, _ = Expander().documents(message(pdf_att("scan.pdf", ["a"]), pdf_att("scan.pdf", ["b"])))
    assert [d.key for d in docs] == ["m1/scan.pdf", "m1/scan.pdf#2"]


def test_zip_members_become_documents():
    zipped = make_zip({"r1.pdf": make_pdf([["Receipt one"]]), "r2.pdf": make_pdf([["Two"]])})
    docs, _ = Expander().documents(message(Attachment("r.zip", "application/zip", zipped)))
    assert [d.key for d in docs] == ["m1/r.zip/r1.pdf", "m1/r.zip/r2.pdf"]


def test_confident_multi_document_pdf_is_split_into_parts():
    att = pdf_att("batch.pdf", ["Invoice No: A-1"], ["page two of A"], ["Invoice No: B-2"])
    docs, _ = Expander(splitter=SPLITTER).documents(message(att))
    assert [d.key for d in docs] == ["m1/batch.pdf#p1-2", "m1/batch.pdf#p3"]
    assert [d.origin for d in docs] == ["batch.pdf [p1-2]", "batch.pdf [p3]"]
    assert [d.filename for d in docs] == ["batch (p1-2).pdf", "batch (p3).pdf"]
    assert len(page_texts(docs[0].data)) == 2
    assert "B-2" in docs[1].text and "A-1" not in docs[1].text


def test_unsure_multi_document_pdf_stays_whole_and_is_flagged():
    att = pdf_att("batch.pdf", ["cover sheet"], ["Invoice No: A-1"], ["Invoice No: B-2"])
    docs, _ = Expander(splitter=SPLITTER).documents(message(att))
    assert [(d.key, d.flags) for d in docs] == [
        ("m1/batch.pdf", ("possible_multiple_documents",))]


def test_without_a_splitter_nothing_is_split():
    att = pdf_att("batch.pdf", ["Invoice No: A-1"], ["Invoice No: B-2"])
    docs, _ = Expander(splitter=None).documents(message(att))
    assert [d.key for d in docs] == ["m1/batch.pdf"]


def test_unreadable_and_encrypted_pdfs_are_flagged_not_dropped():
    att = Attachment("broken.pdf", "application/pdf", b"%PDF-1.4 garbage")
    docs, _ = Expander().documents(message(att))
    assert [d.flags for d in docs] == [("unreadable_pdf",)]


def test_flags_from_containers_carry_over():
    att = Attachment("locked.zip", "application/zip", make_zip({"a.pdf": b"x"}, encrypt=True))
    docs, _ = Expander().documents(message(att))
    assert [(d.key, d.flags) for d in docs] == [("m1/locked.zip", ("encrypted_archive",))]


def test_skips_from_unpacking_and_from_the_mailbox_are_reported():
    logo = Attachment("logo.png", "image/png", make_png(), inline=True)
    docs, skipped = Expander().documents(message(logo, skipped=[("huge.pdf", "too_large")]))
    assert docs == []
    assert skipped == [("huge.pdf", "too_large"), ("logo.png", "inline_image")]


def test_text_comes_from_docx_html_and_plain_text():
    docs, _ = Expander().documents(message(
        Attachment("c.docx", "", make_docx(["Service Agreement"])),
        Attachment("p.html", "text/html",
                   b"<html><style>x{}</style><script>evil()</script><p>Paid &amp; done</p>"),
        Attachment("n.txt", "text/plain", "Receipt café".encode()),
        Attachment("photo.png", "image/png", make_png(padding=40_000)),
    ))
    assert [d.text for d in docs] == ["Service Agreement", "Paid & done", "Receipt café", ""]
    assert [d.kind for d in docs] == ["docx", "html", "text", "image"]


def test_html_text_drops_scripts_styles_and_keeps_link_words_only():
    assert html_text('<p>Total <a href="https://tracker.example.com/x">here</a></p>') == (
        "Total here")
