"""M4 tests: deterministic citation + relevance gate.

Scope note: these tests demonstrate resistance to the attacks REPRESENTED here. The relevance gate
is a deterministic lexical/negation check, not semantic understanding; a passing test shows
resistance to the tested cases only (see citation.py docstring).
"""
from __future__ import annotations

import pytest

from apex_investor.citation import (
    Citation,
    DocumentProvenance,
    ProposedFinding,
    evaluate,
    evaluate_all,
    supports_claim,
)
from apex_investor.docstore import DocumentStore, new_document
from apex_investor.sanitize import SANITIZER_VERSION

URL = "https://www.sec.gov/Archives/acme-10q.htm"

# A realistic sanitized filing body. Distinct facts so unrelated citations are clearly unrelated.
BODY = (
    "ACME Corp quarterly report. Net revenue for the quarter was 1.2 billion dollars, up 8% year "
    "over year. Gross margin improved to 41 percent. The company repurchased shares during the "
    "period. Management expressed caution about the upcoming holiday season demand. "
    "Operating expenses increased modestly. The board declared no dividend this quarter."
)

# A claim whose significant terms are ALL present in GOOD_QUOTE. The gate is lexical, so a
# paraphrase ("rose" vs the quote's "up") would be rejected — a deliberate false-negative. The
# claim here is worded to be faithfully grounded in the quote.
GOOD_QUOTE = "Net revenue for the quarter was 1.2 billion dollars, up 8% year over year."
GOOD_CLAIM = "net revenue was up 8% year over year"


@pytest.fixture()
def store(tmp_path):
    s = DocumentStore(tmp_path / "docs")
    doc = new_document(
        ticker="ACME", kind="filing", source_url=URL, approved_domain="sec.gov",
        retrieved_at="2026-11-07", published_at="2026-11-05", raw=BODY.encode(),
        text=BODY, title="10-Q", sanitizer_version=SANITIZER_VERSION,
    )
    s.put(doc)
    return s, doc


def mkfinding(doc, *, claim, quote, ticker="ACME", doc_id=None, source_url=URL,
              source_docs=None, citations=None, reason="r", **over):
    cit = Citation(doc_id=doc_id or doc.doc_id, quote_span=quote, source_url=source_url)
    kw = dict(
        finding_id="F1", ticker=ticker, created_at="2026-11-07", claim=claim,
        citations=citations if citations is not None else (cit,), reason=reason,
        falsifier="f", confidence=0.5, model="m", prompt_version="v1",
        source_docs=source_docs if source_docs is not None else (doc.doc_id,),
    )
    kw.update(over)
    return ProposedFinding(**kw)


# ---- happy path ------------------------------------------------------------

def test_supported_claim_is_kept(store):
    s, doc = store
    f = mkfinding(doc, claim=GOOD_CLAIM, quote=GOOD_QUOTE)
    res = evaluate(f, s)
    assert res.kept
    assert res.finding is not None
    assert all(e.supports_claim for e in res.finding.evidence)


# ---- gate 1: existence / verbatim -----------------------------------------

def test_unknown_doc_id_dropped(store):
    s, doc = store
    f = mkfinding(doc, claim=GOOD_CLAIM, quote=GOOD_QUOTE, doc_id="0" * 64)
    res = evaluate(f, s)
    assert res.status == "dropped"
    assert "unknown doc_id" in res.dropped_reason


def test_invalid_doc_id_dropped(store):
    s, doc = store
    f = mkfinding(doc, claim=GOOD_CLAIM, quote=GOOD_QUOTE, doc_id="../escape")
    res = evaluate(f, s)
    assert res.status == "dropped"
    assert "invalid doc_id" in res.dropped_reason


def test_non_verbatim_quote_dropped(store):
    s, doc = store
    # Same sentence but "9%" instead of "8%" — not present verbatim.
    f = mkfinding(doc, claim=GOOD_CLAIM,
                  quote="Net revenue for the quarter was 1.2 billion dollars, up 9% year over year.")
    res = evaluate(f, s)
    assert res.status == "dropped"
    assert "not found verbatim" in res.dropped_reason


