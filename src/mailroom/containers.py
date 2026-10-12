"""What an attachment is, and what is inside it.

Types are decided by content, not by the name or MIME type the sender chose (a PDF called
`photo.jpg` is a PDF). Containers (zip archives and attached emails) are opened and their files
become separate items; the container itself is never filed. Anything that cannot be opened
safely (encrypted, corrupt, over a limit, nested too deep) is kept whole and flagged, so it is
filed for review instead of being dropped.

Archive limits are checked against the declared sizes first and then enforced again while
reading, because declared sizes are attacker-controlled.
"""

from __future__ import annotations

import email
import email.policy
import html
import io
import mimetypes
import re
import zipfile
import zlib
from dataclasses import dataclass, field
from pathlib import PurePosixPath

KIB, MIB = 1024, 1024 * 1024


@dataclass(frozen=True)
class Limits:
    max_members: int = 200
    max_member_bytes: int = 25 * MIB
    max_total_bytes: int = 100 * MIB
    max_ratio: int = 100            # uncompressed / compressed; above this it is a zip bomb
    max_depth: int = 2              # containers inside containers opened this deep
    inline_image_min_bytes: int = 32 * KIB  # smaller inline images are logos and pixels


@dataclass(frozen=True)
class Item:
    origin: str
    filename: str
    mime_type: str
    data: bytes
    kind: str
    flags: tuple[str, ...] = ()


@dataclass
class Unpacked:
    items: list[Item] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)

    def extend(self, other: Unpacked) -> None:
        self.items += other.items
        self.skipped += other.skipped


SIGNATURE_EXTS = frozenset({".p7s", ".asc", ".sig"})
SIGNATURE_MIMES = frozenset({"application/pkcs7-signature", "application/x-pkcs7-signature",
                             "application/pgp-signature"})
TEXT_EXTS = frozenset({".txt", ".csv", ".tsv", ".md"})
JUNK_NAMES = frozenset({".ds_store", "thumbs.db", "desktop.ini"})
IMAGE_MAGIC = (b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"GIF87a", b"GIF89a", b"II*\x00",
               b"MM\x00*", b"BM")


def sniff(data: bytes, filename: str = "", mime: str = "") -> str:
    """One of: pdf, image, zip, docx, office, eml, html, text, binary."""
    # Signatures at offset 0 first: a zip that stores a PDF uncompressed has "%PDF-" in its
    # first kilobyte, and PDF readers accept a header anywhere in that kilobyte.
    if data.startswith((b"PK\x03\x04", b"PK\x05\x06")):
        return _zip_flavour(data)
    if data.startswith(IMAGE_MAGIC) or (data[:4] == b"RIFF" and data[8:12] == b"WEBP"):
        return "image"
    if b"%PDF-" in data[:1024]:
        return "pdf"
    ext = PurePosixPath(filename.lower()).suffix
    mime = (mime or "").lower()
    if mime == "message/rfc822" or ext == ".eml":
        return "eml"
    if mime == "text/html" or ext in (".html", ".htm"):
        return "html"
    if (mime.startswith("text/") or ext in TEXT_EXTS) and b"\x00" not in data[:4096]:
        return "text"
    return "binary"


def _zip_flavour(data: bytes) -> str:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            names = set(zf.namelist())
    except (zipfile.BadZipFile, OSError, ValueError):
        return "zip"
    if "[Content_Types].xml" in names:
        return "docx" if "word/document.xml" in names else "office"
    if "mimetype" in names and any(n.startswith("META-INF/") for n in names):
        return "office"
    return "zip"


DEFAULT_LIMITS = Limits()


def unpack(filename: str, mime: str, data: bytes, *, limits: Limits = DEFAULT_LIMITS,
           inline: bool = False, depth: int = 0, origin: str | None = None) -> Unpacked:
    """Every document an attachment holds, plus what was skipped and why."""
    origin = origin or _clean_path(filename) or "attachment"
    if not data:
        return Unpacked(skipped=[(origin, "empty_file")])
    if (PurePosixPath(filename.lower()).suffix in SIGNATURE_EXTS
            or (mime or "").lower() in SIGNATURE_MIMES):
        return Unpacked(skipped=[(origin, "signature_file")])
    kind = sniff(data, filename, mime)
    if inline and kind == "image" and len(data) < limits.inline_image_min_bytes:
        return Unpacked(skipped=[(origin, "inline_image")])
    mime = mime or mimetypes.guess_type(filename)[0] or "application/octet-stream"
    whole = Item(origin, filename, mime, data, kind)
    if kind == "zip":
        if depth >= limits.max_depth:
            return Unpacked(items=[_flag(whole, "archive_too_deep")])
        return _unpack_zip(whole, limits, depth)
    if kind == "eml":
        if depth >= limits.max_depth:
            return Unpacked(items=[_flag(whole, "email_too_deep")])
        return _unpack_eml(whole, limits, depth)
    return Unpacked(items=[whole])


