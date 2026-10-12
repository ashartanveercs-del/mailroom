"""multipart/related bodies for Drive uploads: metadata (JSON) + content, in one request."""

from __future__ import annotations

import json
import re
import secrets
from collections.abc import Mapping
from typing import Any

_MIME = re.compile(r"[a-z0-9][a-z0-9.+-]*/[a-z0-9][a-z0-9.+-]*")
_BOUNDARY = re.compile(r"multipart/related;\s*boundary=\"?([A-Za-z0-9'()+_,\-./:=?]{1,70})\"?",
                       re.IGNORECASE)
GOOGLE_TYPES = "application/vnd.google-apps."  # Drive's own types: folders, Docs, Sheets...


def is_google_type(mime: str) -> bool:
    return (mime or "").strip().lower().startswith(GOOGLE_TYPES)


def safe_mime(mime: str) -> str:
    """A type that is safe to put in an upload: a plain type/subtype that is not one of
    Drive's own types (those make Drive convert the upload, or create a folder). Anything
    else becomes octet-stream."""
    mime = (mime or "").strip().lower()
    if not _MIME.fullmatch(mime) or is_google_type(mime):
        return "application/octet-stream"
    return mime


def build_related(metadata: Mapping[str, Any], data: bytes, mime: str) -> tuple[bytes, str]:
    meta = json.dumps(dict(metadata)).encode("utf-8")
    while True:
        boundary = "mailroom-" + secrets.token_hex(16)
        marker = boundary.encode()
        if marker not in data and marker not in meta:
            break
    dash = b"--" + marker
    body = (dash + b"\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n" + meta
            + b"\r\n" + dash + b"\r\nContent-Type: " + safe_mime(mime).encode() + b"\r\n\r\n"
            + data + b"\r\n" + dash + b"--\r\n")
    return body, f"multipart/related; boundary={boundary}"


def parse_related(body: bytes, content_type: str) -> list[tuple[dict[str, str], bytes]]:
    """The parts of a multipart/related body. Strict: ValueError for anything unexpected."""
    match = _BOUNDARY.fullmatch((content_type or "").strip())
    if not match:
        raise ValueError("not multipart/related")
    dash = b"--" + match.group(1).encode()
    close = b"\r\n" + dash + b"--"
    if not body.startswith(dash + b"\r\n"):
        raise ValueError("body does not start with the boundary")
    if body.endswith(close + b"\r\n"):
        inner = body[len(dash) + 2:-len(close) - 2]
    elif body.endswith(close):
        inner = body[len(dash) + 2:-len(close)]
    else:
        raise ValueError("body does not end with the closing boundary")
    parts = []
    for chunk in inner.split(b"\r\n" + dash + b"\r\n"):
        head, sep, payload = chunk.partition(b"\r\n\r\n")
        if not sep:
            raise ValueError("a part has no header block")
        headers: dict[str, str] = {}
        for line in head.split(b"\r\n"):
            name, colon, value = line.partition(b":")
            if not colon:
                raise ValueError("malformed part header")
            headers[name.decode("ascii").strip().lower()] = value.decode("latin-1").strip()
        parts.append((headers, payload))
    return parts
