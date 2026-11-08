"""M5 tests: advisory report hook (Phase 2).

Covers the 15 locked acceptance criteria. Fixture-driven: no model, network, or ingestion is
required, because M2/M3a/M3 are not implemented. The pre-M5 baseline fixture
`tests/fixtures/report_pre_m5.md` was captured from the unchanged `to_markdown()` before any M5
edit and is treated as immutable.
"""
from __future__ import annotations

import inspect
from pathlib import Path

import pytest

import apex_investor.report as report_mod
from apex_investor.advisory import AdvisorySection
from apex_investor.citation import (
    Citation,
    DocumentProvenance,
    Evidence,
    VerifiedFinding,
)
from apex_investor.context import build_temporal_context
from apex_investor.docstore import DocumentStore, new_document
from apex_investor.interpret import (
    AdvisoryBuildError,
    build_advisory,
    build_advisory_safe,
    build_section_for,
)
from apex_investor.ledger import Ledger, Side, Tag, Transaction
from apex_investor.report import build_report
from apex_investor.sanitize import SANITIZER_VERSION

FIXTURE = Path(__file__).parent / "fixtures" / "report_pre_m5.md"
URL = "https://www.sec.gov/Archives/acme-10q.htm"
AS_OF = "2024-06-01"
BODY = "ACME net revenue was up 8% year over year."


# ---- Phase 1 fixture helpers ----------------------------------------------

def _ledger():
    txs, cost = [], 0.0
    for tk, (sh, px) in {"VTI": (5, 100.0), "JUNK": (5, 100.0)}.items():
        txs.append(Transaction("2024-01-02", tk, Side.BUY, sh, px))
        cost += sh * px
    return Ledger.from_transactions(txs, starting_cash=9000.0 + cost,
                                    tags={"VTI": Tag.CORE, "JUNK": Tag.SPECULATIVE})


def _phase1_report(**over):
    kw = dict(tags={"VTI": Tag.CORE, "JUNK": Tag.SPECULATIVE}, price_as_of=AS_OF,
              generated_at="2024-06-01T12:00:00Z")
    kw.update(over)
    return build_report(AS_OF, _ledger(), {"VTI": 100.0, "JUNK": 100.0}, **kw)


# ---- M4/M4b fixtures (built directly; no model/network) --------------------

@pytest.fixture()
def store(tmp_path):
    return DocumentStore(tmp_path / "docs")


def _doc(store, *, published_at="2024-05-30", url=URL, ticker="ACME"):
    d = new_document(ticker=ticker, kind="filing", source_url=url, approved_domain="sec.gov",
                     retrieved_at=AS_OF, published_at=published_at, raw=BODY.encode(), text=BODY,
                     title="10-Q", sanitizer_version=SANITIZER_VERSION)
    store.put(d)
    return d


def _finding(doc, *, quote=BODY, claim="net revenue was up 8%", finding_id="F1", ticker="ACME"):
    ev = Evidence(citation=Citation(doc.doc_id, quote, doc.source_url), supports_claim=True,
                  relevance_note="ok")
    return VerifiedFinding(
        finding_id=finding_id, ticker=ticker, created_at=AS_OF, claim=claim, evidence=(ev,),
        reason="revenue grew", falsifier="revenue fell", confidence=0.5, model="m",
        prompt_version="v1", source_docs=(doc.doc_id,),
        provenance=(DocumentProvenance.from_document(doc),),
    )


def _pair(store, **doc_kw):
    doc = _doc(store, **doc_kw)
    finding = _finding(doc)
    ctx = build_temporal_context(finding, store, as_of=AS_OF)
    return finding, ctx


# ---- 5. byte-identity with advisory=None (vs captured fixture) -------------

def test_advisory_none_is_byte_identical_to_baseline():
    got = _phase1_report().to_markdown().encode("utf-8")
    assert got == FIXTURE.read_bytes()


def test_baseline_fixture_ends_with_single_newline():
    raw = FIXTURE.read_bytes()
    assert raw.endswith(b"\n") and not raw.endswith(b"\n\n")


# ---- 6. existing six sections unchanged with an advisory -------------------