def _flag(item: Item, flag: str) -> Item:
    return Item(item.origin, item.filename, item.mime_type, item.data, item.kind, (flag,))


def _clean_path(name: str) -> str:
    parts = [p for p in re.split(r"[\\/]+", name) if p not in ("", ".", "..")]
    return "/".join(parts)


def _is_junk(path: str) -> bool:
    base = path.rsplit("/", 1)[-1]
    return (path.startswith("__MACOSX/") or base.startswith("._")
            or base.lower() in JUNK_NAMES)


class _LimitExceeded(Exception):
    pass


def _unpack_zip(archive: Item, limits: Limits, depth: int) -> Unpacked:
    try:
        members = _read_zip(archive.data, limits)
    except _LimitExceeded:
        return Unpacked(items=[_flag(archive, "archive_limits")])
    except _Encrypted:
        return Unpacked(items=[_flag(archive, "encrypted_archive")])
    except (zipfile.BadZipFile, zlib.error, EOFError, OSError, ValueError,
            NotImplementedError, RuntimeError):
        return Unpacked(items=[_flag(archive, "unreadable_archive")])
    out = Unpacked()
    seen: dict[str, int] = {}
    for path, blob in members:
        clean = _clean_path(path) or f"member-{len(seen) + 1}"
        member_origin = _unique(f"{archive.origin}/{clean}", seen)
        name = clean.rsplit("/", 1)[-1]
        mime = mimetypes.guess_type(name)[0] or ""
        out.extend(unpack(name, mime, blob, limits=limits, depth=depth + 1,
                          origin=member_origin))
    return out


class _Encrypted(Exception):
    pass


def _read_zip(data: bytes, limits: Limits) -> list[tuple[str, bytes]]:
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        infos = [i for i in zf.infolist() if not i.is_dir() and not _is_junk(i.filename)]
        if len(infos) > limits.max_members:
            raise _LimitExceeded
        if any(i.flag_bits & 0x1 for i in infos):
            raise _Encrypted
        declared = sum(i.file_size for i in infos)
        if declared > limits.max_total_bytes or declared > limits.max_ratio * max(len(data), 1):
            raise _LimitExceeded
        for info in infos:
            ratio = info.file_size / info.compress_size if info.compress_size else 0
            if info.file_size > limits.max_member_bytes or ratio > limits.max_ratio:
                raise _LimitExceeded
        out, total = [], 0
        for info in infos:
            with zf.open(info) as fh:
                blob = fh.read(limits.max_member_bytes + 1)
            total += len(blob)
            if len(blob) > limits.max_member_bytes or total > limits.max_total_bytes:
                raise _LimitExceeded
            out.append((info.filename, blob))
        return out


def _unique(origin: str, seen: dict[str, int]) -> str:
    count = seen.get(origin, 0) + 1
    seen[origin] = count
    return origin if count == 1 else f"{origin}#{count}"


def _unpack_eml(container: Item, limits: Limits, depth: int) -> Unpacked:
    try:
        msg = email.message_from_bytes(container.data, policy=email.policy.default)
        parts = list(msg.iter_attachments())
    except Exception:
        return Unpacked(items=[_flag(container, "unreadable_email")])
    out = Unpacked()
    seen: dict[str, int] = {}
    for index, part in enumerate(parts, start=1):
        ctype = part.get_content_type()
        if ctype == "message/rfc822":
            inner = part.get_payload(0) if part.is_multipart() else part.get_content()
            blob = inner.as_bytes()
            name = part.get_filename() or f"attached-{index}.eml"
        else:
            blob = part.get_payload(decode=True) or b""
            ext = mimetypes.guess_extension(ctype) or ""
            name = part.get_filename() or f"attachment-{index}{ext}"
        member_origin = _unique(f"{container.origin}/{_clean_path(name) or index}", seen)
        out.extend(unpack(name, ctype, blob, limits=limits, depth=depth + 1,
                          inline=part.get_content_disposition() == "inline",
                          origin=member_origin))
    if not parts:
        out.skipped.append((container.origin, "no_attachments"))
    return out


def docx_text(data: bytes, max_bytes: int = 10 * MIB) -> str:
    """Paragraph text of a Word document. A regex, not an XML parser, pulls the text out: a
    hostile document gets no entity expansion and no external references resolved."""
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        info = zf.getinfo("word/document.xml")
        if info.file_size > max_bytes:
            raise ValueError("document.xml is too large")
        xml = zf.read(info).decode("utf-8", errors="replace")
    paragraphs = []
    for para in re.findall(r"<w:p[ >].*?</w:p>", xml, flags=re.S):
        runs = re.findall(r"<w:t(?:\s[^>]*)?>(.*?)</w:t>", para, flags=re.S)
        paragraphs.append(html.unescape("".join(runs)))
    return "\n".join(p for p in paragraphs if p.strip())
