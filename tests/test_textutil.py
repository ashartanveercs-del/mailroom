from datetime import date

import pytest

from mailroom.textutil import contains_phrase, find_dates, normalize


@pytest.mark.parametrize("text,order,expected", [
    ("Date: 2026-03-14", "MDY", [date(2026, 3, 14)]),
    ("Invoice date: 03/14/2026", "MDY", [date(2026, 3, 14)]),
    ("Invoice date: 14/03/2026", "DMY", [date(2026, 3, 14)]),
    ("Due 04.05.2026", "DMY", [date(2026, 5, 4)]),
    ("March 14, 2026", "MDY", [date(2026, 3, 14)]),
    ("14 March 2026", "MDY", [date(2026, 3, 14)]),
    ("Sept. 3, 2026 and Jan 2 2027", "MDY", [date(2026, 9, 3), date(2027, 1, 2)]),
    ("13/14/2026", "MDY", []),                    # impossible dates are not dates
    ("Ref 2026-02-30", "MDY", []),
    ("phone 555-0100", "MDY", []),
])
def test_find_dates(text, order, expected):
    assert find_dates(text, order) == expected


def test_ambiguous_numeric_dates_follow_the_configured_order_only():
    assert find_dates("03/04/2026", "MDY") == [date(2026, 3, 4)]
    assert find_dates("03/04/2026", "DMY") == [date(2026, 4, 3)]


def test_normalize_folds_case_punctuation_and_ampersands():
    assert normalize("Larkspur &  Fern, LLC.") == "larkspur and fern llc"


def test_contains_phrase_respects_word_boundaries():
    text = "Invoice No: INV-2041 from Blue Fern Hosting"
    assert contains_phrase(text, "blue fern hosting")
    assert contains_phrase(text, "INV-2041")
    assert not contains_phrase(text, "INV-204")      # a truncated number is not grounded
    assert not contains_phrase(text, "Fern Host")
    assert not contains_phrase(text, "")
