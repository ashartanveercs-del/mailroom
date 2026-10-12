"""Everything mailroom may ask of Google, as an allowlist. Nothing else can be sent.

Gmail: list messages, read one message, read one attachment. That is all: no send, modify,
label, trash, delete, draft or batch endpoint exists in this table, so intake is read-only by
construction.

Drive: reserve ids, read a file's metadata, list a folder's children by exact name (the query
must have the one shape the adapter builds), create a folder, and upload a new file. There is
no update, move, copy, permission or delete endpoint, so an existing file can never be
changed. Creates must carry a reserved id (which is what makes retries safe) and exactly one
parent that mailroom manages (the configured root or a folder it made or adopted there).
"""

from __future__ import annotations

import json
import re
import urllib.parse
from collections.abc import Callable

from ..guard import Allowlist, BlockedRequest, Rule
from ..http import Request
from ..ports import FOLDER_MIME
from .multipart import parse_related

GMAIL = "https://gmail.googleapis.com/gmail/v1/users/me"
DRIVE = "https://www.googleapis.com/drive/v3"
UPLOAD = "https://www.googleapis.com/upload/drive/v3/files"
TOKEN_URI = "https://oauth2.googleapis.com/token"

ID = r"[A-Za-z0-9_-]{1,256}"
TOKEN = r"[A-Za-z0-9_\-.~!=+/]{1,2048}"
FIELDS = r"[A-Za-z0-9_,()/]{1,512}"
SMALL_NUMBER = r"[1-9][0-9]{0,2}"
_QUOTED = r"(?:[^'\\]|\\.){1,255}"
DRIVE_QUERY = (rf"'{ID}' in parents and name = '{_QUOTED}' and trashed = false"
               rf"(?: and mimeType = '{re.escape(FOLDER_MIME)}')?")
CREATE_KEYS = frozenset({"id", "name", "mimeType", "parents", "description"})
REFRESH_KEYS = frozenset({"grant_type", "client_id", "client_secret", "refresh_token"})


def drive_query(parent_id: str, name: str, folders_only: bool = False) -> str:
    escaped = name.replace("\\", "\\\\").replace("'", "\\'")
    query = f"'{parent_id}' in parents and name = '{escaped}' and trashed = false"
    return query + (f" and mimeType = '{FOLDER_MIME}'" if folders_only else "")


def _header(request: Request, name: str) -> str:
    for key, value in request.headers.items():
        if key.lower() == name.lower():
            return value
    return ""


def _check_metadata(meta: object, managed: Callable[[str], bool], *, folder: bool) -> None:
    if not isinstance(meta, dict):
        raise BlockedRequest("create metadata must be a JSON object")
    extra = sorted(set(meta) - CREATE_KEYS)
    if extra:
        raise BlockedRequest(f"metadata field(s) {', '.join(extra)} are not allowed")
    if not (isinstance(meta.get("id"), str) and re.fullmatch(ID, meta["id"])):
        raise BlockedRequest("a create needs a reserved id")
    name = meta.get("name")
    if not (isinstance(name, str) and name.strip() and len(name) <= 255):
        raise BlockedRequest("a create needs a name")
    parents = meta.get("parents")
    if not (isinstance(parents, list) and len(parents) == 1 and isinstance(parents[0], str)):
        raise BlockedRequest("a create needs exactly one parent")
    if not managed(parents[0]):
        raise BlockedRequest("the parent folder is not managed by mailroom")
    is_folder = meta.get("mimeType") == FOLDER_MIME
    if folder and not is_folder:
        raise BlockedRequest("only folders can be created without content")
    if not folder and is_folder:
        raise BlockedRequest("an upload cannot create a folder")
    if not isinstance(meta.get("description", ""), str):
        raise BlockedRequest("description must be text")


def _folder_create(managed: Callable[[str], bool]) -> Callable[[Request], None]:
    def check(request: Request) -> None:
        try:
            meta = json.loads(request.body or b"")
        except ValueError:
            raise BlockedRequest("folder metadata is not JSON") from None
        _check_metadata(meta, managed, folder=True)
    return check


def _upload(managed: Callable[[str], bool]) -> Callable[[Request], None]:
    def check(request: Request) -> None:
        problem = "the upload is not a two-part multipart/related request"
        try:
            parts = parse_related(request.body or b"", _header(request, "Content-Type"))
        except ValueError:
            raise BlockedRequest(problem) from None
        if len(parts) != 2 or not parts[0][0].get("content-type", "").startswith(
                "application/json"):
            raise BlockedRequest(problem)
        try:
            meta = json.loads(parts[0][1])
        except ValueError:
            raise BlockedRequest(problem) from None
        _check_metadata(meta, managed, folder=False)
    return check


def _refresh(request: Request) -> None:
    try:
        pairs = urllib.parse.parse_qsl((request.body or b"").decode("ascii"),
                                       strict_parsing=True)
    except ValueError:
        raise BlockedRequest("token request body is malformed") from None
    keys = [k for k, _ in pairs]
    if len(keys) != len(set(keys)) or not set(keys) <= REFRESH_KEYS:
        raise BlockedRequest("token request carries unexpected fields")
    if dict(pairs).get("grant_type") != "refresh_token":
        raise BlockedRequest("only refresh-token grants are allowed")


def google_allowlist(managed_folder: Callable[[str], bool]) -> Allowlist:
    gmail, drive = "gmail.googleapis.com", "www.googleapis.com"
    return Allowlist([
        Rule("gmail.messages.list", "GET", gmail, r"/gmail/v1/users/me/messages",
             query={"q", "maxResults", "pageToken"},
             validators={"q": r"[^\r\n\x00]{0,1000}", "maxResults": SMALL_NUMBER,
                         "pageToken": TOKEN}),
        Rule("gmail.messages.get", "GET", gmail, rf"/gmail/v1/users/me/messages/{ID}",
             required={"format": "full"}),
        Rule("gmail.attachments.get", "GET", gmail,
             rf"/gmail/v1/users/me/messages/{ID}/attachments/{ID}"),
        Rule("drive.files.generateIds", "GET", drive, r"/drive/v3/files/generateIds",
             query={"count"}, required={"space": "drive", "type": "files"},
             validators={"count": r"[1-9][0-9]?"}),
        Rule("drive.files.get", "GET", drive, rf"/drive/v3/files/{ID}", query={"fields"},
             validators={"fields": FIELDS}),
        Rule("drive.files.list", "GET", drive, r"/drive/v3/files",
             query={"q", "fields", "pageSize", "pageToken"}, required={"spaces": "drive"},
             validators={"q": DRIVE_QUERY, "fields": FIELDS, "pageSize": SMALL_NUMBER,
                         "pageToken": TOKEN}),
        Rule("drive.folders.create", "POST", drive, r"/drive/v3/files", query={"fields"},
             validators={"fields": FIELDS}, body=_folder_create(managed_folder)),
        Rule("drive.files.upload", "POST", drive, r"/upload/drive/v3/files", query={"fields"},
             required={"uploadType": "multipart"}, validators={"fields": FIELDS},
             body=_upload(managed_folder)),
        Rule("oauth.token.refresh", "POST", "oauth2.googleapis.com", r"/token",
             body=_refresh),
    ])


def is_idempotent(request: Request) -> bool:
    """Safe to resend after a lost response: reads, and creates (the allowlist guarantees a
    reserved id, so a second create of the same file fails with 409 instead of copying)."""
    path = urllib.parse.urlsplit(request.url).path
    return request.method == "GET" or (
        request.method == "POST" and path in ("/drive/v3/files", "/upload/drive/v3/files"))
