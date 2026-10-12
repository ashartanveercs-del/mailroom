import json
import urllib.parse

import pytest

from mailroom.google.multipart import build_related, parse_related, safe_mime
from mailroom.google.policy import (
    DRIVE,
    FOLDER_MIME,
    GMAIL,
    TOKEN_URI,
    UPLOAD,
    drive_query,
    google_allowlist,
)
from mailroom.guard import BlockedRequest
from mailroom.http import Request

MANAGED = {"root", "folder-1"}
allow = google_allowlist(lambda folder_id: folder_id in MANAGED)


def check(method, url, body=None, headers=None):
    return allow.check(Request(method, url, headers=headers or {}, body=body)).name


def upload(meta, data=b"%PDF-1.4", mime="application/pdf"):
    body, content_type = build_related(meta, data, mime)
    return body, {"Content-Type": content_type}


GOOD_META = {"id": "abc123", "name": "a.pdf", "mimeType": "application/pdf",
             "parents": ["folder-1"], "description": "Filed by mailroom"}
UPLOAD_URL = UPLOAD + "?uploadType=multipart&fields=id"


# --- what is allowed -----------------------------------------------------------------------

@pytest.mark.parametrize("method,url,name", [
    ("GET", GMAIL + "/messages?q=has%3Aattachment&maxResults=100", "gmail.messages.list"),
    ("GET", GMAIL + "/messages?q=x&maxResults=100&pageToken=0987654321", "gmail.messages.list"),
    ("GET", GMAIL + "/messages/18c2f0a1b2?format=full", "gmail.messages.get"),
    ("GET", GMAIL + "/messages/18c2f0a1b2/attachments/ANGjdJ_x-1", "gmail.attachments.get"),
    ("GET", GMAIL + "/messages/18c2f0a1b2/attachments/ANGjdJ" + "8Lw_-Qz" * 60,
     "gmail.attachments.get"),
    ("GET", DRIVE + "/files/generateIds?count=1&space=drive&type=files",
     "drive.files.generateIds"),
    ("GET", DRIVE + "/files/abc123?fields=id%2Cname%2Cparents", "drive.files.get"),
])
def test_the_read_requests_the_adapters_make_are_allowed(method, url, name):
    assert check(method, url) == name


def test_a_listing_query_in_the_exact_shape_the_adapter_builds_is_allowed():
    q = drive_query("folder-1", "O'Brien \\ Sons", folders_only=True)
    url = DRIVE + "/files?" + urllib.parse.urlencode(
        {"q": q, "fields": "nextPageToken,files(id,name)", "pageSize": "100", "spaces": "drive"})
    assert check("GET", url) == "drive.files.list"


def test_uploads_with_a_reserved_id_into_a_managed_folder_are_allowed():
    body, headers = upload(GOOD_META)
    assert check("POST", UPLOAD_URL, body, headers) == "drive.files.upload"


def test_folder_creates_with_a_reserved_id_are_allowed():
    meta = {"id": "f1", "name": "Invoices", "mimeType": FOLDER_MIME, "parents": ["root"]}
    assert check("POST", DRIVE + "/files?fields=id", json.dumps(meta).encode(),
                 {"Content-Type": "application/json"}) == "drive.folders.create"


def test_token_refresh_is_allowed():
    body = urllib.parse.urlencode({"grant_type": "refresh_token", "client_id": "c",
                                   "client_secret": "s", "refresh_token": "r"}).encode()
    assert check("POST", TOKEN_URI, body) == "oauth.token.refresh"


# --- what is not ---------------------------------------------------------------------------