def test_source_url_mismatch_dropped(store):
    s, doc = store
    f = mkfinding(doc, claim=GOOD_CLAIM, quote=GOOD_QUOTE,
                  source_url="https://evil.example/x")
    res = evaluate(f, s)
    assert res.status == "dropped"
    assert "source_url" in res.dropped_reason


def test_ticker_mismatch_dropped(store):
    s, doc = store
    f = mkfinding(doc, claim=GOOD_CLAIM, quote=GOOD_QUOTE, ticker="OTHER")
    res = evaluate(f, s)
    assert res.status == "dropped"
    assert "ticker" in res.dropped_reason


def test_citation_not_listed_in_source_docs_dropped(store):
    s, doc = store
    f = mkfinding(doc, claim=GOOD_CLAIM, quote=GOOD_QUOTE, source_docs=("a" * 64,))
    res = evaluate(f, s)
    assert res.status == "dropped"
    assert "source_docs" in res.dropped_reason


def test_no_citations_dropped(store):
    s, doc = store
    f = mkfinding(doc, claim=GOOD_CLAIM, quote="x", citations=())
    res = evaluate(f, s)
    assert res.status == "dropped"
    assert "no citations" in res.dropped_reason


def test_corrupt_document_fails_closed(tmp_path):
    s = DocumentStore(tmp_path / "docs")
    doc = new_document(ticker="ACME", kind="filing", source_url=URL, approved_domain="sec.gov",
                       retrieved_at="2026-11-07", raw=BODY.encode(), text=BODY,
                       sanitizer_version=SANITIZER_VERSION)
    s.put(doc)
    s._path(doc.doc_id).write_text("{ not json", encoding="utf-8")  # corrupt on disk
    f = mkfinding(doc, claim=GOOD_CLAIM, quote=GOOD_QUOTE)
    res = evaluate(f, s)
    assert res.status == "dropped"
    assert "corrupt document" in res.dropped_reason


# ---- gate 2: relevance (the important distinction) -------------------------

def test_real_but_unrelated_quote_dropped(store):
    # THE key adversarial case: the quote genuinely exists verbatim, but does not support the claim.
    s, doc = store
    f = mkfinding(doc, claim="ACME will cut its dividend next quarter",
                  quote="The company repurchased shares during the period.")
    res = evaluate(f, s)
    assert res.status == "dropped"
    assert res.dropped_reason == "no citation supports the claim"
    # Evidence is still reported with supports_claim=False and a note (recorded outcome).
    ev = res.evaluated_evidence[0]
    assert ev.supports_claim is False
    assert "not grounded" in ev.relevance_note


def test_quote_negating_the_claim_is_rejected(store):
    # All claim terms ARE grounded, but the quote contains a negation the claim lacks.
    s, doc = store
    f = mkfinding(doc, claim="the board declared dividend this quarter",
                  quote="The board declared no dividend this quarter.")
    res = evaluate(f, s)
    assert res.status == "dropped"
    assert "negation" in res.evaluated_evidence[0].relevance_note


def test_topical_overlap_is_not_support(store):
    s, doc = store
    # Shares a couple of terms ("operating", "increased") but the claim is about something else.
    f = mkfinding(doc, claim="Operating leverage increased margins dramatically",
                  quote="Operating expenses increased modestly.")
    res = evaluate(f, s)
    assert res.status == "dropped"


def test_supported_claim_with_extra_citation_kept(store):
    s, doc = store
    good = Citation(doc.doc_id, GOOD_QUOTE, URL)
    irrelevant = Citation(doc.doc_id, "The company repurchased shares during the period.", URL)
    f = mkfinding(doc, claim=GOOD_CLAIM, quote="x", citations=(good, irrelevant))
    res = evaluate(f, s)
    assert res.kept
    assert res.finding is not None
    # Correction 1: the VERIFIED finding carries ONLY the passing citation.
    assert [e.citation.quote_span for e in res.finding.evidence] == [GOOD_QUOTE]
    assert all(e.supports_claim for e in res.finding.evidence)
    # ... but the failed citation is still available for audit in evaluated_evidence.
    audit = {e.citation.quote_span: e.supports_claim for e in res.evaluated_evidence}
    assert audit[GOOD_QUOTE] is True
    assert audit[irrelevant.quote_span] is False


