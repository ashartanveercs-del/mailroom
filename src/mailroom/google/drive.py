"""Google Drive as a create-only store: reserve ids, create folders and files, read them back.

Implements the `Drive` port. There is deliberately no update, move or delete here, and the
allowlist (see `policy.py`) has no endpoint for them either.
"""

from __future__ import annotations

import json
import urllib.parse
from typing import Any

from ..guard import PageCursor
from ..http import HttpError, Request, Response, Transport
from ..ports import FOLDER_MIME, AlreadyExists, DriveError, StoredFile
from .multipart import build_related, safe_mime
from .policy import DRIVE, UPLOAD, drive_query

FILE_FIELDS = "id,name,parents,mimeType,md5Checksum,size,trashed"


def _message(response: Response) -> str:
    try:
        return str(response.json()["error"]["message"])[:200]
    except (KeyError, TypeError, ValueError):
        return f"HTTP {response.status}"


def _error(response: Response) -> HttpError:
    """The error Drive answered with, keeping its reason code (`userRateLimitExceeded`,
    `storageQuotaExceeded`...): the status alone does not say whether to retry."""
    try:
        reason = str(response.json()["error"]["errors"][0]["reason"])[:100]
    except (KeyError, IndexError, TypeError, ValueError):
        reason = ""
    return HttpError(response.status, _message(response), reason=reason)


def _stored(meta: Any) -> StoredFile:
    if not isinstance(meta, dict) or not isinstance(meta.get("id"), str):
        raise HttpError(200, "Drive sent an unexpected file resource")
    size = meta.get("size")
    return StoredFile(id=meta["id"], name=str(meta.get("name", "")),
                      parents=tuple(meta.get("parents") or ()),
                      mime_type=str(meta.get("mimeType", "")),
                      md5=meta.get("md5Checksum"),
                      size=int(size) if size is not None else None,
                      trashed=bool(meta.get("trashed", False)))


class GoogleDrive:
    def __init__(self, transport: Transport, *, page_size: int = 100,
                 max_pages: int = 100) -> None:
        self.transport = transport
        self.page_size = page_size
        self.max_pages = max_pages

    def _send(self, method: str, url: str, body: bytes | None = None,
              headers: dict[str, str] | None = None) -> Response:
        return self.transport.send(Request(method, url, headers=headers or {}, body=body))

    def reserve_ids(self, count: int) -> list[str]:
        query = urllib.parse.urlencode({"count": count, "space": "drive", "type": "files"})
        response = self._send("GET", f"{DRIVE}/files/generateIds?{query}")
        if response.status != 200:
            raise _error(response)
        ids = response.json().get("ids") or []
        if len(ids) < count or not all(isinstance(i, str) for i in ids):
            raise HttpError(200, "Drive returned too few ids")
        return ids[:count]

    def _created(self, response: Response) -> StoredFile:
        if response.status == 200:
            return _stored(response.json())
        if response.status == 409:
            raise AlreadyExists(_message(response))
        if response.status == 404:
            raise DriveError(f"the parent folder was not found: {_message(response)}")
        raise _error(response)

    def create_folder(self, folder_id: str, name: str, parent_id: str) -> StoredFile:
        meta = {"id": folder_id, "name": name, "mimeType": FOLDER_MIME, "parents": [parent_id]}
        return self._created(self._send(
            "POST", f"{DRIVE}/files?fields={urllib.parse.quote(FILE_FIELDS, safe='')}",
            json.dumps(meta).encode(), {"Content-Type": "application/json; charset=UTF-8"}))

    def create_file(self, file_id: str, name: str, parent_id: str, data: bytes,
                    mime_type: str, description: str = "") -> StoredFile:
        meta: dict[str, Any] = {"id": file_id, "name": name, "mimeType": safe_mime(mime_type),
                                "parents": [parent_id]}
        if description:
            meta["description"] = description[:1000]
        body, content_type = build_related(meta, data, mime_type)
        query = urllib.parse.urlencode({"uploadType": "multipart", "fields": FILE_FIELDS})
        return self._created(self._send("POST", f"{UPLOAD}?{query}", body,
                                        {"Content-Type": content_type}))

    def get(self, file_id: str) -> StoredFile | None:
        fid = urllib.parse.quote(file_id, safe="")
        query = urllib.parse.urlencode({"fields": FILE_FIELDS})
        response = self._send("GET", f"{DRIVE}/files/{fid}?{query}")
        if response.status == 404:
            return None
        if response.status != 200:
            raise _error(response)
        return _stored(response.json())

    def find_children(self, parent_id: str, name: str,
                      folders_only: bool = False) -> list[StoredFile]:
        cursor = PageCursor(self.max_pages)
        found: list[StoredFile] = []
        token: str | None = None
        while True:
            params = {"q": drive_query(parent_id, name, folders_only),
                      "fields": f"nextPageToken,files({FILE_FIELDS})",
                      "pageSize": str(self.page_size), "spaces": "drive"}
            if token:
                params["pageToken"] = token
            response = self._send("GET", f"{DRIVE}/files?{urllib.parse.urlencode(params)}")
            if response.status != 200:
                raise _error(response)
            data = response.json()
            found += [f for f in map(_stored, data.get("files") or []) if f.name == name]
            token = cursor.advance(data.get("nextPageToken"))
            if token is None:
                return sorted(found, key=lambda f: f.id)