def test_existing_sections_unchanged_with_advisory(store):
    f, ctx = _pair(store)
    adv = build_advisory([(f, ctx)])
    with_adv = _phase1_report(advisory=adv).to_markdown()
    base = FIXTURE.read_text()
    # Everything before the new section must be identical.
    assert with_adv.startswith(base.rstrip("\n"))
    for heading in ["## 1. Portfolio snapshot", "## 2. Risk status", "## 3. Alerts",
                    "## 4. Evidence", "## 5. Blocked checks", "## 6. Summary"]:
        assert heading in with_adv
    assert "## 7. Interpretation (advisory)" in with_adv


# ---- 8. valid findings render ---------------------------------------------

def test_valid_findings_render(store):
    f, ctx = _pair(store)
    md = _phase1_report(advisory=build_advisory([(f, ctx)])).to_markdown()
    assert "## 7. Interpretation (advisory)" in md
    assert "### ACME — finding F1" in md
    assert "- Claim: net revenue was up 8%" in md
    assert "- Reason: revenue grew" in md
    assert "- Falsifier: revenue fell" in md
    assert "- Confidence: 0.50" in md
    assert "- Model: m (prompt v1)" in md
    assert "- Evidence:" in md


# ---- 1 & 3. each quote identifies its own source; provenance survives ------

def test_each_quote_identifies_its_own_source(store):
    doc = _doc(store)
    ev1 = Evidence(citation=Citation(doc.doc_id, BODY, doc.source_url), supports_claim=True,
                   relevance_note="ok")
    ev2 = Evidence(citation=Citation(doc.doc_id, "quarterly report", doc.source_url),
                   supports_claim=True, relevance_note="ok")
    finding = VerifiedFinding(
        finding_id="F1", ticker="ACME", created_at=AS_OF, claim="c", evidence=(ev1, ev2),
        reason="r", falsifier="f", confidence=0.5, model="m", prompt_version="v1",
        source_docs=(doc.doc_id,), provenance=(DocumentProvenance.from_document(doc),))
    ctx = build_temporal_context(finding, store, as_of=AS_OF)
    adv = build_advisory([(finding, ctx)])
    e = adv.items[0].evidence[0]
    assert e.quote == BODY
    assert e.source_url == URL
    assert e.doc_id == doc.doc_id
    assert e.title == "10-Q"
    md = _phase1_report(advisory=adv).to_markdown()
    assert f'  - "{BODY}"' in md
    assert f"source: {URL} (10-Q) (doc {doc.doc_id})" in md


# ---- 4. published vs retrieved distinct ------------------------------------

def test_published_and_retrieved_render_distinctly(store):
    f, ctx = _pair(store)
    md = _phase1_report(advisory=build_advisory([(f, ctx)])).to_markdown()
    assert "published: 2024-05-30 · retrieved: 2024-06-01" in md


# ---- 11. unknown/invalid date => recency unknown, never recent -------------

def test_unknown_date_recency_unknown(store):
    f, ctx = _pair(store, published_at=None)
    adv = build_advisory([(f, ctx)])
    assert adv.items[0].recency == "unknown"
    md = _phase1_report(advisory=adv).to_markdown()
    assert "Recency: unknown" in md
    assert "Recency: recent" not in md


def test_invalid_date_rendered_as_invalid(store):
    f, ctx = _pair(store, published_at="20240530")  # compact -> invalid
    adv = build_advisory([(f, ctx)])
    assert adv.items[0].recency == "unknown"
    md = _phase1_report(advisory=adv).to_markdown()
    assert "published: invalid (not used for recency)" in md


# ---- 12. empty vs unavailable ---------------------------------------------

def test_no_findings_state(store):
    md = _phase1_report(advisory=build_advisory([])).to_markdown()
    assert "## 7. Interpretation (advisory)" in md
    assert "No findings." in md


def test_unavailable_state():
    md = _phase1_report(advisory=AdvisorySection(items=(), available=False)).to_markdown()
    assert "## 7. Interpretation (advisory)" in md
    assert "No interpretation available." in md


# ---- 2 & 3 & 9. builder joins by doc_id; fails closed (section-level) ------

