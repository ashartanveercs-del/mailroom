"""Small text helpers shared by the rule classifier and the grounding checks."""

from __future__ import annotations

import functools
import re
from datetime import date

_MONTHS = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "jul": 7, "aug": 8,
           "sep": 9, "oct": 10, "nov": 11, "dec": 12}
_MONTH = (r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?"
          r"|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\.?")
_ISO = re.compile(r"(?<!\d)(\d{4})-(\d{1,2})-(\d{1,2})(?!\d)")
_NUMERIC = re.compile(r"(?<![\d/.\-])(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{4})(?!\d)")
_MONTH_FIRST = re.compile(rf"\b{_MONTH}\s+(\d{{1,2}})(?:st|nd|rd|th)?,?\s+(\d{{4}})\b",
                          re.IGNORECASE)
_DAY_FIRST = re.compile(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+{_MONTH},?\s+(\d{{4}})\b",
                        re.IGNORECASE)


def _make(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def find_dates(text: str, order: str = "MDY") -> list[date]:
    """Calendar dates written in `text`, in order of appearance, without repeats.

    Numeric dates like 03/04/2026 are read in the configured `order` only ("MDY" or "DMY"):
    guessing between the two is exactly how a document ends up in the wrong month."""
    found: list[tuple[int, date | None]] = []
    for m in _ISO.finditer(text):
        found.append((m.start(), _make(int(m[1]), int(m[2]), int(m[3]))))
    for m in _NUMERIC.finditer(text):
        a, b, year = int(m[1]), int(m[2]), int(m[3])
        month, day = (a, b) if order == "MDY" else (b, a)
        found.append((m.start(), _make(year, month, day)))
    for m in _MONTH_FIRST.finditer(text):
        found.append((m.start(), _make(int(m[3]), _MONTHS[m[1][:3].lower()], int(m[2]))))
    for m in _DAY_FIRST.finditer(text):
        found.append((m.start(), _make(int(m[3]), _MONTHS[m[2][:3].lower()], int(m[1]))))
    out: list[date] = []
    for _, value in sorted(found, key=lambda pair: pair[0]):
        if value is not None and value not in out:
            out.append(value)
    return out


@functools.lru_cache(maxsize=4096)
def normalize(text: str) -> str:
    """Case-folded words: '&' reads as 'and', punctuation becomes spaces."""
    text = text.casefold().replace("&", " and ")
    return " ".join(re.sub(r"[^\w]+", " ", text).split())


def contains_phrase(text: str, phrase: str) -> bool:
    """True if `phrase` appears in `text` as whole words (after normalising both)."""
    needle = normalize(phrase)
    if not needle:
        return False
    return re.search(rf"(?<!\w){re.escape(needle)}(?!\w)", normalize(text)) is not None
