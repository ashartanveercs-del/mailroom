"""Where a document goes: the decision policy between a classifier's answer and a filing.

`decide` turns a proposal into a destination only when every check passes:

  * the document was opened cleanly (no flags from unpacking or splitting)
  * the type is one the config knows, at or above that type's confidence threshold
    (a higher bar for image-only documents, whose only text is the model's own reading)
  * the rule classifier, if it has an opinion, agrees, and has not ruled the type out
  * every field the folder and file-name templates use is present and *grounded*: it appears
    in the document's own text (dates in the configured day/month order), and in its own
    role: not only right after a label that gives it another one (the customer after "Bill
    to" is not the vendor, a deadline after "Due" is not the document's date)
  * the vendor is not one of the owner's own names (the most common wrong pick on a bill)

Anything else goes to the review folder with the first reason that failed. Templates are a
tiny format of their own, not `str.format`, so a value can never reach attributes or add
folder levels, and every rendered segment is sanitised.
"""

from __future__ import annotations

import mimetypes
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

from .models import Classification, Destination, Document
from .textutil import contains_phrase, date_spans, find_dates, normalize

if TYPE_CHECKING:
    from .classify import RuleOpinion

TEMPLATE_FIELDS = frozenset({"vendor", "date", "number", "title", "total", "type"})
_PLACEHOLDER = re.compile(r"\{([^{}]*)\}")
_DATE_SPEC = re.compile(r"(?:%[YmdBby]|[^%{}])+")
MAX_SEGMENT = 100

# Labels that put a value in a role other than the one its field needs: a name found only
# after "Bill to" is the customer, not the vendor; a date found only after "Due" or "Valid
# until" is a deadline, not the date of the document. Only labels that cannot mean the
# field's own role are listed ("Client" can name the other party of a contract, which is
# what the vendor field holds there; "Payment date" is a receipt's own date). English, like
# the default types.
OTHER_ROLE_LABELS: Mapping[str, tuple[str, ...]] = {
    "vendor": ("bill to", "billed to", "invoice to", "sold to", "ship to", "shipped to",
               "deliver to", "delivered to", "customer"),
    "date": ("due", "due date", "pay by", "valid until", "valid till", "expires", "expiry",
             "expiry date", "expiration date", "delivery date", "ship date", "shipped",
             "renewal date"),
}


def _label_pattern(labels: tuple[str, ...]) -> re.Pattern[str]:
    # at the end of the normalised text in front of a value, maybe followed by on/by/at
    words = sorted({re.escape(normalize(label)) for label in labels}, key=len, reverse=True)
    return re.compile(rf"(?:^|\s)(?:{'|'.join(words)})(?:\s(?:on|by|at))?$")


_OTHER_ROLE = {name: _label_pattern(labels) for name, labels in OTHER_ROLE_LABELS.items()}


class TemplateError(ValueError):
    pass


class UnsafeName(ValueError):
    pass


@dataclass(frozen=True)
class DocType:
    name: str
    description: str
    folder: str                       # e.g. "Invoices/{date:%Y}/{vendor}"
    filename: str                     # e.g. "{date:%Y-%m-%d} {vendor} {number}"
    keywords: tuple[str, ...] = ()    # phrases that point to this type (rule classifier)
    exclude: tuple[str, ...] = ()     # phrases that rule this type out ("quote", "estimate")
    number_pattern: str | None = None  # one capture group: the document number
    min_confidence: float = 0.85

    def fields(self) -> set[str]:
        return template_fields(self.folder) | template_fields(self.filename)


def template_fields(template: str) -> set[str]:
    """The fields a template uses. Raises TemplateError for anything outside the tiny format:
    `{field}` or `{date:<strftime with %Y %m %d %B %b %y only>}`."""
    if "{" in _PLACEHOLDER.sub("", template) or "}" in _PLACEHOLDER.sub("", template):
        raise TemplateError(f"unbalanced braces in {template!r}")
    fields = set()
    for body in _PLACEHOLDER.findall(template):
        name, _, spec = body.partition(":")
        if name not in TEMPLATE_FIELDS:
            raise TemplateError(f"unknown field {name!r} in {template!r}")
        if spec and (name != "date" or not _DATE_SPEC.fullmatch(spec)):
            raise TemplateError(f"format {spec!r} is not allowed for {name!r}")
        fields.add(name)
    return fields


def _render(template: str, values: Mapping[str, object]) -> str:
    def one(match: re.Match[str]) -> str:
        name, _, spec = match.group(1).partition(":")
        value = values[name]
        if isinstance(value, date):
            return value.strftime(spec) if spec else value.isoformat()
        return str(value)

    return _PLACEHOLDER.sub(one, template)


def sanitize_segment(raw: str, max_length: int = MAX_SEGMENT) -> str:
    """A safe folder or file name: no separators, reserved characters or control characters,
    no leading/trailing dots or spaces, at most `max_length` characters."""
    text = unicodedata.normalize("NFC", raw)
    text = re.sub(r"[\x00-\x1f\x7f]", "", text)
    text = re.sub(r'[/\\:*?"<>|]', "-", text)
    text = " ".join(text.split()).strip(" .")
    text = text[:max_length].rstrip(" .")
    if not text:
        raise UnsafeName(f"{raw!r} has no usable characters")
    return text