@pytest.mark.parametrize("method,url", [
    ("POST", GMAIL + "/messages/send"),
    ("POST", GMAIL + "/messages/abc/modify"),
    ("POST", GMAIL + "/messages/abc/trash"),
    ("DELETE", GMAIL + "/messages/abc"),
    ("POST", GMAIL + "/messages/batchModify"),
    ("POST", GMAIL + "/drafts"),
    ("POST", GMAIL + "/labels"),
    ("POST", "https://gmail.googleapis.com/batch/gmail/v1"),
    ("GET", GMAIL + "/messages/abc?format=raw"),
    ("GET", GMAIL + "/messages/abc"),
    ("GET", GMAIL + "/messages?q=x&includeSpamTrash=true"),
    ("GET", "https://www.googleapis.com/gmail/v1/users/me/messages"),
    ("PATCH", DRIVE + "/files/abc"),
    ("PUT", DRIVE + "/files/abc"),
    ("DELETE", DRIVE + "/files/abc"),
    ("POST", DRIVE + "/files/abc/copy"),
    ("POST", DRIVE + "/files/abc/permissions"),
    ("GET", DRIVE + "/files/abc?alt=media"),
    ("PATCH", UPLOAD + "/abc?uploadType=multipart"),
    ("POST", UPLOAD + "?uploadType=resumable"),
    ("POST", UPLOAD + "?uploadType=media"),
    ("GET", DRIVE + "/files?q=name+contains+%27x%27"),
    ("GET", DRIVE + "/files?q=%27a%27+in+parents+or+%27b%27+in+parents"),
    ("GET", DRIVE + "/files/generateIds?count=500&space=drive&type=files"),
    ("GET", DRIVE + "/files/a%2Fb"),
])
def test_everything_else_is_blocked(method, url):
    with pytest.raises(BlockedRequest):
        check(method, url, b"{}")


@pytest.mark.parametrize("change,reason", [
    ({"id": None}, "reserved id"),
    ({"parents": ["folder-1", "root"]}, "exactly one parent"),
    ({"parents": ["someone-elses-folder"]}, "managed"),
    ({"parents": ["root"], "mimeType": FOLDER_MIME}, "folder"),
    ({"mimeType": "application/vnd.google-apps.document"}, "Google"),
    ({"mimeType": "application/vnd.google-apps.spreadsheet"}, "Google"),
    ({"mimeType": "Application/Vnd.Google-Apps.Script"}, "Google"),
    ({"writersCanShare": True}, "metadata"),
    ({"name": ""}, "name"),
])
def test_uploads_that_break_the_rules_are_blocked(change, reason):
    meta = {k: v for k, v in {**GOOD_META, **change}.items() if v is not None}
    body, headers = upload(meta)
    with pytest.raises(BlockedRequest, match=reason):
        check("POST", UPLOAD_URL, body, headers)


def test_an_upload_body_that_is_not_two_well_formed_parts_is_blocked():
    with pytest.raises(BlockedRequest, match="multipart"):
        check("POST", UPLOAD_URL, b"just bytes", {"Content-Type": "application/pdf"})


def test_folder_creates_must_be_folders_with_reserved_ids_in_managed_folders():
    for meta in ({"id": "f1", "name": "x", "mimeType": "application/pdf", "parents": ["root"]},
                 {"name": "x", "mimeType": FOLDER_MIME, "parents": ["root"]},
                 {"id": "f1", "name": "x", "mimeType": FOLDER_MIME, "parents": ["elsewhere"]}):
        with pytest.raises(BlockedRequest):
            check("POST", DRIVE + "/files", json.dumps(meta).encode())


def test_token_requests_other_than_a_refresh_are_blocked():
    body = urllib.parse.urlencode({"grant_type": "authorization_code", "code": "x"}).encode()
    with pytest.raises(BlockedRequest):
        check("POST", TOKEN_URI, body)


# --- multipart -----------------------------------------------------------------------------

def test_multipart_round_trip_and_boundary_never_occurs_in_the_data():
    data = b"--abc\r\n" * 100
    body, content_type = build_related({"id": "x"}, data, "application/pdf")
    [(meta_headers, meta), (media_headers, media)] = parse_related(body, content_type)
    assert json.loads(meta) == {"id": "x"} and media == data
    assert meta_headers["content-type"].startswith("application/json")
    assert media_headers["content-type"] == "application/pdf"


def test_a_sender_supplied_mime_type_cannot_inject_headers_into_the_upload():
    body, content_type = build_related({"id": "x"}, b"data", "application/pdf\r\nX-Evil: 1")
    [_, (media_headers, _)] = parse_related(body, content_type)
    assert media_headers == {"content-type": "application/octet-stream"}


@pytest.mark.parametrize("mime", ["application/vnd.google-apps.folder",
                                  "application/vnd.google-apps.document", "text/plain\r\nX: 1"])
def test_safe_mime_never_passes_a_google_type_or_a_malformed_one(mime):
    assert safe_mime(mime) == "application/octet-stream"


def test_drive_query_escapes_quotes_and_backslashes():
    assert drive_query("p1", "O'Brien \\ Sons") == (
        "'p1' in parents and name = 'O\\'Brien \\\\ Sons' and trashed = false")
