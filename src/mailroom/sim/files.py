"""Builders for synthetic files (zip, PNG, docx). Used by tests and the evaluation corpora."""

from __future__ import annotations

import io
import struct
import zipfile
import zlib
from collections.abc import Mapping, Sequence
from xml.sax.saxutils import escape

_FIXED_DATE = (2026, 1, 1, 0, 0, 0)  # deterministic archives -> stable content hashes


def make_zip(files: Mapping[str, bytes], *, compress: bool = True, encrypt: bool = False,
             declare_size: int | None = None) -> bytes:
    """A zip of `files`. `encrypt` marks every member as encrypted (the flag a real encrypted
    archive carries); `declare_size` forges the uncompressed size recorded in the headers."""
    buf = io.BytesIO()
    method = zipfile.ZIP_DEFLATED if compress else zipfile.ZIP_STORED
    with zipfile.ZipFile(buf, "w", compression=method) as zf:
        for name, data in files.items():
            info = zipfile.ZipInfo(name, date_time=_FIXED_DATE)
            info.compress_type = method
            zf.writestr(info, data)
    raw = bytearray(buf.getvalue())
    if encrypt or declare_size is not None:
        _patch_headers(raw, encrypt, declare_size)
    return bytes(raw)


def _patch_headers(raw: bytearray, encrypt: bool, declare_size: int | None) -> None:
    for signature, flag_at, size_at in ((b"PK\x03\x04", 6, 22), (b"PK\x01\x02", 8, 24)):
        start = 0
        while (pos := raw.find(signature, start)) != -1:
            if encrypt:
                flags = struct.unpack_from("<H", raw, pos + flag_at)[0] | 0x1
                struct.pack_into("<H", raw, pos + flag_at, flags)
            if declare_size is not None:
                struct.pack_into("<I", raw, pos + size_at, declare_size)
            start = pos + 4


def _png_chunk(kind: bytes, payload: bytes) -> bytes:
    return (struct.pack(">I", len(payload)) + kind + payload
            + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF))


def make_png(*, padding: int = 0, seed: int = 0) -> bytes:
    """A valid 1x1 PNG; `padding` bytes of ancillary data make it as large as needed and
    `seed` makes otherwise identical images differ."""
    header = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    pixels = zlib.compress(b"\x00" + bytes([seed % 256, 128, 64]))
    chunks = [_png_chunk(b"IHDR", header)]
    if padding:
        chunks.append(_png_chunk(b"tEXt", b"Comment\x00" + b"." * padding))
    chunks += [_png_chunk(b"IDAT", pixels), _png_chunk(b"IEND", b"")]
    return b"\x89PNG\r\n\x1a\n" + b"".join(chunks)


_CONTENT_TYPES = (
    b'<?xml version="1.0" encoding="UTF-8"?>'
    b'<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    b'<Override PartName="/word/document.xml" ContentType="application/'
    b'vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>')


def make_docx(paragraphs: Sequence[str]) -> bytes:
    """A minimal Word document: enough structure for text extraction."""
    body = "".join(f"<w:p><w:r><w:t>{escape(p)}</w:t></w:r></w:p>" for p in paragraphs)
    document = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body>{body}</w:body></w:document>").encode()
    return make_zip({"[Content_Types].xml": _CONTENT_TYPES, "word/document.xml": document})
