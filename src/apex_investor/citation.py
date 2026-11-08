"""Deterministic citation + relevance gate (Phase 2 / M4).

Scope (M4): given a *proposed* finding (a claim + citations drafted elsewhere, later by a model)
and a document store, decide — with NO model calls, NO network, NO ledger contact — whether the
finding may be kept. It is pure and deterministic: same inputs -> same output.

It performs two things, and keeps them strictly separate:

  1. CITATION-EXISTENCE / VERBATIM gate (deterministic, exact):
     every cited `doc_id` must exist in the store, the citation's `source_url` and the finding's
     ticker must match the stored document, and `quote_span` must be a *verbatim substring* of the
     stored SANITIZED text. This proves the quotation exists in a real document.

  2. RELEVANCE gate (deterministic, conservative):
     existence does NOT prove support. A quote can be genuine yet unrelated (or negate the claim).
     `supports_claim` and `relevance_note` are assigned HERE, never by the model. The mechanism is
     deliberately strict: a claim is supported only if (a) ALL of the claim's significant content
     terms are grounded in the quoted span and (b) the quote does not introduce a negation the
     claim lacks. Topical overlap alone is never treated as proof.

Fail closed: missing, malformed, corrupt, unsupported, or ambiguous evidence DROPS the finding
(`status == "dropped"`). A finding is kept only when at least one citation passes BOTH gates.

HONEST LIMITATION — the relevance gate is not semantic understanding:
  It is a deterministic lexical/negation check. It can reject obviously unrelated evidence, but it
  CANNOT reliably establish that a quote semantically supports a claim. It will reject some
  genuinely-supporting paraphrases (a deliberate false-negative, the safe direction) and it can
  still be fooled by an adversarial paraphrase that shares the claim's terms (a possible
  false-positive). A passing test demonstrates resistance to the TESTED cases only. Because of
  this, the gate is one layer of defense (§4.1) and never the sole basis for acting on a finding.

Preserves provenance: a kept finding's verified output carries the full stored-document provenance
(doc_id, ticker, source_url, retrieved_at, published_at, raw_hash, content_hash,
sanitizer_version) for each PASSING citation only, so the verified result is safe by construction;
failed citations remain available in `GateResult.evaluated_evidence` for audit. See §4.2 / §7.2-3,
§7.10.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass

from .docstore import Document, DocumentIntegrityError, DocumentStore, is_valid_doc_id

_TOKEN_RE = re.compile(r"[a-z0-9]+")

# Normalize curly/typographic apostrophes to ASCII so contractions tokenize consistently.
_APOSTROPHES = str.maketrans({"\u2019": "'", "\u2018": "'", "\u02bc": "'", "\u2032": "'"})

# Expand common negation contractions to explicit words BEFORE tokenization, so the negation
# markers below are actually seen (the tokenizer would otherwise split "didn't" into "did" + "t").
# Whole-word, case-insensitive; ordered longest-first so "won't" is not shadowed by "wo".
_CONTRACTIONS = {
    "cannot": "cannot",
    "can't": "cannot",
    "won't": "will not",
    "shan't": "shall not",
    "isn't": "is not",
    "wasn't": "was not",
    "aren't": "are not",
    "weren't": "were not",
    "don't": "do not",
    "doesn't": "does not",
    "didn't": "did not",
    "haven't": "have not",
    "hasn't": "has not",
    "hadn't": "had not",
    "couldn't": "could not",
    "shouldn't": "should not",
    "wouldn't": "would not",
    "mustn't": "must not",
    "needn't": "need not",
    "ain't": "is not",
}
# Fallback for any other trailing "n't" (e.g. "mightn't"): expand to " not" so the negation is not
# missed. The apostrophe is required, so ordinary words ending in "nt" are untouched.
_CONTRACTION_NEG_RE = re.compile(r"n't\b", re.IGNORECASE)
_CONTRACTION_WORD_RES = {
    re.compile(rf"\b{re.escape(k)}\b", re.IGNORECASE): v for k, v in _CONTRACTIONS.items()
}

# A quote must be at least this long to count: a one-word match proves little.
MIN_QUOTE_CHARS = 8

# The only permitted range for `min_grounded_ratio`. The default is 1.0; the lower bound is
# EXCLUSIVE so a caller cannot pass 0 (or a negative) and disable the grounding requirement.
_MIN_GROUNDED_RATIO_FLOOR = 0.0

# Function words ignored when grounding a claim in a quote. Negation markers are deliberately NOT
# here — they are meaningful and are checked separately.
_STOPWORDS = frozenset("""
a an and are as at be been being but by for from had has have he her his i in into is it its of
on or our that the their them then there these they this to was we were what when where which who
will with would you your s t
""".split())

# Explicit negation markers. If a quote contains one that the claim does not, support is treated as
# ambiguous and rejected (a claim negated by its own evidence must never be auto-accepted). "not"
# is included because trailing contractions are expanded to "not" before tokenization.
_NEGATION_MARKERS = frozenset({
    "not", "no", "never", "none", "cannot", "nor", "neither", "without", "lack", "lacks",
    "lacked", "denies", "denied", "false", "fails", "failed",
})

KEPT = "kept"
DROPPED = "dropped"


@dataclass(frozen=True)
class Citation:
    """A claim's pointer to a quotation. `quote_span` must exist in the stored text."""

    doc_id: str
    quote_span: str
    source_url: str