def render_folder(template: str, values: Mapping[str, object]) -> tuple[str, ...]:
    # split first, so a value containing "/" can never add a level
    return tuple(sanitize_segment(_render(part, values))
                 for part in template.split("/") if part.strip())


def _extension(doc: Document) -> str:
    suffix = PurePosixPath(doc.filename).suffix.lower()
    if re.fullmatch(r"\.[a-z0-9]{1,5}", suffix):
        return suffix
    return mimetypes.guess_extension(doc.mime_type or "") or ""


def review_name(doc: Document) -> str:
    stem = PurePosixPath(doc.filename).stem if PurePosixPath(doc.filename).suffix \
        else doc.filename
    try:
        return sanitize_segment(stem) + _extension(doc)
    except UnsafeName:
        return "document" + _extension(doc)


@dataclass(frozen=True)
class Policy:
    types: Mapping[str, DocType]
    review_folder: str = "Needs review"
    cross_check: bool = True
    grounding: bool = True
    date_order: str = "MDY"
    vision_min_confidence: float = 0.95
    owner_names: tuple[str, ...] = ()  # the owner's own business: never anyone's vendor

    def __post_init__(self) -> None:
        for spec in self.types.values():
            spec.fields()  # raises TemplateError
        sanitize_segment(self.review_folder)
        if self.date_order not in ("MDY", "DMY"):
            raise ValueError("date_order must be MDY or DMY")


def _grounded(name: str, value: str, evidence: str, order: str) -> bool:
    if name == "date":
        return date.fromisoformat(value) in find_dates(evidence, order)
    return contains_phrase(evidence, value)


def _lead_ins(name: str, value: str, line: str, order: str) -> list[str]:
    """The normalised text in front of each place `value` appears on `line`."""
    if name == "date":
        wanted = date.fromisoformat(value)
        return [normalize(line[:start]) for start, found in date_spans(line, order)
                if found == wanted]
    text, needle = normalize(line), normalize(value)
    if not needle:
        return []
    return [text[:m.start()].rstrip()
            for m in re.finditer(rf"(?<!\w){re.escape(needle)}(?!\w)", text)]


def _only_in_another_role(name: str, value: str, evidence: str, order: str) -> bool:
    """True if `value` appears in `evidence` and every time right after a label for another
    role: on its own line, or on the line above when the value starts its line (a label set
    above its value). One appearance in no such place is enough to be in its own role."""
    pattern = _OTHER_ROLE.get(name)
    if pattern is None:
        return False
    seen, above = False, ""
    for line in evidence.splitlines():
        for lead in _lead_ins(name, value, line, order):
            seen = True
            if not pattern.search(lead or above):
                return False
        above = normalize(line) or above
    return seen


def _is_owner(value: str, owner_names: tuple[str, ...]) -> bool:
    return any(contains_phrase(value, owner) or contains_phrase(owner, value)
               for owner in owner_names)


def decide(doc: Document, verdict: Classification, opinion: RuleOpinion | None,
           policy: Policy) -> Destination:
    def review(reason: str) -> Destination:
        return Destination((policy.review_folder,), review_name(doc), review=True,
                           reason=reason)

    if doc.flags:
        return review(doc.flags[0])
    spec = policy.types.get(verdict.doc_type)
    if spec is None:
        return review("unrecognized_type")
    own_text = doc.text.strip()
    evidence = own_text or verdict.transcript.strip()
    if not evidence:
        return review("no_text")
    threshold = spec.min_confidence if own_text else max(spec.min_confidence,
                                                         policy.vision_min_confidence)
    if verdict.confidence < threshold:
        return review("low_confidence")
    if policy.cross_check and opinion is not None:
        if verdict.doc_type in opinion.ruled_out:
            return review("ruled_out_by_rules")
        if opinion.best is not None and opinion.best != verdict.doc_type:
            return review("classifiers_disagree")
    values: dict[str, object] = {"type": spec.name}
    for name in sorted(spec.fields() - {"type"}):
        value = (verdict.fields.get(name) or "").strip()
        if not value:
            return review(f"missing_{name}")
        if name == "date":
            try:
                values[name] = date.fromisoformat(value)
            except ValueError:
                return review("invalid_date")
        else:
            values[name] = value
        if policy.grounding and not _grounded(name, value, evidence, policy.date_order):
            return review(f"ungrounded_{name}")
        if name == "vendor" and _is_owner(value, policy.owner_names):
            return review("vendor_is_owner")
        if policy.grounding and _only_in_another_role(name, value, evidence,
                                                      policy.date_order):
            return review(f"wrong_role_{name}")
    try:
        folder = render_folder(spec.folder, values)
        name = sanitize_segment(_render(spec.filename, values), 120) + _extension(doc)
    except UnsafeName:
        return review("unsafe_name")
    return Destination(folder, name)


def identity_of(verdict: Classification) -> str | None:
    """Type, issuer and number: two different files with the same identity are probably two
    versions of one document (a corrected invoice), which a person should look at."""
    number = verdict.fields.get("number")
    if not number:
        return None
    return "|".join((verdict.doc_type, normalize(verdict.fields.get("vendor", "")),
                     normalize(number)))
