"""Advisory builder (Phase 2 / M5).

Maps already-verified findings (M4 `VerifiedFinding`) plus their date context (M4b
`TemporalContext`) into the neutral display types in `advisory.py`. It imports `citation.py` and
`context.py` — the report (`report.py`) does NOT import this module.

FAIL CLOSED, SECTION-LEVEL: any inconsistency raises `AdvisoryBuildError`, and the caller turns
that into `AdvisorySection(available=False)`. One bad finding makes the WHOLE section unavailable;
the builder never silently omits a finding or renders a partial set. Phase 1 still generates.

The builder does no arithmetic on the portfolio, calls no model, touches no network and never
mutates the store. It only reshapes data and joins each citation to its temporal document BY
doc_id — if a citation cannot be matched to its context, it raises rather than borrowing another
document's dates.
"""
from __future__ import annotations

from collections.abc import Iterable

from .advisory import AdvisoryEvidence, AdvisoryItem, AdvisorySection
from .citation import VerifiedFinding
from .context import (
    ContextError,
    DocumentDateContext,
    TemporalContext,
    build_temporal_context,
)
from .docstore import DocumentStore


class AdvisoryBuildError(Exception):
    """Raised when advisory data is internally inconsistent (fail closed)."""


def _evidence_for(finding: VerifiedFinding, by_doc: dict[str, DocumentDateContext]) -> tuple[AdvisoryEvidence, ...]:
    if not finding.evidence:
        raise AdvisoryBuildError(f"finding {finding.finding_id} has no passing evidence")
    out: list[AdvisoryEvidence] = []
    for ev in finding.evidence:
        # VerifiedFinding is defined as "passed both gates", so every Evidence here should carry the
        # gate's `supports_claim=True`. Assert it deliberately rather than trusting it implicitly: an
        # evidence record the gate did not accept must never be rendered.
        if not ev.supports_claim:
            raise AdvisoryBuildError(
                f"finding {finding.finding_id}: evidence for {ev.citation.doc_id} is not marked "
                "as supporting the claim")
        doc_id = ev.citation.doc_id
        doc = by_doc.get(doc_id)
        if doc is None:
            raise AdvisoryBuildError(
                f"finding {finding.finding_id}: citation doc_id {doc_id} has no temporal context")
        # Verify EVERY field present on both sides rather than silently trusting the context's
        # value. A citation citing one URL while its context carries another is a mismatch.
        if ev.citation.source_url != doc.source_url:
            raise AdvisoryBuildError(
                f"finding {finding.finding_id}: citation source_url {ev.citation.source_url!r} "
                f"does not match temporal context source_url {doc.source_url!r}")
        out.append(AdvisoryEvidence(
            doc_id=doc_id, quote=ev.citation.quote_span, source_url=doc.source_url,
            title=doc.title, published_at=doc.published_at, retrieved_at=doc.retrieved_at,
            published_at_status=doc.published_at_status,
        ))
    return tuple(out)


def _item_for(finding: VerifiedFinding, context: TemporalContext) -> AdvisoryItem:
    if finding.finding_id is None or finding.ticker is None:
        raise AdvisoryBuildError("finding is missing finding_id or ticker")
    by_doc = {d.doc_id: d for d in context.documents}
    evidence = _evidence_for(finding, by_doc)
    return AdvisoryItem(
        finding_id=finding.finding_id, ticker=finding.ticker, claim=finding.claim,
        reason=finding.reason, falsifier=finding.falsifier, confidence=finding.confidence,
        model=finding.model, prompt_version=finding.prompt_version, recency=context.recency,
        evidence_age_days=context.evidence_age_days, evidence=evidence,
    )


def build_advisory(
    pairs: Iterable[tuple[VerifiedFinding, TemporalContext]],
) -> AdvisorySection:
    """Build an `AdvisorySection` from (finding, context) pairs.

    Empty input -> `AdvisorySection(items=(), available=True)` ("No findings."). Any inconsistency
    raises `AdvisoryBuildError` (the caller maps that to `available=False`).
    """
    items: list[AdvisoryItem] = []
    for finding, context in pairs:
        if not isinstance(finding, VerifiedFinding):
            raise AdvisoryBuildError("expected a VerifiedFinding")
        if not isinstance(context, TemporalContext):
            raise AdvisoryBuildError("expected a TemporalContext")
        for doc in context.documents:
            if doc.ticker != finding.ticker:
                raise AdvisoryBuildError(
                    f"finding {finding.finding_id}: context document {doc.doc_id} ticker "
                    f"{doc.ticker!r} does not match finding ticker {finding.ticker!r}")
        items.append(_item_for(finding, context))
    return AdvisorySection(items=tuple(items), available=True)


def build_advisory_safe(
    pairs: Iterable[tuple[VerifiedFinding, TemporalContext]],
) -> AdvisorySection:
    """Orchestration entry point: build the section, or return an UNAVAILABLE section on any
    advisory-build failure.

    This is the caller-side half of the D5 boundary: the builder fails closed (raises), and this
    wrapper turns that into `AdvisorySection(available=False)` so the ordinary Phase 1 report is
    still generated. A rendering failure is caught separately by the report renderer.
    """
    try:
        return build_advisory(pairs)
    except AdvisoryBuildError:
        return AdvisorySection(items=(), available=False)


def build_section_for(
    findings: Iterable[VerifiedFinding], store: DocumentStore, *, as_of: str
) -> AdvisorySection:
    """Full orchestration path: build each finding's TemporalContext (M4b), then the section.

    Catches BOTH failure sources so Phase 1 always renders:
      - `ContextError` from `build_temporal_context` (M4b date/context failure), and
      - `AdvisoryBuildError` from the builder (inconsistent finding/evidence).
    Either way the result is `AdvisorySection(available=False)` ("No interpretation available."),
    never a raised exception reaching the report. The caller passes the returned section straight
    into `build_report(..., advisory=...)`.
    """
    try:
        pairs = [(f, build_temporal_context(f, store, as_of=as_of)) for f in findings]
    except ContextError:
        return AdvisorySection(items=(), available=False)
    return build_advisory_safe(pairs)