@dataclass(frozen=True)
class Evidence:
    """A citation plus the gate's verdict. `supports_claim` is set by the gate, not the model."""

    citation: Citation
    supports_claim: bool
    relevance_note: str


@dataclass(frozen=True)
class DocumentProvenance:
    """The provenance of a stored document cited by a finding (preserved, never invented)."""

    doc_id: str
    ticker: str
    source_url: str
    retrieved_at: str
    published_at: str | None
    raw_hash: str
    content_hash: str
    sanitizer_version: str

    @classmethod
    def from_document(cls, doc: Document) -> DocumentProvenance:
        return cls(
            doc_id=doc.doc_id, ticker=doc.ticker, source_url=doc.source_url,
            retrieved_at=doc.retrieved_at, published_at=doc.published_at,
            raw_hash=doc.raw_hash, content_hash=doc.content_hash,
            sanitizer_version=doc.sanitizer_version,
        )


@dataclass(frozen=True)
class ProposedFinding:
    """A claim with citations, as drafted upstream (later, by a model). Not yet verified."""

    finding_id: str
    ticker: str
    created_at: str
    claim: str
    citations: tuple[Citation, ...]
    reason: str
    falsifier: str
    confidence: float
    model: str
    prompt_version: str
    source_docs: tuple[str, ...]

    def __post_init__(self) -> None:
        for name in ("finding_id", "ticker", "created_at", "claim", "model", "prompt_version"):
            if not isinstance(getattr(self, name), str) or not getattr(self, name):
                raise ValueError(f"{name} must be a non-empty string")
        if not isinstance(self.reason, str):
            raise ValueError("reason must be a string")
        if not isinstance(self.falsifier, str):
            raise ValueError("falsifier must be a string")
        if isinstance(self.confidence, bool) or not isinstance(self.confidence, (int, float)):
            raise ValueError("confidence must be a number in [0, 1]")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("confidence must be in [0, 1]")
        if not isinstance(self.citations, tuple) or any(
            not isinstance(c, Citation) for c in self.citations
        ):
            raise ValueError("citations must be a tuple of Citation")
        if not isinstance(self.source_docs, tuple) or any(
            not isinstance(d, str) for d in self.source_docs
        ):
            raise ValueError("source_docs must be a tuple of str")


