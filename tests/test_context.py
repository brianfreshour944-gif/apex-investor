"""M4b tests: deterministic date/context handling.

Covers the approved acceptance criteria, including strict YYYY-MM-DD validation (compact dates are
invalid), fail-closed store/provenance consistency, duplicate-document deduplication, the 30/31-day
boundary, and the absence of any wall-clock dependency.
"""
from __future__ import annotations

import inspect
from datetime import date as _date

import pytest

import apex_investor.context as ctx
from apex_investor.citation import (
    Citation,
    DocumentProvenance,
    Evidence,
    VerifiedFinding,
)
from apex_investor.context import (
    ContextError,
    _parse_strict_date,
    build_temporal_context,
)
from apex_investor.docstore import DocumentStore, new_document
from apex_investor.sanitize import SANITIZER_VERSION

URL = "https://www.sec.gov/Archives/acme-10q.htm"
AS_OF = "2026-11-07"


def _mk_doc(store, *, ticker="ACME", url=URL, retrieved_at=AS_OF, published_at="2026-11-05",
            body="ACME net revenue was up 8% year over year."):
    doc = new_document(
        ticker=ticker, kind="filing", source_url=url, approved_domain="sec.gov",
        retrieved_at=retrieved_at, published_at=published_at, raw=body.encode(), text=body,
        title="10-Q", sanitizer_version=SANITIZER_VERSION,
    )
    store.put(doc)
    return doc


def _prov(doc):
    return DocumentProvenance.from_document(doc)


def _mk_finding(doc, *, prov=None, extra_prov=(), ticker="ACME", claim="net revenue was up 8%",
                quote="ACME net revenue was up 8% year over year."):
    cit = Citation(doc.doc_id, quote, doc.source_url)
    ev = Evidence(citation=cit, supports_claim=True, relevance_note="ok")
    return VerifiedFinding(
        finding_id="F1", ticker=ticker, created_at=AS_OF, claim=claim, evidence=(ev,),
        reason="r", falsifier="f", confidence=0.5, model="m", prompt_version="v1",
        source_docs=(doc.doc_id,), provenance=(prov or _prov(doc), *extra_prov),
    )


@pytest.fixture()
def store(tmp_path):
    return DocumentStore(tmp_path / "docs")


# ---- 1. distinct dates, normal case ---------------------------------------

def test_distinct_dates_present_and_labeled(store):
    doc = _mk_doc(store)
    tc = build_temporal_context(_mk_finding(doc), store, as_of=AS_OF)
    d = tc.documents[0]
    assert d.published_at == "2026-11-05"
    assert d.retrieved_at == AS_OF
    assert d.published_at != d.retrieved_at
    assert d.published_at_status == "known"
    assert d.age_days == 2
    assert tc.recency == "recent"
    assert tc.evidence_age_days == 2


# ---- 2. never back-fill ----------------------------------------------------

def test_unknown_published_at_is_not_backfilled(store):
    doc = _mk_doc(store, published_at=None)
    tc = build_temporal_context(_mk_finding(doc), store, as_of=AS_OF)
    d = tc.documents[0]
    assert d.published_at is None
    assert d.published_at is not d.retrieved_at
    assert d.published_at_status == "unknown"
    assert d.age_days is None
    assert tc.recency == "unknown"
    assert tc.evidence_age_days is None


# ---- 3. old document is not breaking news ---------------------------------

def test_old_document_is_not_recent(store):
    doc = _mk_doc(store, published_at="2019-01-02")
    tc = build_temporal_context(_mk_finding(doc), store, as_of=AS_OF)
    assert tc.recency == "older"
    assert tc.evidence_age_days is not None and tc.evidence_age_days > 30


# ---- 4. unknown never recent ----------------------------------------------

def test_unknown_never_recent(store):
    doc = _mk_doc(store, published_at=None)
    tc = build_temporal_context(_mk_finding(doc), store, as_of=AS_OF)
    assert tc.recency == "unknown"


# ---- 5 / 11 / 12. impossible + strict format ------------------------------

def test_published_after_retrieved_is_invalid(store):
    # published 2026-11-05 but retrieved 2026-11-01 (retrieved before published)
    doc = _mk_doc(store, retrieved_at="2026-11-01", published_at="2026-11-05")
    tc = build_temporal_context(_mk_finding(doc), store, as_of=AS_OF)
    d = tc.documents[0]
    assert d.published_at_status == "invalid"
    assert d.age_days is None
    assert tc.recency == "unknown"


def test_published_after_as_of_is_invalid(store):
    # retrieved_at within as_of, but published_at is in the future relative to as_of.
    doc = _mk_doc(store, retrieved_at="2026-11-06", published_at="2026-11-08")
    tc = build_temporal_context(_mk_finding(doc), store, as_of=AS_OF)
    assert tc.documents[0].published_at_status == "invalid"
    assert tc.recency == "unknown"


