"""Neutral advisory display types (Phase 2 / M5).

These dataclasses are the CONTRACT between the interpretation pipeline (`interpret.py`) and the
report renderer (`report.py`). They deliberately import NOTHING from the rest of the package: no
citation, context, model, ingestion, risk, ledger or report code. That keeps `report.py` free of
any dependency on the advisory pipeline — the report depends on a plain data type, not on the code
that produces it.

Scope: display data only. Nothing here computes equity/weight/gain, calls a model, touches the
network, or reads/writes the ledger.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AdvisoryEvidence:
    """One quoted span bound to its own source document and dates.

    A quote and its source are a single record so a rendered quote can never be paired with the
    wrong document. `published_at_status` is "known" | "unknown" | "invalid".
    """

    doc_id: str
    quote: str
    source_url: str
    title: str
    published_at: str | None
    retrieved_at: str
    published_at_status: str


@dataclass(frozen=True)
class AdvisoryItem:
    """One advisory finding, rendered for display only."""

    finding_id: str
    ticker: str
    claim: str
    reason: str
    falsifier: str
    confidence: float
    model: str
    prompt_version: str
    recency: str                  # "recent" | "older" | "unknown"
    evidence_age_days: int | None
    evidence: tuple[AdvisoryEvidence, ...]


@dataclass(frozen=True)
class AdvisorySection:
    """The advisory section payload.

    Three distinct states, which must not be conflated:
      - `advisory is None` (no section requested) -> the report has no section at all;
      - `available=False`                          -> "No interpretation available.";
      - `available=True, items=()`                 -> "No findings."
    """

    items: tuple[AdvisoryItem, ...]
    available: bool = True
