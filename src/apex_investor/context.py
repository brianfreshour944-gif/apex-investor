"""Date/context handling (Phase 2 / M4b).

Scope (M4b): given an already-verified finding (one that passed the M4 citation + relevance gate)
and a document store, attach the publication and retrieval dates and document context to its
evidence, and classify recency as DATA. It is pure and deterministic: same inputs -> same output,
NO wall clock, NO model, NO network, NO ledger contact, NO report integration.

The M4 gate (`citation.py`) is NOT modified. M4b consumes `VerifiedFinding`, whose `provenance` is
already filtered to the PASSING citations only, so M4b inherits that guarantee.

Core rules:
  - DATES ARE DISTINCT. `published_at` (when the document was published) and `retrieved_at` (when
    we fetched it) are carried separately and never conflated.
  - NEVER BACK-FILL. An unknown `published_at` stays unknown; it is never set from `retrieved_at`.
  - UNKNOWN IS EXPLICIT. Unknown `published_at` -> status "unknown"; it can never yield "recent".
  - IMPOSSIBLE DATES ARE FLAGGED, NOT TRUSTED. `published_at` after `retrieved_at` or after `as_of`
    -> status "invalid"; age is None. The evidence is preserved, but its date cannot establish
    recency. An invalid `published_at` does NOT raise.
  - STRICT FORMAT. Only exactly `YYYY-MM-DD` is accepted (regex + real-calendar check). A compact
    date such as `20261107` — even though the M1 store's own validator accepts it — is treated as
    INVALID here, so it can never support a freshness label.
  - CONSERVATIVE. A finding is "recent" only when EVERY supporting document has a valid publication
    date and the OLDEST of them is within `RECENT_WINDOW_DAYS`. Any unknown/invalid date makes the
    whole finding "unknown"; a single fresh citation cannot make stale evidence look fresh.
  - NO WALL CLOCK. `as_of` is an explicit input; the system clock is never consulted.

Fail closed: an invalid `as_of`, an invalid `retrieved_at`, `retrieved_at` after `as_of`, a missing,
corrupt, or provenance-mismatched stored document, or the absence of any supporting document raises
`ContextError`. No partial or fabricated context is ever returned.

HONEST LIMITATION: this module reports dates and a coarse recency label; it is not an event-study
engine. It does not interpret a SEC "period of report" (deferred), and "recent" is a fixed-window
label, not a claim of materiality or price impact.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date as _date

from .citation import DocumentProvenance, VerifiedFinding
from .docstore import Document, DocumentIntegrityError, DocumentStore

# Recency labels (exactly three; no "partial").
RECENT = "recent"
OLDER = "older"
UNKNOWN = "unknown"

# Publication-date statuses.
KNOWN = "known"
INVALID = "invalid"

# A finding is "recent" when the oldest supporting document is at most this many days old.
RECENT_WINDOW_DAYS = 30

# Strict date: exactly four digits, dash, two, dash, two. Deliberately narrower than
# `date.fromisoformat`, which also accepts forms like "20261107".
_STRICT_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class ContextError(Exception):
    """Raised when date context cannot be built safely (fail closed)."""


@dataclass(frozen=True)
class DocumentDateContext:
    """Dates and preserved provenance for one distinct supporting document."""

    doc_id: str
    ticker: str
    source_url: str
    title: str
    kind: str
    published_at: str | None      # None = unknown; never back-filled
    retrieved_at: str
    published_at_status: str      # "known" | "unknown" | "invalid"
    age_days: int | None          # None unless published_at_status == "known"
    raw_hash: str
    content_hash: str
    sanitizer_version: str


@dataclass(frozen=True)
class TemporalContext:
    """Recency classification for a verified finding, with per-document context."""

    as_of: str
    recency: str                              # "recent" | "older" | "unknown"
    evidence_age_days: int | None             # oldest doc age; None if any date unknown/invalid
    documents: tuple[DocumentDateContext, ...]


def _parse_strict_date(value: object) -> _date | None:
    """Return a `date` only for an exact, real `YYYY-MM-DD`; otherwise None."""
    if not isinstance(value, str) or not _STRICT_DATE_RE.match(value):
        return None
    try:
        return _date.fromisoformat(value)
    except ValueError:
        return None


def _require_strict_date(value: object, field: str) -> _date:
    parsed = _parse_strict_date(value)
    if parsed is None:
        raise ContextError(f"{field} must be exactly YYYY-MM-DD, got {value!r}")
    return parsed


def _distinct_provenance(provenance: tuple[DocumentProvenance, ...]) -> list[DocumentProvenance]:
    """One DocumentProvenance per distinct doc_id, in first-appearance order.

    Duplicate doc_ids with identical provenance collapse to one entry (dates belong to documents,
    not quote spans); duplicate doc_ids with DIFFERING provenance are a mismatch and fail closed.
    """
    seen: dict[str, DocumentProvenance] = {}
    for prov in provenance:
        existing = seen.get(prov.doc_id)
        if existing is None:
            seen[prov.doc_id] = prov
        elif existing != prov:
            raise ContextError(f"conflicting provenance for doc_id {prov.doc_id}")
    return list(seen.values())


def _verify_stored(doc_id: str, expected: DocumentProvenance, store: DocumentStore) -> Document:
    """Fetch the stored document and confirm its provenance matches the finding's (fail closed)."""
    try:
        doc = store.get(doc_id)
    except DocumentIntegrityError as exc:
        raise ContextError(f"corrupt document {doc_id}: {exc}") from exc
    if doc is None:
        raise ContextError(f"document {doc_id} not found in store")
    # Defense in depth: the store keys documents by id and verifies the id on read, but assert the
    # returned document's identity explicitly so a wrongly-returned document fails closed here.
    if doc.doc_id != doc_id:
        raise ContextError(f"document ID mismatch: requested {doc_id}, stored {doc.doc_id}")
    checks = (
        ("ticker", doc.ticker, expected.ticker),
        ("source_url", doc.source_url, expected.source_url),
        ("retrieved_at", doc.retrieved_at, expected.retrieved_at),
        ("published_at", doc.published_at, expected.published_at),
        ("raw_hash", doc.raw_hash, expected.raw_hash),
        ("content_hash", doc.content_hash, expected.content_hash),
        ("sanitizer_version", doc.sanitizer_version, expected.sanitizer_version),
    )
    for name, stored, claimed in checks:
        if stored != claimed:
            raise ContextError(
                f"provenance mismatch for {doc_id}: {name} stored={stored!r} finding={claimed!r}")
    return doc


