from mailroom.pdf import page_texts
from mailroom.sim.pdfgen import make_pdf
from mailroom.split import NumberSplitter, Part, apply_plan

PATTERNS = [r"Invoice (?:No\.?|Number|#)\s*:?\s*([A-Z0-9][A-Z0-9-]{2,})",
            r"Receipt\s*#\s*:?\s*([A-Z0-9][A-Z0-9-]{2,})"]
splitter = NumberSplitter(PATTERNS)


def test_one_document_over_several_pages_is_not_split():
    plan = splitter.plan(["Invoice No: INV-1", "Line items continued", "Total due"])
    assert plan.parts == (Part(1, 3),)
    assert plan.confident and not plan.should_split


def test_each_new_number_starts_a_new_document():
    plan = splitter.plan(["Invoice No: INV-1", "continued", "Invoice No: INV-2",
                          "Receipt #: R-300"])
    assert plan.parts == (Part(1, 2), Part(3, 3), Part(4, 4))
    assert plan.confident and plan.should_split


def test_the_same_number_on_a_later_page_continues_the_document():
    plan = splitter.plan(["Invoice No: INV-1", "Invoice No: INV-1 (continued)"])
    assert plan.parts == (Part(1, 2),)


def test_a_page_sharing_any_number_with_the_current_document_continues_it():
    # a statement page that lists two invoice numbers is one document, not two
    plan = splitter.plan(["Invoice No: INV-1 Invoice No: INV-2", "Invoice No: INV-2"])
    assert plan.parts == (Part(1, 2),)


def test_page_one_of_n_marker_starts_a_new_document_even_without_a_new_number():
    plan = splitter.plan(["Invoice No: INV-9 Page 1 of 1", "Invoice No: INV-9 Page 1 of 1"])
    assert plan.parts == (Part(1, 1), Part(2, 2))
    assert plan.confident


def test_leading_pages_without_a_number_make_the_plan_unsure():
    plan = splitter.plan(["Cover letter", "Invoice No: INV-1", "Invoice No: INV-2"])
    assert plan.parts == (Part(1, 1), Part(2, 2), Part(3, 3))
    assert not plan.confident and plan.reason == "unnumbered_part"
    assert not plan.should_split


def test_a_page_without_text_makes_the_plan_unsure():
    plan = splitter.plan(["Invoice No: INV-1", "", "Invoice No: INV-2"])
    assert not plan.confident and plan.reason == "page_without_text"


def test_apply_plan_cuts_the_pdf_into_parts():
    data = make_pdf([["Invoice No: INV-1"], ["more"], ["Invoice No: INV-2"]])
    plan = splitter.plan(page_texts(data))
    parts = apply_plan(data, plan)
    assert [p.part for p in parts] == [Part(1, 2), Part(3, 3)]
    assert len(page_texts(parts[0].data)) == 2
    assert "INV-2" in page_texts(parts[1].data)[0]


def test_part_labels_are_human_readable():
    assert Part(1, 2).label == "p1-2"
    assert Part(3, 3).label == "p3"
