import pytest

from mailroom.classify import RuleOpinion
from mailroom.models import Classification, Document
from mailroom.routing import (
    DocType,
    Policy,
    TemplateError,
    UnsafeName,
    decide,
    identity_of,
    render_folder,
    sanitize_segment,
    template_fields,
)

INVOICE = DocType("invoice", "A bill", folder="Invoices/{date:%Y}/{vendor}",
                  filename="{date:%Y-%m-%d} {vendor} {number}")
CONTRACT = DocType("contract", "An agreement", folder="Contracts/{vendor}",
                   filename="{title}", min_confidence=0.9)
POLICY = Policy(types={"invoice": INVOICE, "contract": CONTRACT})

TEXT = """Blue Fern Hosting
INVOICE  Invoice No: BF-2041   Invoice date: 03/14/2026
Amount due: $120.00"""
FIELDS = {"vendor": "Blue Fern Hosting", "number": "BF-2041", "date": "2026-03-14"}


def doc(text=TEXT, *, flags=(), filename="scan 1.PDF", kind="pdf"):
    return Document(key="m1/x", message_id="m1", origin=filename, filename=filename,
                    mime_type="application/pdf", data=b"x", text=text, kind=kind, flags=flags)


def verdict(doc_type="invoice", confidence=0.95, transcript="", **fields):
    return Classification(doc_type, confidence, {**FIELDS, **fields}, "llm", transcript)


def review(reason, name="scan 1.pdf"):
    return ("Needs review",), name, True, reason


def as_tuple(dest):
    return dest.folder, dest.name, dest.review, dest.reason


# --- templates and names -------------------------------------------------------------------

def test_template_fields():
    assert template_fields("Invoices/{date:%Y}/{vendor}") == {"date", "vendor"}
    assert template_fields("Receipts") == set()


@pytest.mark.parametrize("template", [
    "{vendor.__class__}", "{amount}", "{vendor:>20}", "{date:%H:%M}", "Bad {brace", "{}",
])
def test_unsafe_or_unknown_templates_are_rejected(template):
    with pytest.raises(TemplateError):
        Policy(types={"x": DocType("x", "", folder=template, filename="{number}")})


def test_values_cannot_add_folder_levels():
    folder = render_folder("Invoices/{vendor}", {"vendor": "A/B Testing\\Co"})
    assert folder == ("Invoices", "A-B Testing-Co")


@pytest.mark.parametrize("raw,clean", [
    ("  Blue   Fern  ", "Blue Fern"),
    ('What: "Q1"?', "What- -Q1--"),
    ("tab\there\x00", "tabhere"),
    ("report.", "report"),
])
def test_sanitize_segment(raw, clean):
    assert sanitize_segment(raw) == clean


@pytest.mark.parametrize("raw", ["", "  ", "..", ". .", "\x00"])
def test_names_that_sanitise_to_nothing_are_refused(raw):
    with pytest.raises(UnsafeName):
        sanitize_segment(raw)


def test_long_names_are_capped():
    assert len(sanitize_segment("x" * 300)) == 100


# --- decisions -----------------------------------------------------------------------------

def test_a_confident_grounded_invoice_is_filed_by_its_templates():
    dest = decide(doc(), verdict(), RuleOpinion("invoice"), POLICY)
    assert as_tuple(dest) == (("Invoices", "2026", "Blue Fern Hosting"),
                              "2026-03-14 Blue Fern Hosting BF-2041.pdf", False, "")


@pytest.mark.parametrize("case,expected_reason", [
    (dict(v=verdict(confidence=0.6)), "low_confidence"),
    (dict(v=verdict(doc_type="other")), "unrecognized_type"),
    (dict(v=verdict(doc_type="memo")), "unrecognized_type"),
    (dict(d=doc(flags=("possible_multiple_documents",))), "possible_multiple_documents"),
    (dict(o=RuleOpinion("contract")), "classifiers_disagree"),
    (dict(o=RuleOpinion(None, frozenset({"invoice"}))), "ruled_out_by_rules"),
    (dict(v=verdict(vendor="")), "missing_vendor"),
    (dict(v=verdict(number="BF-2042")), "ungrounded_number"),
    (dict(v=verdict(number="BF-204")), "ungrounded_number"),
    (dict(v=verdict(vendor="Blue Fern Cloud")), "ungrounded_vendor"),
    (dict(v=verdict(date="2026-04-14")), "ungrounded_date"),
    (dict(v=verdict(date="14/03/2026")), "invalid_date"),
    (dict(d=doc(text="")), "no_text"),
])
def test_anything_short_of_certain_goes_to_review_with_a_reason(case, expected_reason):
    dest = decide(case.get("d", doc()), case.get("v", verdict()), case.get("o"), POLICY)
    assert as_tuple(dest) == review(expected_reason)


def test_rules_abstaining_is_not_a_disagreement():
    dest = decide(doc(), verdict(), RuleOpinion(None), POLICY)
    assert not dest.review


def test_cross_check_and_grounding_can_be_switched_off():
    loose = Policy(types=POLICY.types, cross_check=False, grounding=False)
    dest = decide(doc(), verdict(number="BF-9999"), RuleOpinion("contract"), loose)
    assert not dest.review and dest.name.endswith("BF-9999.pdf")


def test_image_only_documents_are_grounded_in_the_transcript_and_need_more_confidence():
    image = doc(text="", kind="image", filename="photo.png")
    unsure = verdict(confidence=0.9, transcript=TEXT)
    assert as_tuple(decide(image, unsure, None, POLICY)) == review("low_confidence",
                                                                   "photo.png")
    sure = verdict(confidence=0.97, transcript=TEXT)
    assert not decide(image, sure, None, POLICY).review


def test_a_value_that_would_make_an_unsafe_name_goes_to_review():
    # With grounding on, ".." can never be grounded; this is the layer behind it.
    loose = Policy(types=POLICY.types, grounding=False)
    dest = decide(doc(), verdict(doc_type="contract", title=".."), None, loose)
    assert as_tuple(dest) == review("unsafe_name")
    assert as_tuple(decide(doc(), verdict(doc_type="contract", title=".."), None, POLICY)) == (
        review("ungrounded_title"))


def test_review_names_keep_the_original_file_name():
    dest = decide(doc(filename="weird:name?.pdf"), verdict(confidence=0.1), None, POLICY)
    assert dest.name == "weird-name-.pdf"


def test_identity_ties_a_document_to_its_issuer_and_number():
    assert identity_of(verdict()) == "invoice|blue fern hosting|bf 2041"
    assert identity_of(Classification("receipt", 0.9, {"vendor": "X"})) is None