def _document_context(doc: Document, as_of: _date) -> DocumentDateContext:
    # retrieved_at is required to be strict and not after as_of (fail closed).
    retrieved = _require_strict_date(doc.retrieved_at, f"retrieved_at of {doc.doc_id}")
    if retrieved > as_of:
        raise ContextError(
            f"retrieved_at {doc.retrieved_at} of {doc.doc_id} is after as_of {as_of.isoformat()}")

    if doc.published_at is None:
        status, age = UNKNOWN, None
    else:
        published = _parse_strict_date(doc.published_at)
        if published is None or published > retrieved or published > as_of:
            status, age = INVALID, None
        else:
            status, age = KNOWN, (as_of - published).days

    return DocumentDateContext(
        doc_id=doc.doc_id, ticker=doc.ticker, source_url=doc.source_url, title=doc.title,
        kind=doc.kind, published_at=doc.published_at, retrieved_at=doc.retrieved_at,
        published_at_status=status, age_days=age, raw_hash=doc.raw_hash,
        content_hash=doc.content_hash, sanitizer_version=doc.sanitizer_version,
    )


def build_temporal_context(
    finding: VerifiedFinding, store: DocumentStore, *, as_of: str
) -> TemporalContext:
    """Attach dates and a recency label to a verified finding's passing evidence.

    Deterministic; never mutates the store; never reads the clock. Raises `ContextError` on any
    fail-closed condition. See the module docstring for the rules.
    """
    if not isinstance(finding, VerifiedFinding):
        raise TypeError("finding must be a VerifiedFinding")
    as_of_date = _require_strict_date(as_of, "as_of")

    distinct = _distinct_provenance(finding.provenance)
    if not distinct:
        raise ContextError("finding has no supporting documents")

    documents = tuple(
        _document_context(_verify_stored(prov.doc_id, prov, store), as_of_date)
        for prov in distinct
    )

    if any(d.published_at_status != KNOWN for d in documents):
        evidence_age_days: int | None = None
        recency = UNKNOWN
    else:
        evidence_age_days = max(d.age_days for d in documents if d.age_days is not None)
        recency = RECENT if evidence_age_days <= RECENT_WINDOW_DAYS else OLDER

    return TemporalContext(as_of=as_of, recency=recency, evidence_age_days=evidence_age_days,
                           documents=documents)
