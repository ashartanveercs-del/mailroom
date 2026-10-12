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


_IMAGE_TYPES = ((b"\x89PNG\r\n\x1a\n", "image/png"), (b"\xff\xd8\xff", "image/jpeg"),
                (b"GIF87a", "image/gif"), (b"GIF89a", "image/gif"), (b"II*\x00", "image/tiff"),
                (b"MM\x00*", "image/tiff"), (b"BM", "image/bmp"))
_KIND_TYPES = {
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "zip": "application/zip",
    "eml": "message/rfc822",
    "html": "text/html",
}
_TEXT_TYPES = {".csv": "text/csv", ".tsv": "text/tab-separated-values", ".md": "text/markdown"}
_OOXML_PARTS = {
    "xl/workbook.xml": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "ppt/presentation.xml":
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}
_ODF_TYPES = frozenset(f"application/vnd.oasis.opendocument.{kind}"
                       for kind in ("text", "spreadsheet", "presentation", "graphics"))


def content_type(kind: str, data: bytes, filename: str = "") -> str:
    """The MIME type to store a file under, decided from its bytes as its kind (`sniff`) is.

    The Content-Type an attachment arrives with is the sender's choice, and a drive acts on
    the type it is handed: a Google-native type makes Drive convert the upload (or refuse it
    as a folder). So that value is never passed on. Only fixed, plain types come out of here;
    the name's extension can at most pick between text types."""
    if kind == "image":
        if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            return "image/webp"
        return next((mime for magic, mime in _IMAGE_TYPES if data.startswith(magic)),
                    "application/octet-stream")
    if kind == "text":
        return _TEXT_TYPES.get(PurePosixPath(filename.lower()).suffix, "text/plain")
    if kind == "office":
        return _office_type(data)
    return _KIND_TYPES.get(kind, "application/octet-stream")


def _office_type(data: bytes) -> str:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            names = set(zf.namelist())
            for part, mime in _OOXML_PARTS.items():
                if part in names:
                    return mime
            if "mimetype" in names:
                with zf.open("mimetype") as fh:  # read 100 bytes at most, whatever it claims
                    declared = fh.read(100).decode("ascii", errors="replace").strip()
                if declared in _ODF_TYPES:
                    return declared
    except (zipfile.BadZipFile, zlib.error, EOFError, OSError, ValueError,
            NotImplementedError, RuntimeError):
        pass
    return "application/octet-stream"


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
    names = UniqueNames()
    for number, (path, blob) in enumerate(members, start=1):
        clean = _clean_path(path) or f"member-{number}"
        member_origin = names.claim(f"{archive.origin}/{clean}")
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


class UniqueNames:
    """Hands out names that are unique among those already handed out: `a.pdf`, then
    `a.pdf#2`, `a.pdf#3`... A suffixed name is checked like any other, so a file that is
    itself called `a.pdf#2` cannot end up with the same name as the second `a.pdf`. The next
    suffix to try is remembered per name, so a flood of identical names stays linear."""

    def __init__(self) -> None:
        self._taken: set[str] = set()
        self._next: dict[str, int] = {}

    def claim(self, name: str) -> str:
        if name not in self._taken:
            self._taken.add(name)
            return name
        number = self._next.get(name, 2)
        while f"{name}#{number}" in self._taken:
            number += 1
        self._next[name] = number + 1
        unique = f"{name}#{number}"
        self._taken.add(unique)
        return unique


def _unpack_eml(container: Item, limits: Limits, depth: int) -> Unpacked:
    try:
        msg = email.message_from_bytes(container.data, policy=email.policy.default)
        parts = list(msg.iter_attachments())
    except Exception:
        return Unpacked(items=[_flag(container, "unreadable_email")])
    out = Unpacked()
    names = UniqueNames()
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
        member_origin = names.claim(f"{container.origin}/{_clean_path(name) or index}")
        out.extend(unpack(name, ctype, blob, limits=limits, depth=depth + 1,
                          inline=part.get_content_disposition() == "inline",
                          origin=member_origin))
    if not parts:
        out.skipped.append((container.origin, "no_attachments"))
    return out


# A paragraph (w:p) or text (w:t) tag. `[^<>]*` cannot run past the next tag, so each match
# attempt stops at the next "<" and the scan as a whole stays linear in the input.
_WORD_TAG = re.compile(r"<(/?)w:([pt])\b[^<>]*>")
_ANY_TAG = re.compile(r"<[^<>]*>")


def docx_text(data: bytes, max_bytes: int = 10 * MIB, max_paragraphs: int = 100_000) -> str:
    """Paragraph text of a Word document, one line per complete paragraph.

    One forward pass over the paragraph and text tags pulls the text out. It is not an XML
    parser, so a hostile document gets no entity expansion and no external references
    resolved, and not a backtracking pattern either: with paragraphs left open, a pattern
    like `<w:p>.*?</w:p>` rescans to the end of the file from every one of them, so its cost
    grows with the square of the size (30,000 open paragraphs, 2 KB zipped, kept one busy
    for almost two minutes). Paragraphs nested in a paragraph (a text box) come out as
    paragraphs of their own; a paragraph never closed is left out."""
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        info = zf.getinfo("word/document.xml")
        if info.file_size > max_bytes:
            raise ValueError("document.xml is too large")
        xml = zf.read(info).decode("utf-8", errors="replace")
    finished: list[str] = []
    open_paragraphs: list[list[str]] = []  # innermost last
    text_starts: int | None = None         # just after the open <w:t>, if one is open
    opened = 0
    for tag in _WORD_TAG.finditer(xml):
        closing, kind = tag.group(1) == "/", tag.group(2)
        empty = tag.group().endswith("/>")
        if kind == "t":
            if closing and text_starts is not None and open_paragraphs:
                run = _ANY_TAG.sub("", xml[text_starts:tag.start()])
                open_paragraphs[-1].append(run)
            text_starts = None if closing or empty else tag.end()
        elif closing:
            if open_paragraphs:
                finished.append("".join(open_paragraphs.pop()))
        elif not empty:
            opened += 1
            if opened > max_paragraphs:
                raise ValueError(f"document.xml has more than {max_paragraphs} paragraphs")
            open_paragraphs.append([])
    paragraphs = (html.unescape(text) for text in finished)
    return "\n".join(text for text in paragraphs if text.strip())