@dataclass(frozen=True)
class VerifiedFinding:
    """A finding that passed both gates, with gate-assigned evidence and document provenance."""

    finding_id: str
    ticker: str
    created_at: str
    claim: str
    evidence: tuple[Evidence, ...]
    reason: str
    falsifier: str
    confidence: float
    model: str
    prompt_version: str
    source_docs: tuple[str, ...]
    provenance: tuple[DocumentProvenance, ...]


@dataclass(frozen=True)
class GateResult:
    """Outcome of the gate. On `dropped`, `finding` is None and `dropped_reason` explains why."""

    status: str
    finding: VerifiedFinding | None
    dropped_reason: str | None
    evaluated_evidence: tuple[Evidence, ...] = ()

    @property
    def kept(self) -> bool:
        return self.status == KEPT


def _expand_contractions(text: str) -> str:
    text = text.translate(_APOSTROPHES)
    for pattern, repl in _CONTRACTION_WORD_RES.items():
        text = pattern.sub(repl, text)
    return _CONTRACTION_NEG_RE.sub(" not", text)


def _content_terms(text: str) -> frozenset[str]:
    normalized = _expand_contractions(text.lower())
    return frozenset(
        t for t in _TOKEN_RE.findall(normalized)
        if t not in _STOPWORDS and (len(t) > 1 or t.isdigit())
    )


def _has_negation(text: str) -> bool:
    return bool(_content_terms(text) & _NEGATION_MARKERS)


