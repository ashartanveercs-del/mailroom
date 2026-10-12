"""An in-process stand-in for the Gmail and Drive REST APIs (the parts mailroom uses).

It speaks the real wire format: Gmail message resources with base64url bodies and attachment
ids, Drive file resources with md5Checksum and string sizes, multipart/related uploads,
409 on a duplicate id, 404 on a missing parent, opaque page tokens, bearer tokens that expire
and a refresh-token grant. That lets the production adapters, the allowlist guard and the
retry and auth layers run end to end with no network.

Drive state lives in an `InMemoryDrive`, so crash plans and inspection helpers work the same
for both transports. `Faults` injects throttling and lost responses on a fixed schedule.
"""

from __future__ import annotations

import base64
import email
import email.policy
import functools
import hashlib
import json
import re
import urllib.parse
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from email.message import EmailMessage
from email.utils import format_datetime
from typing import Any

from ..google.auth import OAuthCredentials
from ..google.multipart import parse_related
from ..http import Request, Response, Transport, TransportError
from ..models import Message
from ..ports import FOLDER_MIME, AlreadyExists, DriveError, StoredFile
from .memory import InMemoryDrive

CLIENT_ID, CLIENT_SECRET, REFRESH_TOKEN = "sim-client", "sim-secret", "sim-refresh-token"
_QUERY = re.compile(r"'([A-Za-z0-9_-]+)' in parents and name = '((?:[^'\\]|\\.)*)' "
                    r"and trashed = false( and mimeType = '[^']+')?")
_LABEL_TERMS = {"sent": "SENT", "drafts": "DRAFT", "draft": "DRAFT", "spam": "SPAM",
                "trash": "TRASH", "inbox": "INBOX"}


@dataclass
class Faults:
    transient_every: int | None = None           # every Nth request: 503 before processing
    lose_write_response_every: int | None = None  # every Nth new write: applied, reply lost
    injected: int = 0
    _requests: int = 0
    _writes: int = 0
    _lost: set[str] = field(default_factory=set)

    def before(self, request: Request) -> Response | None:
        self._requests += 1
        if self.transient_every and self._requests % self.transient_every == 0:
            self.injected += 1
            return _error(503, "backend error", headers={"Retry-After": "0"})
        return None

    def after(self, request: Request, response: Response) -> None:
        if not (self.lose_write_response_every and request.method == "POST"
                and response.status == 200 and "googleapis.com" in request.url):
            return
        digest = hashlib.sha256(request.url.encode() + (request.body or b"")).hexdigest()
        if digest in self._lost:
            return
        self._writes += 1
        if self._writes % self.lose_write_response_every == 0:
            self._lost.add(digest)
            self.injected += 1
            raise TransportError("connection reset after the write was applied")


def _error(status: int, message: str, headers: dict[str, str] | None = None) -> Response:
    body = {"error": {"code": status, "message": message,
                      "errors": [{"message": message, "reason": "simulated"}]}}
    return Response(status, {"Content-Type": "application/json", **(headers or {})},
                     json.dumps(body).encode())


def _ok(payload: Any) -> Response:
    return Response(200, {"Content-Type": "application/json"}, json.dumps(payload).encode())


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def to_email(message: Message) -> EmailMessage:
    """The RFC 822 form of a `Message`, as the sender's mail client would have built it."""
    em = EmailMessage()
    em["From"] = message.sender
    em["To"] = "files@example.com"
    em["Subject"] = message.subject
    em["Date"] = format_datetime(message.received_at)
    em["Message-ID"] = f"<{message.id}@mail.example.com>"
    em.set_content("Documents attached.\n")
    for index, att in enumerate(message.attachments):
        maintype, _, subtype = (att.mime_type or "application/octet-stream").partition("/")
        if (maintype, subtype) == ("message", "rfc822"):
            inner = email.message_from_bytes(att.data, policy=email.policy.default)
            em.add_attachment(inner, filename=att.filename)
        elif att.inline:
            em.add_attachment(att.data, maintype=maintype, subtype=subtype or "octet-stream",
                              filename=att.filename, disposition="inline",
                              cid=f"<part{index}@mail.example.com>")
        else:
            em.add_attachment(att.data, maintype=maintype, subtype=subtype or "octet-stream",
                              filename=att.filename)
    return em


def attachment_id_for(message_id: str, part_id: str) -> str:
    """An attachment id shaped like the ones Gmail issues: opaque URL-safe base64, several
    hundred characters long (far longer than message or Drive file ids)."""
    seed = hashlib.sha256(f"{message_id}/{part_id}".encode()).digest()
    return "ANGjdJ" + _b64url(seed * 9)[:378]


@functools.lru_cache(maxsize=1024)
def _gmail_resource(message: Message) -> tuple[dict[str, Any], dict[str, bytes]]:
    """The Gmail payload of a message and its attachments by id. A pure function of the
    (immutable) message, so it is computed once per process; callers must not mutate it."""
    attachments: dict[str, bytes] = {}
    return _payload(to_email(message), "", message.id, attachments), attachments


