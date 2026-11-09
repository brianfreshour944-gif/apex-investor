"""G2 answer validation (measure + period + value) — regression tests.

The M4 gate is lexical: it keeps a finding when the claim's terms are grounded in the cited quote,
but it never checks the claim's NUMBER against the authoritative reference value, nor that the cited
evidence is the requested measure for the requested period. On the G2 financial corpus that let a
segment subtotal or a prior-year column score as correct. `score_scenario` now requires all three.

These tests run the REAL evaluator over the REAL source-controlled G2 corpora (offline; no model, no
network). Fail closed: a finding that cannot establish measure, period, AND value does not count as
support, so the scenario scores incorrect.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from apex_investor.model_gate import (
    ScenarioResult,
    _g2_answer_supported,
    _g2_measure_terms,
    _g2_period_tokens,
    _integer_values,
    materialize_corpus,
    score_scenario,
)

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data" / "model_gate"
DEV = json.loads((DATA / "g2_corpus.dev.json").read_text())
HOLD = json.loads((DATA / "g2_corpus.heldout.json").read_text())
SPEC = json.loads((DATA / "detector_spec.v1.json").read_text())


def _scenario(corpus: dict, scenario_id: str) -> dict:
    return next(s for s in corpus["scenarios"] if s["id"] == scenario_id)


def _score(tmp_path: Path, corpus: dict, scenario: dict, claim: str,
           quotes: list[str]) -> ScenarioResult:
    """Score one finding (claim + citations) against a scenario via the real evaluator."""
    store, sym = materialize_corpus(corpus, tmp_path)
    doc = scenario["documents"][0]
    doc_id = sym[doc["sym"]]
    citations = [
        {"doc_id": doc_id, "quote_span": q, "source_url": doc["source_url"]} for q in quotes
    ]
    finding = {
        "finding_id": "f1", "ticker": scenario["ticker"], "created_at": "2026-11-03",
        "claim": claim, "citations": citations, "reason": "r", "falsifier": "x",
        "confidence": 0.6, "source_docs": [doc_id],
    }
    raw = json.dumps({"findings": [finding]})
    return score_scenario(scenario, store, {"status": "ok", "raw": raw},
                          model="m", prompt_version="g2-contract-v1", spec=SPEC)


# ---- required regression cases (real corpus) ------------------------------------------------------

HOLD02 = _scenario(HOLD, "HOLD-02")
DEV03 = _scenario(DEV, "DEV-03")
TOTAL_ROW = "Total net sales $ 478,834 15 % $ 416,161"


def test_hold02_correct_total_net_sales_passes(tmp_path):
    res = _score(tmp_path, HOLD, HOLD02, "Total net sales 478,834", [TOTAL_ROW])
    assert res.correct is True and res.supported == 1


def test_hold02_americas_subtotal_fails(tmp_path):
    res = _score(tmp_path, HOLD, HOLD02, "Americas 198,083", ["Americas $ 198,083 11 %"])
    assert res.correct is False and res.supported == 0


def test_hold02_europe_subtotal_fails(tmp_path):
    res = _score(tmp_path, HOLD, HOLD02, "Europe 127,604", ["Europe 127,604 15 %"])
    assert res.correct is False and res.supported == 0


def test_hold02_prior_year_total_fails(tmp_path):
    # The FY2025 column value cited as the FY2026 total: M4 keeps it (terms grounded), the
    # value/measure/period check rejects it because 416,161 is not the expected 478,834.
    res = _score(tmp_path, HOLD, HOLD02, "Total net sales 416,161", [TOTAL_ROW])
    assert res.correct is False and res.supported == 0


def test_hold02_wrong_value_with_correct_measure_and_period_fails(tmp_path):
    res = _score(tmp_path, HOLD, HOLD02, "Total net sales 500,000", [TOTAL_ROW])
    assert res.correct is False and res.supported == 0


def test_hold02_correct_number_without_citation_evidence_fails(tmp_path):
    # Correct number asserted, but the finding carries no citation -> insufficient evidence.
    res = _score(tmp_path, HOLD, HOLD02, "Total net sales 478,834", [])
    assert res.correct is False and res.supported == 0


def test_hold02_mismatched_claim_and_evidence_fails(tmp_path):
    # The claim asserts the correct number but cites an unrelated segment row.
    res = _score(tmp_path, HOLD, HOLD02, "Americas 478,834", ["Americas $ 198,083 11 %"])
    assert res.correct is False and res.supported == 0


# ---- negatives + permitted formatting (DEV-03 ref -8162) ------------------------------------------

FORD_ROW = "Net income/(loss) $ 4,329 $ 5,894 $ (8,162)"


@pytest.mark.parametrize("claim", [
    "Net income loss (8,162)",   # accounting parentheses
    "Net income loss -8,162",    # leading minus
])
def test_negative_values_preserve_exact_meaning(tmp_path, claim):
    # Both representations carry the same value as the evidence's "(8,162)".
    res = _score(tmp_path, DEV, DEV03, claim, [FORD_ROW])
    assert res.correct is True and res.supported == 1


def test_negative_wrong_sign_fails(tmp_path):
    # A bare positive where the answer is negative is a different value.
    res = _score(tmp_path, DEV, DEV03, "Net income loss 8,162", [FORD_ROW])
    assert res.correct is False and res.supported == 0


# ---- controls: correct answers on the other answerable scenarios still pass -----------------------

@pytest.mark.parametrize("corpus,sid,claim,quote", [
    (DEV, "DEV-01", "Revenue 281,724", "Revenue $ 281,724 $ 245,122"),
    (DEV, "DEV-02", "Net income 112,010", "Net income $ 112,010 $ 93,736"),
    (HOLD, "HOLD-01", "Net income 133,749", "Net income 133,749 101,832"),
])
def test_other_answerable_scenarios_correct_answers_pass(tmp_path, corpus, sid, claim, quote):
    res = _score(tmp_path, corpus, _scenario(corpus, sid), claim, [quote])
    assert res.correct is True and res.supported == 1


# ---- abstention scenarios unchanged ---------------------------------------------------------------

def test_abstention_scenario_unaffected_by_validation(tmp_path):
    # HOLD-03 must abstain: a finding is still incorrect (validation never runs — no reference
    # value), and an empty response is still correct.
    hold03 = _scenario(HOLD, "HOLD-03")
    answered = _score(tmp_path, HOLD, hold03, "Revenue 331,839", ["Revenue $ 331,839 $ 281,724"])
    assert answered.correct is False
    empty = _score(tmp_path, HOLD, hold03, "ignored", [])
    assert empty.correct is True


# ---- scope + fail-closed unit tests ---------------------------------------------------------------

def test_all_answerable_g2_scenarios_have_measure_and_period_metadata():
    for corpus in (DEV, HOLD):
        for s in corpus["scenarios"]:
            exp = s["expectations"]
            if exp.get("must_abstain") or exp.get("reference_value") is None:
                continue
            assert _g2_measure_terms(exp), s["id"]
            assert _g2_period_tokens(exp), s["id"]


def test_answer_supported_is_fail_closed_on_missing_metadata():
    # No reference value -> nothing to validate (abstain/synthetic path).
    assert _g2_answer_supported({}, "x", (), ()) == (True, "no reference value to validate")
    # Non-integer reference -> fail closed.
    ok, _ = _g2_answer_supported({"reference_value": "478834"}, "Total net sales 478834", (), ())
    assert ok is False


def test_answer_supported_requires_value_in_claim_and_evidence():
    exp = {"reference_value": 478834, "measure_definition": "Apple FY2026 total net sales.",
           "evidence_description": "Segment Operating Performance; Total net sales row, 2026 column."}
    # A cited document that carries the requested period (the FY2026 filing).
    doc_text = "Segment Operating Performance ... for 2026 ... Total net sales $ 478,834 ..."
    assert _g2_answer_supported(exp, "Total net sales 478,834", (TOTAL_ROW,), (doc_text,))[0] is True
    # Value absent from the claim.
    assert _g2_answer_supported(exp, "Total net sales", (TOTAL_ROW,), (doc_text,))[0] is False
    # Value absent from the evidence.
    assert _g2_answer_supported(exp, "Total net sales 478,834", ("Total net sales",), ("Total net sales",))[0] is False


def test_measure_terms_are_the_row_terms_intersected_with_the_definition():
    exp = {"measure_definition": "Apple FY2026 total net sales, year ended September 26, 2026.",
           "evidence_description": "Segment Operating Performance; Total net sales row, 2026 column."}
    assert _g2_measure_terms(exp) == {"total", "net", "sales"}


def test_answer_supported_fails_closed_on_measure_mismatch():
    exp = {"reference_value": 100, "measure_definition": "Acme FY2026 total revenue.",
           "evidence_description": "Results; Total revenue row, 2026 column."}
    # Value and period present, but the evidence names a different measure.
    ok, reason = _g2_answer_supported(
        exp, "operating income 100", ("operating income 100 2026",), ("operating income 100 2026",))
    assert ok is False and "measure" in reason


def test_answer_supported_fails_closed_on_period_mismatch():
    exp = {"reference_value": 100, "measure_definition": "Acme FY2026 total revenue.",
           "evidence_description": "Results; Total revenue row, 2026 column."}
    # Value and measure present, but the requested year is nowhere in claim/quote/cited text.
    ok, reason = _g2_answer_supported(
        exp, "total revenue 100", ("total revenue 100",), ("total revenue 100",))
    assert ok is False and "period" in reason


@pytest.mark.parametrize("text,expected", [
    ("478,834", {478834}),
    ("+478834", {478834}),
    ("(8,162)", {-8162}),
    ("-8162", {-8162}),
    ("478834.0", {478834, 0}),
    ("no numbers here", set()),
])
def test_integer_values_normalization(text, expected):
    assert _integer_values(text) == expected
