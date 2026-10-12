from datetime import UTC, datetime, timedelta
from email.message import EmailMessage

import pytest

from mailroom.app import google_stack
from mailroom.google.gmail import GmailMailbox
from mailroom.guard import BlockedRequest, UnsafePageToken
from mailroom.http import Request
from mailroom.models import Attachment, Message
from mailroom.sim.files import make_png, make_zip
from mailroom.sim.google_api import SimulatedGoogle
from mailroom.sim.pdfgen import make_pdf

T0 = datetime(2026, 3, 1, 9, 0, tzinfo=UTC)
PDF = make_pdf([["Invoice No: INV-1"]])


def message(mid, *attachments, minutes=0, labels=("INBOX",)):
    return Message(id=mid, sender="Accounts <accounts@example.com>", subject=f"Docs {mid}",
                   received_at=T0 + timedelta(minutes=minutes), labels=frozenset(labels),
                   attachments=tuple(attachments))


def build(messages, **options):
    sim = SimulatedGoogle(messages, **options)
    transport, guarded = google_stack(sim, lambda folder_id: folder_id == sim.root_id,
                                      credentials=sim.credentials, sleep=lambda s: None)
    return sim, guarded, GmailMailbox(transport, page_size=2)


def test_listing_pages_through_everything_and_returns_oldest_first():
    msgs = [message(f"m{i}", Attachment("a.pdf", "application/pdf", PDF), minutes=i)
            for i in range(5)]
    sim, _, mailbox = build(msgs)
    assert mailbox.list_message_ids("has:attachment") == ["m0", "m1", "m2", "m3", "m4"]
    lists = [url for method, url in sim.requests if "/messages?" in url]
    assert len(lists) == 3 and "pageToken=" in lists[1]


def test_a_next_page_token_that_is_really_a_url_is_never_followed():
    msgs = [message(f"m{i}", minutes=i) for i in range(3)]
    sim, _, mailbox = build(msgs, next_page_token=lambda page: "https://attacker.example.com/p2")
    with pytest.raises(UnsafePageToken):
        mailbox.list_message_ids("")
    assert [url for _, url in sim.requests if "/messages?" in url] == [
        "https://gmail.googleapis.com/gmail/v1/users/me/messages?q=&maxResults=2"]
    assert not any("attacker" in url for _, url in sim.requests)


def test_a_next_page_token_that_repeats_stops_the_listing():
    msgs = [message(f"m{i}", minutes=i) for i in range(5)]
    _, _, mailbox = build(msgs, next_page_token=lambda page: "same-token")
    with pytest.raises(UnsafePageToken, match="repeated"):
        mailbox.list_message_ids("")


def test_a_message_round_trips_with_its_attachments():
    png = make_png(padding=50_000)
    inner = EmailMessage()
    inner["From"] = "vendor@example.com"
    inner["Subject"] = "Original"
    inner.set_content("see attached")
    inner.add_attachment(PDF, maintype="application", subtype="pdf", filename="inner.pdf")
    original = message("m1",
                       Attachment("invoice.pdf", "application/pdf", PDF),
                       Attachment("photo.png", "image/png", png),
                       Attachment("logo.png", "image/png", make_png(), inline=True),
                       Attachment("bundle.zip", "application/zip", make_zip({"r.pdf": PDF})),
                       Attachment("fwd.eml", "message/rfc822", inner.as_bytes()),
                       labels=("INBOX", "IMPORTANT"))
    _, _, mailbox = build([original])
    got = mailbox.get_message("m1")
    assert (got.id, got.sender, got.subject, got.received_at, got.labels) == (
        "m1", original.sender, original.subject, original.received_at, original.labels)
    assert [(a.filename, a.mime_type, a.inline) for a in got.attachments] == [
        ("invoice.pdf", "application/pdf", False), ("photo.png", "image/png", False),
        ("logo.png", "image/png", True), ("bundle.zip", "application/zip", False),
        ("fwd.eml", "message/rfc822", False)]
    assert [a.data for a in got.attachments[:4]] == [a.data for a in original.attachments[:4]]
    assert b"inner.pdf" in got.attachments[4].data


def test_attachment_ids_as_long_as_the_ones_gmail_issues_are_fetched():
    sim, _, mailbox = build([message("m1", Attachment("a.pdf", "application/pdf", PDF))])
    [att] = mailbox.get_message("m1").attachments
    assert att.data == PDF
    [url] = [u for _, u in sim.requests if "/attachments/" in u]
    assert len(url.rsplit("/", 1)[1]) > 256


def test_a_listing_longer_than_the_page_cap_keeps_the_newest_messages_and_says_so():
    msgs = [message(f"m{i:02d}", minutes=i) for i in range(30)]
    _, _, mailbox = build(msgs)
    mailbox.max_pages = 10                                 # 10 pages of 2: 20 of the 30
    assert mailbox.list_message_ids("") == [f"m{i:02d}" for i in range(10, 30)]
    assert mailbox.listing_truncated
    mailbox.max_pages = 15
    assert len(mailbox.list_message_ids("")) == 30 and not mailbox.listing_truncated


def test_oversized_attachments_are_skipped_without_being_downloaded():
    sim, _, mailbox = build([message("m1", Attachment("big.pdf", "application/pdf", PDF))])
    mailbox.max_attachment_bytes = 100
    got = mailbox.get_message("m1")
    assert got.attachments == () and got.skipped == (("big.pdf", "too_large"),)
    assert not any("/attachments/" in url for _, url in sim.requests)


def test_intake_only_ever_reads():
    msgs = [message("m1", Attachment("a.pdf", "application/pdf", PDF))]
    sim, guarded, mailbox = build(msgs)
    mailbox.get_message(mailbox.list_message_ids("")[0])
    gmail_requests = [(m, u) for m, u in sim.requests if "gmail.googleapis.com" in u]
    assert gmail_requests and all(method == "GET" for method, _ in gmail_requests)
    # and the guard refuses a write even if some code tried one
    with pytest.raises(BlockedRequest):
        guarded.send(Request("POST", "https://gmail.googleapis.com/gmail/v1/users/me/"
                                     "messages/m1/modify", body=b"{}"))
    assert sim.unexpected == []


def test_the_query_excludes_sent_and_spam_on_the_server_side_too():
    msgs = [message("m1"), message("m2", labels=("SENT",)), message("m3", labels=("SPAM",))]
    _, _, mailbox = build(msgs)
    assert mailbox.list_message_ids("-in:sent -in:spam") == ["m1"]
