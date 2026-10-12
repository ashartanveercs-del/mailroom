import pytest

from mailroom.evaluation.corpora import CORPUS_NAMES, Corpus, Expected, get_corpus
from mailroom.evaluation.gate import (
    MODES,
    TRANSPORTS,
    GateOptions,
    floor_violations,
    run_cell,
    run_gate,
    score,
)
from mailroom.models import Outcome, OutcomeKind

# --- the scorer itself (so that a passing gate can be trusted) -----------------------------

TINY = Corpus(
    name="tiny", description="hand-made", messages=(), script=(),
    expected={"m1/a.pdf": Expected("filed", "Invoices/a.pdf"),
              "m1/b.pdf": Expected("review", "Needs review/b.pdf", "low_confidence"),
              "m1/logo.png": Expected("skipped", reason="inline_image"),
              "m2/a.pdf": Expected("duplicate")})


def out(key, kind, path="", reason=""):
    folder, _, name = path.rpartition("/")
    return Outcome(key=key, message_id=key.split("/")[0], origin=key, kind=OutcomeKind(kind),
                   reason=reason, folder=tuple(folder.split("/")) if folder else (), name=name)


PERFECT = {"m1/a.pdf": out("m1/a.pdf", "filed", "Invoices/a.pdf"),
           "m1/b.pdf": out("m1/b.pdf", "review", "Needs review/b.pdf", "low_confidence"),
           "m1/logo.png": out("m1/logo.png", "skipped", reason="inline_image"),
           "m2/a.pdf": out("m2/a.pdf", "duplicate", reason="same content as m1/a.pdf")}
TREE = {"Invoices/a.pdf", "Needs review/b.pdf"}


def test_scorer_accepts_outcomes_that_match_exactly():
    assert score(TINY, PERFECT, TREE) == ([], [])


def test_filing_in_the_wrong_place_is_a_misfile():
    outcomes = {**PERFECT, "m1/a.pdf": out("m1/a.pdf", "filed", "Receipts/a.pdf")}
    misfiles, _ = score(TINY, outcomes, {"Receipts/a.pdf", "Needs review/b.pdf"})
    assert any("m1/a.pdf" in m for m in misfiles)


def test_filing_what_should_have_gone_to_review_is_a_misfile():
    outcomes = {**PERFECT, "m1/b.pdf": out("m1/b.pdf", "filed", "Invoices/b.pdf")}
    misfiles, _ = score(TINY, outcomes, {"Invoices/a.pdf", "Invoices/b.pdf"})
    assert any("m1/b.pdf" in m for m in misfiles)


def test_filing_a_duplicate_is_a_misfile():
    outcomes = {**PERFECT, "m2/a.pdf": out("m2/a.pdf", "filed", "Invoices/a_2.pdf")}
    misfiles, _ = score(TINY, outcomes, TREE | {"Invoices/a_2.pdf"})
    assert any("m2/a.pdf" in m for m in misfiles)


def test_an_unneeded_review_is_a_mismatch_not_a_misfile():
    outcomes = {**PERFECT, "m1/a.pdf": out("m1/a.pdf", "review", "Needs review/a.pdf", "x")}
    misfiles, mismatches = score(TINY, outcomes, {"Needs review/a.pdf", "Needs review/b.pdf"})
    assert misfiles == [] and any("m1/a.pdf" in m for m in mismatches)


def test_lost_documents_unexpected_documents_and_stray_files_are_caught():
    outcomes = {k: v for k, v in PERFECT.items() if k != "m1/logo.png"}
    outcomes["m9/x.pdf"] = out("m9/x.pdf", "filed", "Invoices/x.pdf")
    misfiles, mismatches = score(TINY, outcomes, TREE | {"Invoices/x.pdf", "Stray/y.pdf"})
    assert any("m1/logo.png" in m for m in mismatches)
    assert any("m9/x.pdf" in m for m in misfiles)
    assert any("Stray/y.pdf" in m for m in misfiles)


def test_a_wrong_review_reason_is_a_mismatch():
    outcomes = {**PERFECT, "m1/b.pdf": out("m1/b.pdf", "review", "Needs review/b.pdf", "other")}
    assert score(TINY, outcomes, TREE)[1]


def test_floor_violations_are_reported():
    assert floor_violations(writes_outside_root=["x"], mailbox_writes=[], preview_writes=0,
                            bad_ledger_rows=[], changed_owner_files=[]) == [
        "drive write outside the filing root: x"]


# --- the corpora ---------------------------------------------------------------------------

def test_corpora_cover_every_kind_of_outcome():
    kinds = {e.kind for name in CORPUS_NAMES for e in get_corpus(name).expected.values()}
    assert kinds == {"filed", "review", "duplicate", "skipped"}
    for name in CORPUS_NAMES:
        assert len(get_corpus(name).expected) >= 10


def test_the_adversarial_corpus_needs_every_safety_check():
    reasons = {e.reason for e in get_corpus("adversarial").expected.values()
               if e.kind == "review"}
    assert {"ruled_out_by_rules", "classifiers_disagree", "low_confidence",
            "ungrounded_number", "ungrounded_vendor", "ungrounded_date", "missing_date",
            "possible_revision", "classifier_unavailable"} <= reasons


# --- the gate ------------------------------------------------------------------------------

@pytest.mark.parametrize("transport", TRANSPORTS)
@pytest.mark.parametrize("corpus", CORPUS_NAMES)
def test_live_runs_file_every_document_exactly_as_expected(corpus, transport):
    cell = run_cell(corpus, transport, "live")
    assert cell.passed, cell.problems()
    assert cell.documents == len(get_corpus(corpus).expected)


@pytest.mark.parametrize("transport", TRANSPORTS)
@pytest.mark.parametrize("corpus", CORPUS_NAMES)
def test_preview_writes_nothing_and_agrees_with_live(corpus, transport):
    cell = run_cell(corpus, transport, "preview")
    assert cell.passed, cell.problems()


@pytest.mark.parametrize("transport", TRANSPORTS)
@pytest.mark.parametrize("corpus", CORPUS_NAMES)
def test_a_crash_at_any_write_converges_to_the_same_result(corpus, transport):
    cell = run_cell(corpus, transport, "crash")
    assert cell.passed, cell.problems()
    assert cell.crash_points > 10


def test_the_gate_has_teeth():
    """Switch off the cross-check and grounding: the adversarial corpus must now misfile."""
    cell = run_cell("adversarial", "memory", "live",
                    policy_overrides={"cross_check": False, "grounding": False})
    assert not cell.passed and len(cell.misfiles) >= 5


def test_run_gate_reports_every_cell():
    report = run_gate(GateOptions(corpora=("everyday",), transports=("memory",),
                                  modes=MODES))
    assert report.passed and [c.mode for c in report.cells] == list(MODES)
    assert report.to_dict()["totals"]["misfiles"] == 0
