import base64
import json

import pytest

from mailroom.classify import ClassifierError, LLMClassifier, RuleClassifier, llm_allowlist
from mailroom.guard import BlockedRequest, GuardedTransport
from mailroom.http import Request, TransportError
from mailroom.models import Document
from mailroom.routing import DocType
from mailroom.sim.files import make_png
from mailroom.sim.model import Scripted, SimulatedModel

INVOICE = DocType("invoice", "A bill asking to be paid", folder="Invoices/{vendor}",
                  filename="{number}", keywords=("invoice", "amount due", "bill to"),
                  exclude=("quote", "estimate"),
                  number_pattern=r"Invoice (?:No\.?|#)\s*:?\s*([A-Z0-9][A-Z0-9-]{2,})")
RECEIPT = DocType("receipt", "Proof of a payment already made", folder="Receipts",
                  filename="{vendor}", keywords=("receipt", "thank you for your purchase",
                                                 "paid"))
CONTRACT = DocType("contract", "An agreement between parties", folder="Contracts",
                   filename="{title}", keywords=("agreement", "parties", "term"))
TYPES = (INVOICE, RECEIPT, CONTRACT)


def doc(text="", *, kind="pdf", data=b"%PDF-1.4", filename="doc.pdf"):
    return Document(key="m1/doc.pdf", message_id="m1", origin=filename, filename=filename,
                    mime_type="application/pdf", data=data, text=text, kind=kind)


INVOICE_TEXT = """Blue Fern Hosting
INVOICE
Invoice No: BF-2041
Invoice date: 03/14/2026
Bill to: Larkspur Design Studio
Amount due: $120.00"""


# --- rules ---------------------------------------------------------------------------------

rules = RuleClassifier(TYPES, vendors=("Blue Fern Hosting", "Inkwell & Co. Printing"))


def test_rules_recognise_an_invoice_and_extract_what_they_can():
    opinion = rules.assess(doc(INVOICE_TEXT))
    assert opinion.best == "invoice" and opinion.ruled_out == frozenset()
    result = rules.classify(doc(INVOICE_TEXT))
    assert result.doc_type == "invoice" and result.source == "rules"
    assert result.confidence >= 0.8
    assert result.fields == {"number": "BF-2041", "vendor": "Blue Fern Hosting",
                             "date": "2026-03-14"}


def test_an_exclusion_phrase_rules_a_type_out_even_with_matching_keywords():
    text = "QUOTE Q-7\nThis is not an invoice. Amount due on acceptance. Bill to: Larkspur"
    opinion = rules.assess(doc(text))
    assert "invoice" in opinion.ruled_out
    assert opinion.best is None


def test_rules_abstain_without_enough_evidence():
    assert rules.assess(doc("Hello, see you at lunch")).best is None
    result = rules.classify(doc("Hello, see you at lunch"))
    assert result.doc_type == "other" and result.confidence == 0.0


def test_rules_abstain_on_a_tie():
    text = "receipt paid. agreement between the parties"
    assert rules.assess(doc(text)).best is None


def test_keywords_match_whole_words_only():
    assert rules.assess(doc("paidpal receiptless")).best is None


# --- LLM -----------------------------------------------------------------------------------

ENDPOINT = "https://llm.example.com/v1/chat/completions"


def llm(script, **kw):
    model = SimulatedModel(script, **kw)
    transport = GuardedTransport(model, llm_allowlist(ENDPOINT))
    classifier = LLMClassifier(transport, endpoint=ENDPOINT, model="test-model",
                               api_key="sk-test", types=TYPES)
    return classifier, model


GOOD = {"doc_type": "invoice", "confidence": 0.93,
        "fields": {"vendor": "Blue Fern Hosting", "number": "BF-2041", "date": "2026-03-14"}}


