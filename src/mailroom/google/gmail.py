"""Gmail intake: list matching messages, read them and their attachments. Nothing else.

Every request goes out through the guarded transport (see `policy.py`), which only knows the
three read endpoints. Next-page tokens are validated by a `PageCursor` and spliced into this
adapter's own URL; nothing the API returns is ever requested as a URL.
"""

from __future__ import annotations

import base64
import urllib.parse
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

from ..guard import PageCursor
from ..http import HttpError, Request, Transport
from ..models import Attachment, Message
from .policy import GMAIL


def _b64url_decode(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def _walk(part: dict[str, Any]) -> Iterator[dict[str, Any]]:
    yield part
    if part.get("mimeType") == "message/rfc822":
        return  # an attached email is one attachment; container expansion opens it later
    for sub in part.get("parts") or []:
        yield from _walk(sub)


class GmailMailbox:
    def __init__(self, transport: Transport, *, page_size: int = 100,
                 max_attachment_bytes: int = 25 * 1024 * 1024, max_pages: int = 200) -> None:
        self.transport = transport
        self.page_size = page_size
        self.max_attachment_bytes = max_attachment_bytes
        self.max_pages = max_pages

    def _get(self, url: str) -> dict[str, Any]:
        response = self.transport.send(Request("GET", url))
        if response.status != 200:
            raise HttpError(response.status, f"Gmail answered HTTP {response.status}")
        data = response.json()
        if not isinstance(data, dict):
            raise HttpError(response.status, "Gmail sent an unexpected response")
        return data

    def list_message_ids(self, query: str) -> list[str]:
        """Message ids matching `query`, oldest first (Gmail lists newest first)."""
        cursor = PageCursor(self.max_pages)
        ids: list[str] = []
        seen: set[str] = set()
        token: str | None = None
        while True:
            params = {"q": query, "maxResults": str(self.page_size)}
            if token:
                params["pageToken"] = token
            data = self._get(f"{GMAIL}/messages?{urllib.parse.urlencode(params)}")
            for item in data.get("messages") or []:
                mid = item.get("id") if isinstance(item, dict) else None
                if isinstance(mid, str) and mid not in seen:
                    seen.add(mid)
                    ids.append(mid)
            token = cursor.advance(data.get("nextPageToken"))
            if token is None:
                return list(reversed(ids))

    def get_message(self, message_id: str) -> Message:
        mid = urllib.parse.quote(message_id, safe="")
        data = self._get(f"{GMAIL}/messages/{mid}?format=full")
        payload = data.get("payload") or {}
        headers = {h.get("name", "").lower(): h.get("value", "")
                   for h in payload.get("headers") or []}
        attachments: list[Attachment] = []
        skipped: list[tuple[str, str]] = []
        for part in _walk(payload):
            found = self._attachment(mid, part)
            if isinstance(found, Attachment):
                attachments.append(found)
            elif found is not None:
                skipped.append(found)
        received = datetime.fromtimestamp(int(data.get("internalDate", "0")) / 1000, UTC)
        return Message(id=message_id, sender=headers.get("from", ""),
                       subject=headers.get("subject", ""), received_at=received,
                       labels=frozenset(data.get("labelIds") or ()),
                       attachments=tuple(attachments), skipped=tuple(skipped))

    def _attachment(self, mid: str, part: dict[str, Any]) -> Attachment | tuple[str, str] | None:
        mime = part.get("mimeType") or "application/octet-stream"
        if mime.startswith("multipart/"):
            return None
        headers = {h.get("name", "").lower(): h.get("value", "")
                   for h in part.get("headers") or []}
        disposition = headers.get("content-disposition", "").lower()
        filename = part.get("filename") or ""
        if not (filename or mime == "message/rfc822" or disposition.startswith("attachment")):
            return None  # the message body itself
        part_id = part.get("partId") or "0"
        if not filename:
            filename = f"message-{part_id}.eml" if mime == "message/rfc822" \
                else f"attachment-{part_id}"
        body = part.get("body") or {}
        if int(body.get("size") or 0) > self.max_attachment_bytes:
            return (filename, "too_large")
        if body.get("attachmentId"):
            aid = urllib.parse.quote(str(body["attachmentId"]), safe="")
            data = _b64url_decode(self._get(f"{GMAIL}/messages/{mid}/attachments/{aid}")
                                  .get("data") or "")
        else:
            data = _b64url_decode(body.get("data") or "")
        inline = disposition.startswith("inline") or (
            "content-id" in headers and not disposition.startswith("attachment"))
        return Attachment(filename, mime, data, part_id=part_id, inline=inline)
