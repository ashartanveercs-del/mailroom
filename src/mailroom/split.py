"""Splitting one PDF that holds several documents (a batch scan, a merged export).

Splitting is pluggable: a `Splitter` looks at the text of each page and returns a `SplitPlan`.
The pipeline only cuts when the plan is *confident*; an unsure plan leaves the file whole and
flags it, so it goes to review instead of being cut in the wrong place. A wrong cut is worse
than no cut: it produces documents that never existed and files them confidently.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Protocol

from . import pdf

PAGE_ONE = re.compile(r"\bpage\s+1\s+of\s+\d+\b", re.IGNORECASE)


@dataclass(frozen=True)
class Part:
    first: int  # 1-based, inclusive
    last: int

    @property
    def label(self) -> str:
        return f"p{self.first}" if self.first == self.last else f"p{self.first}-{self.last}"


@dataclass(frozen=True)
class SplitPlan:
    parts: tuple[Part, ...]
    confident: bool
    reason: str = ""

    @property
    def should_split(self) -> bool:
        return self.confident and len(self.parts) > 1


class Splitter(Protocol):
    def plan(self, page_texts: Sequence[str]) -> SplitPlan: ...


class NumberSplitter:
    """A page starts a new document when it shows a document number that shares nothing with
    the numbers seen so far in the current document, or when it says "Page 1 of N".

    The plan is confident only if every part's first page shows a number and every page has
    text. A page without text (a scanned image with no text layer, a blank separator) could
    hide a boundary, so the plan is not trusted."""

    def __init__(self, number_patterns: Iterable[str]) -> None:
        self.patterns = [re.compile(p, re.IGNORECASE) for p in number_patterns]

    def numbers(self, text: str) -> set[str]:
        found: set[str] = set()
        for pattern in self.patterns:
            for match in pattern.finditer(text):
                found.add((match.group(1) if match.groups() else match.group(0)).upper())
        return found

    def plan(self, page_texts: Sequence[str]) -> SplitPlan:
        if not page_texts:
            return SplitPlan((), confident=False, reason="no_pages")
        starts: list[int] = []
        numbered: list[bool] = []
        current: set[str] = set()
        for index, text in enumerate(page_texts, start=1):
            found = self.numbers(text)
            # a number after unnumbered pages, or one sharing nothing with this document's
            new_number = bool(found) and not (found & current)
            page_one = bool(PAGE_ONE.search(text))
            if not starts or new_number or page_one:
                starts.append(index)
                numbered.append(bool(found))
                current = set(found)
            else:
                current |= found
        ends = [s - 1 for s in starts[1:]] + [len(page_texts)]
        parts = tuple(Part(s, e) for s, e in zip(starts, ends, strict=True))
        if len(parts) > 1 and not all(numbered):
            return SplitPlan(parts, confident=False, reason="unnumbered_part")
        if len(parts) > 1 and any(not t.strip() for t in page_texts):
            return SplitPlan(parts, confident=False, reason="page_without_text")
        return SplitPlan(parts, confident=True)


@dataclass(frozen=True)
class CutPart:
    part: Part
    data: bytes


def apply_plan(data: bytes, plan: SplitPlan) -> list[CutPart]:
    blobs = pdf.cut(data, [(p.first, p.last) for p in plan.parts])
    return [CutPart(part, blob) for part, blob in zip(plan.parts, blobs, strict=True)]