def test_citation_without_temporal_document_fails_closed(store):
    doc = _doc(store)
    finding = _finding(doc)
    ctx = build_temporal_context(finding, store, as_of=AS_OF)
    # Corrupt the link: the finding cites a doc_id absent from the context.
    other = _doc(store, url="https://www.sec.gov/Archives/other.htm")
    broken = VerifiedFinding(
        finding_id="F1", ticker="ACME", created_at=AS_OF, claim="c",
        evidence=(Evidence(citation=Citation(other.doc_id, BODY, other.source_url),
                           supports_claim=True, relevance_note="ok"),),
        reason="r", falsifier="f", confidence=0.5, model="m", prompt_version="v1",
        source_docs=(other.doc_id,), provenance=(DocumentProvenance.from_document(other),))
    with pytest.raises(AdvisoryBuildError, match="no temporal context"):
        build_advisory([(broken, ctx)])


def test_finding_without_evidence_fails_closed(store):
    doc = _doc(store)
    finding = _finding(doc)
    ctx = build_temporal_context(finding, store, as_of=AS_OF)
    empty = VerifiedFinding(
        finding_id="F1", ticker="ACME", created_at=AS_OF, claim="c", evidence=(),
        reason="r", falsifier="f", confidence=0.5, model="m", prompt_version="v1",
        source_docs=(), provenance=())
    with pytest.raises(AdvisoryBuildError, match="no passing evidence"):
        build_advisory([(empty, ctx)])


def test_ticker_mismatch_fails_closed(store):
    f, ctx = _pair(store)
    mismatched = VerifiedFinding(
        finding_id="F1", ticker="OTHER", created_at=AS_OF, claim=f.claim, evidence=f.evidence,
        reason=f.reason, falsifier=f.falsifier, confidence=f.confidence, model=f.model,
        prompt_version=f.prompt_version, source_docs=f.source_docs, provenance=f.provenance)
    with pytest.raises(AdvisoryBuildError, match="ticker"):
        build_advisory([(mismatched, ctx)])


def test_citation_source_url_mismatch_fails_closed(store):
    # The doc_id matches the context, but the citation claims a different source_url.
    f, ctx = _pair(store)
    bad_cit = Citation(f.source_docs[0], BODY, "https://evil.example/x")
    bad_finding = VerifiedFinding(
        finding_id="F1", ticker="ACME", created_at=AS_OF, claim=f.claim,
        evidence=(Evidence(citation=bad_cit, supports_claim=True, relevance_note="ok"),),
        reason=f.reason, falsifier=f.falsifier, confidence=f.confidence, model=f.model,
        prompt_version=f.prompt_version, source_docs=f.source_docs, provenance=f.provenance)
    with pytest.raises(AdvisoryBuildError, match="source_url"):
        build_advisory([(bad_finding, ctx)])


def test_evidence_not_marked_passing_fails_closed(store):
    # The builder relies on the gate's supports_claim guarantee DELIBERATELY: an evidence record the
    # gate did not accept must be rejected, never rendered.
    f, ctx = _pair(store)
    not_passing = VerifiedFinding(
        finding_id="F1", ticker="ACME", created_at=AS_OF, claim=f.claim,
        evidence=(Evidence(citation=f.evidence[0].citation, supports_claim=False,
                           relevance_note="rejected"),),
        reason=f.reason, falsifier=f.falsifier, confidence=f.confidence, model=f.model,
        prompt_version=f.prompt_version, source_docs=f.source_docs, provenance=f.provenance)
    with pytest.raises(AdvisoryBuildError, match="not marked as supporting"):
        build_advisory([(not_passing, ctx)])


def test_builder_does_not_silently_drop_a_bad_finding(store):
    # One good + one broken => the WHOLE section must fail (no partial render).
    good_f, good_ctx = _pair(store)
    other = _doc(store, url="https://www.sec.gov/Archives/other.htm")
    broken_ctx = build_temporal_context(_finding(other), store, as_of=AS_OF)
    # broken_f cites the GOOD document's id, which is absent from broken_ctx -> unmatched join.
    broken_f = VerifiedFinding(
        finding_id="F2", ticker="ACME", created_at=AS_OF, claim="c",
        evidence=(Evidence(citation=Citation(good_f.source_docs[0], BODY, URL),
                           supports_claim=True, relevance_note="ok"),),
        reason="r", falsifier="f", confidence=0.5, model="m", prompt_version="v1",
        source_docs=(good_f.source_docs[0],),
        provenance=(DocumentProvenance.from_document(other),))
    with pytest.raises(AdvisoryBuildError):
        build_advisory([(good_f, good_ctx), (broken_f, broken_ctx)])