def _payload(part: EmailMessage, part_id: str, message_id: str,
             attachments: dict[str, bytes]) -> dict[str, Any]:
    mime = part.get_content_type()
    out: dict[str, Any] = {"partId": part_id, "mimeType": mime,
                           "filename": part.get_filename() or "",
                           "headers": [{"name": k, "value": str(v)} for k, v in part.items()]}
    if part.is_multipart() and mime != "message/rfc822":
        out["body"] = {"size": 0}
        out["parts"] = [_payload(sub, f"{part_id}.{i}" if part_id else str(i), message_id,
                                 attachments)
                        for i, sub in enumerate(part.iter_parts())]
        return out
    if mime == "message/rfc822":
        data = part.get_payload(0).as_bytes()
    else:
        data = part.get_payload(decode=True) or b""
    if out["filename"] or mime == "message/rfc822" or part.get_content_disposition():
        attachment_id = attachment_id_for(message_id, part_id)
        attachments[attachment_id] = data
        out["body"] = {"attachmentId": attachment_id, "size": len(data)}
    else:
        out["body"] = {"size": len(data), "data": _b64url(data)}
    return out


class SimulatedGoogle:
    def __init__(self, messages: Iterable[Message] = (), *, root_id: str = "root",
                 page_size: int | None = None,
                 next_page_token: Callable[[int], str] | None = None,
                 faults: Faults | None = None, drive: InMemoryDrive | None = None) -> None:
        self.root_id = root_id
        self.drive = drive or InMemoryDrive(root_id)
        self.messages = {m.id: m for m in messages}
        self.page_size = page_size
        self.next_page_token = next_page_token
        self.faults = faults or Faults()
        self.requests: list[tuple[str, str]] = []
        self.unexpected: list[tuple[str, str]] = []
        self.tokens_issued = 0
        self._tokens: set[str] = set()
        self._page_tokens: dict[str, int] = {}
        self._routes = [
            ("GET", "gmail.googleapis.com", r"/gmail/v1/users/me/messages", self._gmail_list),
            ("GET", "gmail.googleapis.com", r"/gmail/v1/users/me/messages/([^/]+)",
             self._gmail_get),
            ("GET", "gmail.googleapis.com",
             r"/gmail/v1/users/me/messages/([^/]+)/attachments/([^/]+)",
             self._gmail_attachment),
            ("GET", "www.googleapis.com", r"/drive/v3/files/generateIds", self._generate_ids),
            ("GET", "www.googleapis.com", r"/drive/v3/files/([^/]+)", self._file_get),
            ("GET", "www.googleapis.com", r"/drive/v3/files", self._file_list),
            ("POST", "www.googleapis.com", r"/drive/v3/files", self._folder_create),
            ("POST", "www.googleapis.com", r"/upload/drive/v3/files", self._upload),
        ]

    # -- plumbing --------------------------------------------------------------------------

    def credentials(self, transport: Transport) -> OAuthCredentials:
        return OAuthCredentials(client_id=CLIENT_ID, client_secret=CLIENT_SECRET,
                                refresh_token=REFRESH_TOKEN, transport=transport)

    def expire_tokens(self) -> None:
        self._tokens.clear()

    def send(self, request: Request) -> Response:
        self.requests.append((request.method, request.url))
        parts = urllib.parse.urlsplit(request.url)
        query = dict(urllib.parse.parse_qsl(parts.query, keep_blank_values=True))
        fault = self.faults.before(request)
        if fault is not None:
            return fault
        if parts.hostname == "oauth2.googleapis.com" and parts.path == "/token":
            return self._token(request)
        bearer = request.headers.get("Authorization", "").removeprefix("Bearer ")
        if bearer not in self._tokens:
            return _error(401, "Invalid Credentials")
        for method, host, pattern, handler in self._routes:
            match = re.fullmatch(pattern, parts.path)
            if method == request.method and host == parts.hostname and match:
                response = handler(request, query, *match.groups())
                self.faults.after(request, response)
                return response
        self.unexpected.append((request.method, request.url))
        return _error(404, "Not Found")

    def _token(self, request: Request) -> Response:
        form = dict(urllib.parse.parse_qsl((request.body or b"").decode()))
        if (form.get("grant_type"), form.get("refresh_token"), form.get("client_id")) != (
                "refresh_token", REFRESH_TOKEN, CLIENT_ID):
            return Response(400, {}, b'{"error": "invalid_grant"}')
        self.tokens_issued += 1
        token = f"sim-access-{self.tokens_issued}"
        self._tokens.add(token)
        return _ok({"access_token": token, "expires_in": 3599, "token_type": "Bearer"})

    # -- Gmail -----------------------------------------------------------------------------

    def _matches(self, message: Message, q: str) -> bool:
        labels = {label.upper() for label in message.labels}
        terms = q.split()
        include_spam_trash = any(t.lower() in ("in:spam", "in:trash") for t in terms)
        if not include_spam_trash and labels & {"SPAM", "TRASH"}:
            return False  # Gmail leaves spam and trash out unless asked for them
        for term in terms:
            lowered = term.lower()
            negate = lowered.startswith("-")
            lowered = lowered.lstrip("-")
            if lowered.startswith("in:") and lowered[3:] in _LABEL_TERMS:
                if (_LABEL_TERMS[lowered[3:]] in labels) == negate:
                    return False
            elif lowered == "has:attachment" and not message.attachments and not negate:
                return False
        return True

    def _gmail_list(self, request: Request, query: dict[str, str]) -> Response:
        found = sorted((m for m in self.messages.values() if self._matches(m, query.get("q", ""))),
                       key=lambda m: (m.received_at, m.id), reverse=True)
        size = self.page_size or int(query.get("maxResults", "100"))
        token = query.get("pageToken")
        if token is not None and token not in self._page_tokens:
            return _error(400, "Invalid pageToken")
        offset = self._page_tokens[token] if token else 0
        page = found[offset:offset + size]
        payload: dict[str, Any] = {"messages": [{"id": m.id, "threadId": m.id} for m in page],
                                   "resultSizeEstimate": len(found)}
        if offset + size < len(found):
            following = (self.next_page_token(offset // size + 1) if self.next_page_token
                         else str(1_000_000 + offset + size))
            self._page_tokens[following] = offset + size
            payload["nextPageToken"] = following
        return _ok(payload)

    def _gmail_get(self, request: Request, query: dict[str, str], message_id: str) -> Response:
        message = self.messages.get(message_id)
        if message is None:
            return _error(404, "Requested entity was not found.")
        payload, _ = _gmail_resource(message)
        return _ok({"id": message.id, "threadId": message.id, "labelIds": sorted(message.labels),
                    "internalDate": str(int(message.received_at.timestamp() * 1000)),
                    "payload": payload})

    def _gmail_attachment(self, request: Request, query: dict[str, str], message_id: str,
                          attachment_id: str) -> Response:
        message = self.messages.get(message_id)
        data = _gmail_resource(message)[1].get(attachment_id) if message else None
        if data is None:
            return _error(404, "Invalid attachment id")
        return _ok({"size": len(data), "data": _b64url(data)})

    # -- Drive -----------------------------------------------------------------------------

    @staticmethod
    def _resource(stored: StoredFile) -> dict[str, Any]:
        out: dict[str, Any] = {"kind": "drive#file", "id": stored.id, "name": stored.name,
                               "mimeType": stored.mime_type, "parents": list(stored.parents),
                               "trashed": stored.trashed}
        if not stored.is_folder:
            out["md5Checksum"] = stored.md5
            out["size"] = str(stored.size)  # Drive sends int64 values as strings
        return out

    def _generate_ids(self, request: Request, query: dict[str, str]) -> Response:
        ids = self.drive.reserve_ids(int(query.get("count", "10")))
        return _ok({"kind": "drive#generatedIds", "space": "drive", "ids": ids})

    def _file_get(self, request: Request, query: dict[str, str], file_id: str) -> Response:
        stored = self.drive.get(urllib.parse.unquote(file_id))
        if stored is None:
            return _error(404, f"File not found: {file_id}.")
        return _ok(self._resource(stored))

    def _file_list(self, request: Request, query: dict[str, str]) -> Response:
        match = _QUERY.fullmatch(query.get("q", ""))
        if not match:
            return _error(400, "Invalid Value")
        parent, raw_name, folders = match.groups()
        name = re.sub(r"\\(.)", r"\1", raw_name)
        found = self.drive.find_children(parent, name, folders_only=bool(folders))
        size = int(query.get("pageSize", "100"))
        offset = int(query.get("pageToken", "0"))
        payload: dict[str, Any] = {"files": [self._resource(f)
                                             for f in found[offset:offset + size]]}
        if offset + size < len(found):
            payload["nextPageToken"] = str(offset + size)
        return _ok(payload)

    def _create(self, make: Callable[[], StoredFile]) -> Response:
        try:
            return _ok(self._resource(make()))
        except AlreadyExists:
            return _error(409, "A file already exists with the provided ID.")
        except DriveError:
            return _error(404, "File not found: parent.")

    def _folder_create(self, request: Request, query: dict[str, str]) -> Response:
        meta = json.loads(request.body or b"{}")
        if meta.get("mimeType") != FOLDER_MIME:
            return _error(400, "metadata-only creates must be folders here")
        return self._create(lambda: self.drive.create_folder(meta["id"], meta["name"],
                                                             meta["parents"][0]))

    def _upload(self, request: Request, query: dict[str, str]) -> Response:
        content_type = next((v for k, v in request.headers.items()
                             if k.lower() == "content-type"), "")
        try:
            (_, raw_meta), (media_headers, data) = parse_related(request.body or b"",
                                                                 content_type)
        except ValueError:
            return _error(400, "Malformed multipart body")
        meta = json.loads(raw_meta)
        return self._create(lambda: self.drive.create_file(
            meta["id"], meta["name"], meta["parents"][0], data,
            meta.get("mimeType") or media_headers.get("content-type", ""),
            meta.get("description", "")))