def test_compact_date_is_invalid_not_recent(store):
    # M1's validator accepts "20261107"; M4b must classify it invalid (Option A).
    doc = _mk_doc(store, published_at="20261107")
    assert doc.published_at == "20261107"  # stored as-is; M4b does NOT normalize
    tc = build_temporal_context(_mk_finding(doc), store, as_of=AS_OF)
    d = tc.documents[0]
    assert d.published_at_status == "invalid"
    assert d.age_days is None
    assert tc.recency == "unknown"


@pytest.mark.parametrize("bad", ["20261107", "2026/11/07", "2026-2-3", "2026-02-30", "2026-13-01",
                                 "not-a-date", "", "2026-11-07T00:00:00Z"])
def test_parse_strict_date_rejects_non_strict(bad):
    assert _parse_strict_date(bad) is None


@pytest.mark.parametrize("good", ["2026-11-07", "2028-02-29"])
def test_parse_strict_date_accepts_strict(good):
    assert _parse_strict_date(good) == _date.fromisoformat(good)


def test_retrieved_at_after_as_of_fails_closed(store):
    doc = _mk_doc(store, retrieved_at="2026-11-08", published_at="2026-11-05")
    with pytest.raises(ContextError, match="after as_of"):
        build_temporal_context(_mk_finding(doc), store, as_of=AS_OF)


def test_invalid_retrieved_at_fails_closed(store):
    # "20261107" passes M1's validator but is not strict YYYY-MM-DD -> ContextError.
    doc = _mk_doc(store, retrieved_at="20261107", published_at="2026-11-05")
    with pytest.raises(ContextError, match="retrieved_at"):
        build_temporal_context(_mk_finding(doc), store, as_of=AS_OF)


def test_invalid_as_of_fails_closed(store):
    doc = _mk_doc(store)
    for bad in ["20261107", "2026-2-3", "2026-02-30", "nope"]:
        with pytest.raises(ContextError, match="as_of"):
            build_temporal_context(_mk_finding(doc), store, as_of=bad)


# ---- 6. determinism --------------------------------------------------------

def test_determinism(store):
    doc = _mk_doc(store)
    f = _mk_finding(doc)
    assert build_temporal_context(f, store, as_of=AS_OF) == build_temporal_context(f, store, as_of=AS_OF)


def test_as_of_changes_only_age_not_provenance(store):
    doc = _mk_doc(store)
    f = _mk_finding(doc)
    a = build_temporal_context(f, store, as_of="2026-11-07")
    b = build_temporal_context(f, store, as_of="2026-12-07")
    assert a.documents[0].doc_id == b.documents[0].doc_id
    assert a.documents[0].raw_hash == b.documents[0].raw_hash
    assert a.evidence_age_days == 2 and b.evidence_age_days == 32
    assert a.recency == "recent" and b.recency == "older"


# ---- 7. multi-document conservative ---------------------------------------

def test_multi_doc_oldest_drives_recency(store):
    fresh = _mk_doc(store, url="https://www.sec.gov/Archives/fresh.htm", published_at="2026-11-06")
    old = _mk_doc(store, url="https://www.sec.gov/Archives/old.htm", published_at="2020-01-01")
    f = _mk_finding(fresh, extra_prov=(_prov(old),))
    tc = build_temporal_context(f, store, as_of=AS_OF)
    assert tc.recency == "older"
    assert tc.evidence_age_days == ( _date.fromisoformat(AS_OF) - _date.fromisoformat("2020-01-01")).days


def test_multi_doc_any_unknown_forces_unknown(store):
    known = _mk_doc(store, url="https://www.sec.gov/Archives/a.htm", published_at="2026-11-06")
    unknown = _mk_doc(store, url="https://www.sec.gov/Archives/b.htm", published_at=None)
    f = _mk_finding(known, extra_prov=(_prov(unknown),))
    tc = build_temporal_context(f, store, as_of=AS_OF)
    assert tc.recency == "unknown"
    assert tc.evidence_age_days is None


# ---- 8. provenance preserved ----------------------------------------------

def test_provenance_intact_and_only_passing_docs(store):
    doc = _mk_doc(store)
    tc = build_temporal_context(_mk_finding(doc), store, as_of=AS_OF)
    d = tc.documents[0]
    assert d.doc_id == doc.doc_id
    assert d.ticker == "ACME"
    assert d.source_url == URL
    assert d.title == "10-Q"
    assert d.kind == "filing"
    assert d.raw_hash == doc.raw_hash
    assert d.content_hash == doc.content_hash
    assert d.sanitizer_version == SANITIZER_VERSION
    assert [x.doc_id for x in tc.documents] == [doc.doc_id]


# ---- 13. store/provenance consistency (fail closed) -----------------------

def test_missing_document_fails_closed(store):
    doc = _mk_doc(store)
    f = _mk_finding(doc)
    (store.root / f"{doc.doc_id}.json").unlink()  # remove after finding built
    with pytest.raises(ContextError, match="not found"):
        build_temporal_context(f, store, as_of=AS_OF)


def test_corrupt_document_fails_closed(store):
    doc = _mk_doc(store)
    f = _mk_finding(doc)
    (store.root / f"{doc.doc_id}.json").write_text("{ not json", encoding="utf-8")
    with pytest.raises(ContextError, match="corrupt document"):
        build_temporal_context(f, store, as_of=AS_OF)