# ---- 4 (orchestration). builder failure -> unavailable, Phase 1 still runs -

def test_orchestration_maps_build_error_to_unavailable(store):
    # The real caller path: build_advisory_safe turns a build failure into an unavailable section.
    good_f, good_ctx = _pair(store)
    other = _doc(store, url="https://www.sec.gov/Archives/other.htm")
    broken_ctx = build_temporal_context(_finding(other), store, as_of=AS_OF)
    broken_f = VerifiedFinding(
        finding_id="F2", ticker="ACME", created_at=AS_OF, claim="c",
        evidence=(Evidence(citation=Citation(good_f.source_docs[0], BODY, URL),
                           supports_claim=True, relevance_note="ok"),),
        reason="r", falsifier="f", confidence=0.5, model="m", prompt_version="v1",
        source_docs=(good_f.source_docs[0],),
        provenance=(DocumentProvenance.from_document(other),))
    adv = build_advisory_safe([(good_f, good_ctx), (broken_f, broken_ctx)])
    assert adv.available is False and adv.items == ()
    # ...and a valid pair goes through the same entry point as available.
    ok = build_advisory_safe([(good_f, good_ctx)])
    assert ok.available is True and len(ok.items) == 1


def test_build_section_for_valid_path(store):
    f, ctx = _pair(store)
    section = build_section_for([f], store, as_of=AS_OF)
    assert section.available is True and len(section.items) == 1
    md = _phase1_report(advisory=section).to_markdown()
    assert "## 7. Interpretation (advisory)" in md
    assert "### ACME — finding F1" in md


def test_build_section_for_m4b_context_failure_is_unavailable(store):
    # Full orchestration path: an M4b ContextError must become an UNAVAILABLE section, and the
    # Phase 1 report must still render. as_of before the doc's retrieved_at => context raises.
    f, _ = _pair(store)
    section = build_section_for([f], store, as_of="2024-05-01")
    assert section.available is False and section.items == ()
    md = _phase1_report(advisory=section).to_markdown()
    assert "No interpretation available." in md
    assert "## 1. Portfolio snapshot" in md and "## 6. Summary" in md


# ---- 13. advisory failure does not stop Phase 1 ---------------------------

def test_render_failure_degrades_to_fallback(store, monkeypatch):
    f, ctx = _pair(store)
    adv = build_advisory([(f, ctx)])

    def boom(self):
        raise RuntimeError("render exploded")

    monkeypatch.setattr(report_mod.DailyReport, "_render_advisory", boom)
    md = _phase1_report(advisory=adv).to_markdown()
    assert "## 7. Interpretation (advisory)" in md
    assert "No interpretation available." in md
    # Phase 1 body still fully present.
    assert "## 1. Portfolio snapshot" in md and "## 6. Summary" in md


def test_write_report_survives_advisory_render_failure(tmp_path, store, monkeypatch):
    # End-to-end: a rendering failure must not stop write_report from writing the Phase 1 report.
    from apex_investor.report import write_report
    f, ctx = _pair(store)

    def boom(self):
        raise RuntimeError("render exploded")

    monkeypatch.setattr(report_mod.DailyReport, "_render_advisory", boom)
    r = _phase1_report(advisory=build_advisory([(f, ctx)]))
    p = write_report(r, tmp_path, regenerate=True)
    text = p.read_text()
    assert "## 7. Interpretation (advisory)" in text
    assert "No interpretation available." in text
    assert "## 1. Portfolio snapshot" in text and "## 6. Summary" in text


def test_write_report_with_valid_advisory(tmp_path, store):
    from apex_investor.report import write_report
    f, ctx = _pair(store)
    r = _phase1_report(advisory=build_advisory([(f, ctx)]))
    p = write_report(r, tmp_path, regenerate=True)
    assert p.exists() and "## 7. Interpretation (advisory)" in p.read_text()


# ---- 7. no advisory data affects Phase 1 fields ---------------------------