def test_kept_finding_retains_only_passing_provenance_and_source_docs(store):
    s, doc = store
    good = Citation(doc.doc_id, GOOD_QUOTE, URL)
    irrelevant = Citation(doc.doc_id, "The company repurchased shares during the period.", URL)
    f = mkfinding(doc, claim=GOOD_CLAIM, quote="x", citations=(good, irrelevant))
    res = evaluate(f, s)
    assert res.kept and res.finding is not None
    # Only passing citations => only their doc ids, and provenance is 1:1 with passing evidence.
    assert res.finding.source_docs == (doc.doc_id,)
    assert len(res.finding.provenance) == len(res.finding.evidence) == 1
    assert all(e.supports_claim for e in res.finding.evidence)


def test_evaluated_evidence_retains_failures_for_audit(store):
    s, doc = store
    # All citations fail the relevance gate: the finding is dropped, but evidence is still reported.
    f = mkfinding(doc, claim="ACME will cut its dividend next quarter",
                  quote="The company repurchased shares during the period.")
    res = evaluate(f, s)
    assert res.status == "dropped"
    assert res.finding is None
    assert len(res.evaluated_evidence) == 1
    assert res.evaluated_evidence[0].supports_claim is False


def test_paraphrase_is_rejected_false_negative_limitation(store):
    # Documents the honest limitation: a claim that MEANS the same but paraphrases ("rose" vs the
    # quote's "up") is rejected. The gate errs toward dropping — the safe direction.
    s, doc = store
    f = mkfinding(doc, claim="net revenue rose 8% year over year", quote=GOOD_QUOTE)
    res = evaluate(f, s)
    assert res.status == "dropped"


# ---- supports_claim unit-level ---------------------------------------------

def test_supports_claim_rejects_short_quote():
    ok, note = supports_claim("revenue rose", "revenue")
    assert ok is False and "shorter" in note


def test_supports_claim_rejects_empty_quote():
    ok, note = supports_claim("revenue rose", "   ")
    assert ok is False and "empty" in note


def test_supports_claim_requires_all_claim_terms():
    ok, _ = supports_claim("net revenue rose 8 percent", "net revenue rose")
    assert ok is False  # "8" and "percent" are ungrounded


def test_supports_claim_accepts_grounded_claim():
    ok, note = supports_claim(GOOD_CLAIM, GOOD_QUOTE)
    assert ok is True and "grounded" in note


# ---- min_grounded_ratio validation -----------------------------------------

@pytest.mark.parametrize("bad", [
    0, 0.0, -0.1, -1, 1.0000001, 2, True, False, float("nan"),
    float("inf"), float("-inf"), None, "1.0", [1.0], 10 ** 400,
])
def test_supports_claim_rejects_invalid_min_grounded_ratio(bad):
    with pytest.raises(ValueError, match="min_grounded_ratio"):
        supports_claim(GOOD_CLAIM, GOOD_QUOTE, min_grounded_ratio=bad)


@pytest.mark.parametrize("good", [1.0, 0.5, 1])
def test_supports_claim_accepts_valid_min_grounded_ratio(good):
    ok, _ = supports_claim(GOOD_CLAIM, GOOD_QUOTE, min_grounded_ratio=good)
    assert ok is True


def test_evaluate_propagates_min_grounded_ratio_validation(store):
    s, doc = store
    f = mkfinding(doc, claim=GOOD_CLAIM, quote=GOOD_QUOTE)
    with pytest.raises(ValueError, match="min_grounded_ratio"):
        evaluate(f, s, min_grounded_ratio=float("nan"))


# ---- negation: contractions and claim-side negation ------------------------

@pytest.mark.parametrize("contraction", [
    "didn't", "doesn't", "isn't", "wasn't", "can't", "won't", "don't", "hasn't",
])
def test_quote_contraction_negation_is_detected(contraction):
    # The claim has no negation; the quote negates via a contraction. Must be rejected.
    claim = "net revenue was up 8% year over year"
    quote = f"net revenue {contraction} up 8% year over year"
    ok, note = supports_claim(claim, quote)
    assert ok is False
    assert "negation" in note


def test_curly_apostrophe_contraction_is_detected():
    claim = "revenue increase"          # all terms ground in the quote
    quote = "revenue didn\u2019t increase"  # typographic apostrophe
    ok, note = supports_claim(claim, quote)
    assert ok is False
    assert "negation" in note