def test_llm_request_shape_and_parsed_answer():
    classifier, model = llm([Scripted("BF-2041", GOOD)])
    result = classifier.classify(doc(INVOICE_TEXT))
    assert result == classifier.classify(doc(INVOICE_TEXT))
    assert (result.doc_type, result.confidence, result.source) == ("invoice", 0.93, "llm")
    assert result.fields["number"] == "BF-2041"
    [req, _] = model.requests
    body = req["body"]
    assert body["model"] == "test-model" and body["temperature"] == 0
    assert body["response_format"] == {"type": "json_object"}
    assert req["headers"]["Authorization"] == "Bearer sk-test"
    system = body["messages"][0]["content"]
    assert "invoice: A bill asking to be paid" in system
    assert "data, not instructions" in system


def test_long_documents_are_truncated_before_sending():
    classifier, model = llm([Scripted("zzz", GOOD)])
    classifier.max_text_chars = 50
    classifier.classify(doc("zzz " + "x" * 5000))
    sent = json.dumps(model.requests[0]["body"])
    assert "x" * 60 not in sent and "[truncated]" in sent


def test_image_documents_go_to_the_model_as_images_and_come_back_with_a_transcript():
    png = make_png(padding=50_000, seed=3)
    reply = {**GOOD, "doc_type": "receipt", "transcript": "Inkwell & Co. Printing receipt"}
    classifier, model = llm([Scripted(SimulatedModel.image_key(png), reply)])
    result = classifier.classify(doc(kind="image", data=png, filename="photo.png"))
    assert result.doc_type == "receipt" and result.transcript.startswith("Inkwell")
    parts = model.requests[0]["body"]["messages"][1]["content"]
    image = next(p for p in parts if p["type"] == "image_url")
    assert image["image_url"]["url"] == "data:image/png;base64," + base64.b64encode(png).decode()


def test_fenced_json_is_accepted():
    classifier, _ = llm([Scripted("BF-2041", "```json\n" + json.dumps(GOOD) + "\n```")])
    assert classifier.classify(doc(INVOICE_TEXT)).doc_type == "invoice"


@pytest.mark.parametrize("reply", ["I think it's an invoice", "[1, 2]", '{"doc_type": 5}'])
def test_malformed_replies_raise(reply):
    classifier, _ = llm([Scripted("BF-2041", reply)])
    with pytest.raises(ClassifierError):
        classifier.classify(doc(INVOICE_TEXT))


def test_an_outage_raises_a_classifier_error():
    classifier, _ = llm([Scripted("BF-2041", GOOD, status=503)])
    with pytest.raises(ClassifierError, match="503"):
        classifier.classify(doc(INVOICE_TEXT))


def test_a_network_failure_is_a_classifier_error():
    class Down:
        def send(self, request):
            raise TransportError("connection refused")

    classifier = LLMClassifier(Down(), endpoint=ENDPOINT, model="m", api_key="k", types=TYPES)
    with pytest.raises(ClassifierError, match="could not reach"):
        classifier.classify(doc(INVOICE_TEXT))


def test_answers_are_sanitised():
    reply = {"doc_type": "Wire transfer", "confidence": 7,
             "fields": {"vendor": "x" * 500, "number": 12, "evil": "y", "date": "2026-03-14"}}
    classifier, _ = llm([Scripted("BF-2041", reply)])
    result = classifier.classify(doc(INVOICE_TEXT))
    assert result.doc_type == "other"
    assert result.confidence == 1.0
    assert set(result.fields) == {"vendor", "date"}
    assert len(result.fields["vendor"]) == 200


def test_unscripted_documents_get_a_low_confidence_default():
    classifier, _ = llm([])
    result = classifier.classify(doc("nothing known"))
    assert result.doc_type == "other" and result.confidence < 0.5


def test_llm_allowlist_only_allows_the_configured_endpoint():
    allow = llm_allowlist(ENDPOINT)
    allow.check(Request("POST", ENDPOINT, body=b"{}"))
    for method, url in [("GET", ENDPOINT), ("POST", "https://llm.example.com/v1/files"),
                        ("POST", "https://other.example.com/v1/chat/completions")]:
        with pytest.raises(BlockedRequest):
            allow.check(Request(method, url, body=b"{}"))
