"""Default document types: invoices, receipts and contracts. A config file can replace them."""

from __future__ import annotations

from .routing import DocType

INVOICE = DocType(
    name="invoice",
    description="A bill from a supplier asking to be paid: an invoice number and an amount due.",
    folder="Invoices/{date:%Y}/{vendor}",
    filename="{date:%Y-%m-%d} {vendor} {number}",
    keywords=("invoice", "amount due", "bill to", "payment terms", "due date"),
    exclude=("quote", "quotation", "estimate", "pro forma", "proforma", "credit note",
             "statement of account"),
    number_pattern=r"invoice\s*(?:no\.?|number|#)\s*:?\s*([A-Z0-9][A-Z0-9-]{2,})",
    min_confidence=0.85,
)

RECEIPT = DocType(
    name="receipt",
    description="Proof of a payment that has already been made.",
    folder="Receipts/{date:%Y-%m}",
    filename="{date:%Y-%m-%d} {vendor} receipt",
    keywords=("receipt", "paid", "payment received", "amount paid",
              "thank you for your purchase"),
    exclude=("quote", "estimate"),
    number_pattern=r"receipt\s*(?:no\.?|number|#)\s*:?\s*([A-Z0-9][A-Z0-9-]{2,})",
    min_confidence=0.85,
)

CONTRACT = DocType(
    name="contract",
    description="An agreement between parties: terms, obligations, signatures.",
    folder="Contracts/{vendor}",
    filename="{date:%Y-%m-%d} {title}",
    keywords=("agreement", "contract", "parties", "term", "signature", "governing law"),
    min_confidence=0.9,
)

DEFAULT_TYPES: dict[str, DocType] = {t.name: t for t in (INVOICE, RECEIPT, CONTRACT)}


def number_patterns(types: dict[str, DocType]) -> list[str]:
    """Every type's number pattern: what the PDF splitter looks for at document boundaries."""
    return [t.number_pattern for t in types.values() if t.number_pattern]