def test_claim_with_negation_and_quote_without_is_rejected():
    # The claim is negated but the quote is not: the quote cannot support the negated claim.
    # At the default threshold this is caught by grounding (the claim's "not" is ungrounded), so we
    # lower the threshold to reach and exercise the dedicated claim-side negation branch.
    ok, note = supports_claim("revenue did not increase", "revenue increased sharply")
    assert ok is False  # rejected at default 1.0 (via grounding)
    ok2, note2 = supports_claim("revenue did not increase", "revenue did increase",
                                min_grounded_ratio=0.75)
    assert ok2 is False
    assert "claim is negated" in note2


def test_evaluate_validates_ratio_before_no_citations_early_return(store):
    # An invalid threshold must be rejected even on the no-citations path, which never reaches
    # supports_claim. This guards the early-return ordering in evaluate().
    s, doc = store
    f = mkfinding(doc, claim=GOOD_CLAIM, quote="x", citations=())
    with pytest.raises(ValueError, match="min_grounded_ratio"):
        evaluate(f, s, min_grounded_ratio=float("nan"))


def test_negation_in_both_claim_and_quote_is_allowed_to_match():
    # Both negate: negation parity holds, and terms are grounded.
    ok, note = supports_claim("revenue did not increase", "revenue did not increase")
    assert ok is True and "grounded" in note


def test_ordinary_word_ending_in_nt_is_not_treated_as_negation():
    # "plant"/"front"/"important" must not be mangled by contraction handling.
    ok, _ = supports_claim("plant capacity increased", "plant capacity increased")
    assert ok is True


def test_reason_field_is_never_counted_as_evidence(store):
    # The model's reason contains the supporting words, but the quote does not: must be dropped.
    s, doc = store
    f = mkfinding(doc, claim=GOOD_CLAIM, quote="The company repurchased shares.",
                  reason="Net revenue was up 8% year over year according to the filing.")
    res = evaluate(f, s)
    assert res.status == "dropped"


# ---- provenance preserved --------------------------------------------------

def test_provenance_preserved_on_kept_finding(store):
    s, doc = store
    f = mkfinding(doc, claim=GOOD_CLAIM, quote=GOOD_QUOTE)
    res = evaluate(f, s)
    assert res.kept and res.finding is not None
    prov = res.finding.provenance[0]
    assert isinstance(prov, DocumentProvenance)
    assert prov.doc_id == doc.doc_id
    assert prov.ticker == "ACME"
    assert prov.source_url == URL
    assert prov.retrieved_at == "2026-11-07"
    assert prov.published_at == "2026-11-05"
    assert prov.raw_hash == doc.raw_hash
    assert prov.content_hash == doc.content_hash
    assert prov.sanitizer_version == SANITIZER_VERSION


# ---- determinism / batching ------------------------------------------------

def test_evaluate_is_deterministic(store):
    s, doc = store
    f = mkfinding(doc, claim=GOOD_CLAIM, quote=GOOD_QUOTE)
    assert evaluate(f, s) == evaluate(f, s)


def test_evaluate_all_reports_kept_and_dropped(store):
    s, doc = store
    good = mkfinding(doc, claim=GOOD_CLAIM, quote=GOOD_QUOTE)
    bad = mkfinding(doc, claim="ACME will cut its dividend next quarter",
                    quote="The company repurchased shares during the period.")
    results = evaluate_all([good, bad], s)
    assert [r.status for r in results] == ["kept", "dropped"]


def test_evaluate_all_validates_ratio_on_empty_batch(store):
    # The per-item evaluate() never runs for an empty batch, so evaluate_all() must validate itself.
    s, _ = store
    with pytest.raises(ValueError, match="min_grounded_ratio"):
        evaluate_all([], s, min_grounded_ratio=float("nan"))


def test_evaluate_all_empty_batch_returns_empty_list(store):
    s, _ = store
    assert evaluate_all([], s) == []


def test_evaluate_rejects_non_finding(store):
    s, _ = store
    with pytest.raises(TypeError, match="ProposedFinding"):
        evaluate({"claim": "x"}, s)