def test_stored_document_id_mismatch_fails_closed(store, monkeypatch):
    # Defense in depth: if the store ever returns a document whose id is not the one requested,
    # M4b must fail closed rather than build context from the wrong document.
    doc = _mk_doc(store)
    f = _mk_finding(doc)
    other = _mk_doc(store, url="https://www.sec.gov/Archives/other.htm")
    monkeypatch.setattr(store, "get", lambda doc_id: other)
    with pytest.raises(ContextError, match="document ID mismatch"):
        build_temporal_context(f, store, as_of=AS_OF)


def test_provenance_mismatch_fails_closed(store):
    doc = _mk_doc(store)
    wrong = DocumentProvenance(
        doc_id=doc.doc_id, ticker="OTHER", source_url=doc.source_url,
        retrieved_at=doc.retrieved_at, published_at=doc.published_at,
        raw_hash=doc.raw_hash, content_hash=doc.content_hash,
        sanitizer_version=doc.sanitizer_version,
    )
    f = _mk_finding(doc, prov=wrong)
    with pytest.raises(ContextError, match="provenance mismatch"):
        build_temporal_context(f, store, as_of=AS_OF)


def test_no_supporting_documents_fails_closed(store):
    _mk_doc(store)
    empty = VerifiedFinding(
        finding_id="F", ticker="ACME", created_at=AS_OF, claim="c", evidence=(),
        reason="r", falsifier="f", confidence=0.5, model="m", prompt_version="v1",
        source_docs=(), provenance=(),
    )
    with pytest.raises(ContextError, match="no supporting documents"):
        build_temporal_context(empty, store, as_of=AS_OF)


# ---- 14. duplicate-document dedup -----------------------------------------

def test_duplicate_doc_id_collapses_to_one_entry(store):
    doc = _mk_doc(store)
    f = _mk_finding(doc, extra_prov=(_prov(doc),))  # same doc twice, identical provenance
    tc = build_temporal_context(f, store, as_of=AS_OF)
    assert len(tc.documents) == 1


def test_conflicting_duplicate_provenance_fails_closed(store):
    doc = _mk_doc(store)
    conflicting = DocumentProvenance(
        doc_id=doc.doc_id, ticker=doc.ticker, source_url=doc.source_url,
        retrieved_at=doc.retrieved_at, published_at="2000-01-01",  # differs
        raw_hash=doc.raw_hash, content_hash=doc.content_hash,
        sanitizer_version=doc.sanitizer_version,
    )
    f = _mk_finding(doc, extra_prov=(conflicting,))
    with pytest.raises(ContextError, match="conflicting provenance"):
        build_temporal_context(f, store, as_of=AS_OF)


# ---- 15. boundary 30 / 31 --------------------------------------------------

@pytest.mark.parametrize("published,expected", [
    ("2026-10-08", "recent"),   # 30 days
    ("2026-10-07", "older"),    # 31 days
    ("2026-11-07", "recent"),   # 0 days
])
def test_recent_window_boundary(store, published, expected):
    doc = _mk_doc(store, retrieved_at="2026-11-07", published_at=published)
    tc = build_temporal_context(_mk_finding(doc), store, as_of=AS_OF)
    assert tc.recency == expected


# ---- 10. leap-year / month-end arithmetic ---------------------------------

@pytest.mark.parametrize("as_of,published,age", [
    ("2028-03-01", "2028-02-29", 1),   # leap day
    ("2026-03-01", "2026-02-28", 1),   # non-leap Feb end
    ("2026-01-01", "2025-12-31", 1),   # year boundary
])
def test_calendar_arithmetic(store, as_of, published, age):
    doc = _mk_doc(store, retrieved_at=as_of, published_at=published)
    tc = build_temporal_context(_mk_finding(doc), store, as_of=as_of)
    assert tc.documents[0].age_days == age


# ---- 16. no wall clock -----------------------------------------------------

def test_no_wall_clock_static():
    src = inspect.getsource(ctx)
    assert "today(" not in src
    assert "date.today" not in src


def test_no_wall_clock_runtime(store, monkeypatch):
    doc = _mk_doc(store)
    f = _mk_finding(doc)

    class NoToday(_date):
        @classmethod
        def today(cls):
            raise AssertionError("wall clock was used")

    monkeypatch.setattr(ctx, "_date", NoToday)
    tc = build_temporal_context(f, store, as_of=AS_OF)  # must not call today()
    assert tc.recency == "recent"


# ---- 9. isolation ----------------------------------------------------------

def test_isolation_no_phase1_imports():
    src = inspect.getsource(ctx)
    for forbidden in ("risk", "ledger", "report", "monitor", "prices", "sanity"):
        assert f"import {forbidden}" not in src
        assert f"from .{forbidden}" not in src


def test_type_guard(store):
    _mk_doc(store)
    with pytest.raises(TypeError, match="VerifiedFinding"):
        build_temporal_context({"not": "a finding"}, store, as_of=AS_OF)