def _validate_min_grounded_ratio(value: object) -> float:
    """Return `value` if it is a finite number in (0, 1]; otherwise raise ValueError.

    Rejects booleans (a bool is an int in Python), NaN, +/-infinity, non-numbers, and anything
    outside the permitted range. This prevents a caller from disabling the grounding requirement
    (e.g. 0, negative, or NaN — against which every comparison is False) or loosening it silently.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"min_grounded_ratio must be a number in (0, 1], got {value!r}")
    try:
        finite = math.isfinite(value)
    except OverflowError:
        # An int too large to convert to float is certainly not in (0, 1]; report it as such rather
        # than leaking an OverflowError.
        finite = False
    if not finite:
        raise ValueError(f"min_grounded_ratio must be finite, got {value!r}")
    if not (_MIN_GROUNDED_RATIO_FLOOR < value <= 1.0):
        raise ValueError(f"min_grounded_ratio must be in (0, 1], got {value!r}")
    return float(value)


def supports_claim(claim: str, quote_span: str, *, min_grounded_ratio: float = 1.0) -> tuple[bool, str]:
    """Deterministic, conservative verdict: does `quote_span` support `claim`?

    Returns (supported, note). `min_grounded_ratio` is the fraction of the claim's significant
    content terms that must appear in the quote; the default 1.0 requires ALL of them (strict, so
    ambiguous or partial support is rejected). It must be a finite number in (0, 1]; booleans, NaN,
    infinity, and out-of-range values raise ValueError. This is a lexical/negation check, NOT
    semantic understanding — see the module docstring.
    """
    min_grounded_ratio = _validate_min_grounded_ratio(min_grounded_ratio)
    quote = quote_span.strip()
    if not quote:
        return False, "quote is empty"
    if len(quote) < MIN_QUOTE_CHARS:
        return False, f"quote shorter than {MIN_QUOTE_CHARS} characters"
    claim_terms = _content_terms(claim)
    if not claim_terms:
        return False, "claim has no verifiable content terms"
    quote_terms = _content_terms(quote)
    grounded = claim_terms & quote_terms
    ratio = len(grounded) / len(claim_terms)
    if ratio < min_grounded_ratio:
        missing = sorted(claim_terms - quote_terms)
        return False, f"claim terms not grounded in quote: {missing}"
    claim_negated = _has_negation(claim)
    quote_negated = _has_negation(quote)
    if claim_negated != quote_negated:
        if quote_negated:
            return False, "quote contains negation absent from the claim (ambiguous)"
        return False, "claim is negated but the quote is not (ambiguous)"
    return True, "claim terms grounded in quote"


def _drop(reason: str, evidence: tuple[Evidence, ...] = ()) -> GateResult:
    return GateResult(status=DROPPED, finding=None, dropped_reason=reason, evaluated_evidence=evidence)


def evaluate(
    finding: ProposedFinding, store: DocumentStore, *, min_grounded_ratio: float = 1.0
) -> GateResult:
    """Run both gates over one proposed finding. Deterministic; never mutates the store."""
    if not isinstance(finding, ProposedFinding):
        raise TypeError("finding must be a ProposedFinding")
    # Validate the threshold up front, BEFORE any early return, so an invalid value is always
    # rejected even on paths that never reach supports_claim (e.g. a finding with no citations).
    min_grounded_ratio = _validate_min_grounded_ratio(min_grounded_ratio)
    if not finding.citations:
        return _drop("no citations provided")

    evidence: list[Evidence] = []
    provenance: list[DocumentProvenance] = []

    for cit in finding.citations:
        if not is_valid_doc_id(cit.doc_id):
            return _drop(f"invalid doc_id {cit.doc_id!r}")
        try:
            doc = store.get(cit.doc_id)
        except DocumentIntegrityError as exc:
            return _drop(f"corrupt document {cit.doc_id}: {exc}")
        if doc is None:
            return _drop(f"unknown doc_id {cit.doc_id}")
        if cit.source_url != doc.source_url:
            return _drop(f"citation source_url does not match stored document {cit.doc_id}")
        if doc.ticker != finding.ticker:
            return _drop(
                f"document ticker {doc.ticker!r} does not match finding ticker {finding.ticker!r}")
        if cit.doc_id not in finding.source_docs:
            return _drop(f"doc_id {cit.doc_id} is not listed in source_docs")
        if cit.quote_span not in doc.text:
            return _drop(f"quote_span not found verbatim in document {cit.doc_id}")
        ok, note = supports_claim(finding.claim, cit.quote_span,
                                  min_grounded_ratio=min_grounded_ratio)
        evidence.append(Evidence(citation=cit, supports_claim=ok, relevance_note=note))
        provenance.append(DocumentProvenance.from_document(doc))

    # Keep the finding only if at least one citation passed BOTH gates; otherwise drop it (with the
    # full evaluated evidence, including failures, attached for audit).
    passing = [i for i, e in enumerate(evidence) if e.supports_claim]
    if not passing:
        return _drop("no citation supports the claim", evidence=tuple(evidence))

    # The verified output carries ONLY passing evidence, so a kept finding is safe by construction:
    # a downstream consumer never has to remember to filter out supports_claim=False entries.
    verified = VerifiedFinding(
        finding_id=finding.finding_id, ticker=finding.ticker, created_at=finding.created_at,
        claim=finding.claim, evidence=tuple(evidence[i] for i in passing), reason=finding.reason,
        falsifier=finding.falsifier, confidence=finding.confidence, model=finding.model,
        prompt_version=finding.prompt_version,
        source_docs=tuple(evidence[i].citation.doc_id for i in passing),
        provenance=tuple(provenance[i] for i in passing),
    )
    return GateResult(status=KEPT, finding=verified, dropped_reason=None,
                      evaluated_evidence=tuple(evidence))


def evaluate_all(
    findings: tuple[ProposedFinding, ...] | list[ProposedFinding],
    store: DocumentStore,
    *,
    min_grounded_ratio: float = 1.0,
) -> list[GateResult]:
    """Evaluate many findings; kept and dropped are both returned (drops carry their reason)."""
    # Validate up front so an empty batch still rejects an invalid threshold (the per-item
    # evaluate() never runs when `findings` is empty).
    min_grounded_ratio = _validate_min_grounded_ratio(min_grounded_ratio)
    return [evaluate(f, store, min_grounded_ratio=min_grounded_ratio) for f in findings]
