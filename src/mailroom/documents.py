"""From a message to the documents in it: unpack containers, read text, split batch PDFs.

Every document gets a key built from the message id and where it came from
(`<message>/<attachment>[/<member>][#<pages>]`). Keys are stable across runs, which is what
lets the ledger recognise a document it has already handled after a crash or a re-run.
"""

from __future__ import annotations

import functools
import re
from html.parser import HTMLParser
from pathlib import PurePosixPath

from . import pdf
from .containers import DEFAULT_LIMITS, Item, Limits, docx_text, unpack
from .models import Document, Message
from .split import Splitter, apply_plan


class Expander:
    def __init__(self, *, limits: Limits = DEFAULT_LIMITS, splitter: Splitter | None = None):
        self.limits = limits
        self.splitter = splitter

    def documents(self, message: Message) -> tuple[list[Document], list[tuple[str, str]]]:
        """The message's documents, and (origin, reason) for everything that is not one."""
        docs: list[Document] = []
        skipped = list(message.skipped)
        seen: dict[str, int] = {}
        for att in message.attachments:
            base = re.sub(r"[\\/]+", "_", att.filename).strip() or "attachment"
            count = seen[base] = seen.get(base, 0) + 1
            origin = base if count == 1 else f"{base}#{count}"
            unpacked = unpack(att.filename, att.mime_type, att.data, limits=self.limits,
                              inline=att.inline, origin=origin)
            skipped += unpacked.skipped
            for item in unpacked.items:
                docs += self._from_item(message.id, item)
        return docs, skipped

    def _from_item(self, message_id: str, item: Item) -> list[Document]:
        key = f"{message_id}/{item.origin}"
        flags = item.flags
        text = ""
        if item.kind == "pdf" and not flags:
            try:
                pages = _page_texts(item.data)
            except pdf.PdfUnreadable as err:
                pages, flags = (), ("encrypted_pdf" if err.encrypted else "unreadable_pdf",)
            if self.splitter is not None and len(pages) > 1:
                plan = self.splitter.plan(pages)
                if plan.should_split:
                    try:
                        return self._parts(message_id, key, item, pages, plan)
                    except (pdf.PdfUnreadable, ValueError):
                        flags = ("split_failed",)
                elif len(plan.parts) > 1:
                    flags = ("possible_multiple_documents",)
            text = "\n".join(pages)
        elif item.kind == "docx":
            try:
                text = docx_text(item.data)
            except Exception:
                flags = (*flags, "unreadable_document")
        elif item.kind == "text":
            text = _decode(item.data)
        elif item.kind == "html":
            text = html_text(_decode(item.data))
        return [Document(key, message_id, item.origin, item.filename, item.mime_type,
                         item.data, text=text, kind=item.kind, flags=flags)]

    def _parts(self, message_id, key, item, pages, plan) -> list[Document]:
        name = PurePosixPath(item.filename)
        stem, ext = name.stem or "document", name.suffix or ".pdf"
        out = []
        for cut in apply_plan(item.data, plan):
            label = cut.part.label
            out.append(Document(
                f"{key}#{label}", message_id, f"{item.origin} [{label}]",
                f"{stem} ({label}){ext}", "application/pdf", cut.data,
                text="\n".join(pages[cut.part.first - 1:cut.part.last]), kind="pdf"))
        return out


def _page_texts(data: bytes) -> tuple[str, ...]:
    result = _read_pages(data)
    if isinstance(result, pdf.PdfUnreadable):
        # a fresh exception each time: re-raising the cached one would keep growing its
        # traceback (and the frames it holds) on every run
        raise pdf.PdfUnreadable(str(result), encrypted=result.encrypted)
    return result


@functools.lru_cache(maxsize=512)
def _read_pages(data: bytes) -> tuple[str, ...] | pdf.PdfUnreadable:
    # content-addressed: the same bytes always read the same way, failures included
    try:
        return tuple(pdf.page_texts(data))
    except pdf.PdfUnreadable as err:
        return err


def _decode(data: bytes) -> str:
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = data.decode("latin-1")
    return text.replace("\x00", "").strip()


class _TextOnly(HTMLParser):
    SKIP = frozenset({"script", "style", "head", "title", "noscript"})
    BLOCK = frozenset({"p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "table"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.chunks: list[str] = []
        self._skipping = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skipping += 1
        elif tag in self.BLOCK:
            self.chunks.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self._skipping:
            self._skipping -= 1
        elif tag in self.BLOCK:
            self.chunks.append("\n")

    def handle_data(self, data):
        if not self._skipping:
            self.chunks.append(data)


def html_text(markup: str) -> str:
    """Visible text only: no scripts, styles, attributes or URLs (a link keeps its words)."""
    parser = _TextOnly()
    parser.feed(markup)
    parser.close()
    lines = (" ".join(line.split()) for line in "".join(parser.chunks).splitlines())
    return "\n".join(line for line in lines if line)