def test_no_mutation_of_phase1_fields(store):
    f, ctx = _pair(store)
    a = _phase1_report()
    b = _phase1_report(advisory=build_advisory([(f, ctx)]))
    for attr in ("equity", "cash", "cash_weight", "holdings", "alerts", "status"):
        assert getattr(a, attr) == getattr(b, attr), attr
    assert a.risk.equity == b.risk.equity
    assert a.risk.trustworthy == b.risk.trustworthy


# ---- 14. isolation: report.py imports nothing from citation/context --------

def test_report_does_not_import_citation_or_context():
    src = inspect.getsource(report_mod)
    assert "import citation" not in src
    assert "import context" not in src
    assert "from .citation" not in src
    assert "from .context" not in src


def test_interpret_imports_no_phase1_math():
    import apex_investor.interpret as interp
    src = inspect.getsource(interp)
    for forbidden in ("risk", "ledger", "report", "monitor", "prices", "sanity"):
        assert f"import {forbidden}" not in src
        assert f"from .{forbidden}" not in src


# ---- 15. determinism / provenance -----------------------------------------

def test_deterministic_section(store):
    f, ctx = _pair(store)
    a = _phase1_report(advisory=build_advisory([(f, ctx)])).to_markdown()
    b = _phase1_report(advisory=build_advisory([(f, ctx)])).to_markdown()
    assert a == b


def test_item_carries_model_prompt_and_doc_ids(store):
    f, ctx = _pair(store)
    item = build_advisory([(f, ctx)]).items[0]
    assert item.model == "m" and item.prompt_version == "v1"
    assert item.evidence[0].doc_id == f.source_docs[0]


# ---- console path unaffected ----------------------------------------------

def test_console_matches_markdown(store):
    f, ctx = _pair(store)
    r = _phase1_report(advisory=build_advisory([(f, ctx)]))
    assert r.to_console() == r.to_markdown().rstrip()


# ---- injection resistance: untrusted Markdown content ----------------------

def test_markdown_control_chars_cannot_inject_structure(store):
    # A malicious quote tries to inject a heading, a link, a tab and a NUL byte.
    evil_quote = "line1\n## 1. Portfolio snapshot\n[click](http://evil.example)\t# PWNED\x00\x07"
    d2 = new_document(ticker="ACME", kind="filing", source_url=URL, approved_domain="sec.gov",
                      retrieved_at=AS_OF, published_at="2024-05-30", raw=evil_quote.encode(),
                      text=evil_quote, title="t", sanitizer_version=SANITIZER_VERSION)
    store.put(d2)
    finding = VerifiedFinding(
        finding_id="F1\n## 9. Injected", ticker="ACME", created_at=AS_OF,
        claim="claim\n# HEADING",
        evidence=(Evidence(citation=Citation(d2.doc_id, evil_quote, d2.source_url),
                           supports_claim=True, relevance_note="ok"),),
        reason="r\n- injected bullet", falsifier="f", confidence=0.5, model="m",
        prompt_version="v1", source_docs=(d2.doc_id,),
        provenance=(DocumentProvenance.from_document(d2),))
    ctx = build_temporal_context(finding, store, as_of=AS_OF)
    md = _phase1_report(advisory=build_advisory([(finding, ctx)])).to_markdown()
    # Rendered output itself must contain no injected structure or control characters.
    assert "\n# PWNED" not in md
    assert "\n## 9. Injected" not in md
    assert "[click](http://evil.example)" not in md
    assert "\t" not in md and "\x00" not in md and "\x07" not in md
    assert md.count("## 1. Portfolio snapshot") == 1
    assert md.count("## 7. Interpretation (advisory)") == 1
    # The advisory section is exactly one extra line-block; verify the rendered evidence line.
    adv_lines = md.split("## 7. Interpretation (advisory)")[1].splitlines()
    quote_line = next(ln for ln in adv_lines if ln.strip().startswith('- "'))
    assert "\\#" in quote_line and "\\[" in quote_line  # escaped, single line


def test_md_safe_unit_behaviour():
    from apex_investor.report import _md_safe
    assert _md_safe("a\nb\r\nc\td") == "a b c d"
    assert _md_safe("x\x00\x07y\x1fz") == "xyz"          # C0 removed
    assert _md_safe("plain") == "plain"
    assert _md_safe(None) == ""
    assert _md_safe("#h [l](u) *e*") == "\\#h \\[l\\]\\(u\\) \\*e\\*"
