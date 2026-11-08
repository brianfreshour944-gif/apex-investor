"""M2 ingestion orchestration: held tickers -> fetch -> sanitize -> docstore (Phase 2).

Deterministic glue. It never mutates the ledger, never calls a model, and never invents document
identity: deduplication and storage reuse the accepted M1 docstore (`new_document` + `put`). Every
failure is converted into a recorded outcome, so a run always returns a result object rather than
raising.

Budget (deterministic): at most `MAX_DOCUMENTS_PER_RUN` documents are STORED per run. The budget is
checked BEFORE each fetch, so an eleventh fetch is never issued just to discover the limit. When the
limit is reached the run is flagged `incomplete=True`.

Dedup: classification uses CONTENT identity, not full-dataclass equality. `retrieved_at` (and
descriptive metadata such as `title`) legitimately change between runs and must NOT turn identical
stored content into an integrity conflict. A document whose content-identity fields all match an
existing one is a duplicate; any difference in those fields is an integrity conflict. Neither case
overwrites the existing document — the M1 docstore stays authoritative.

Manifest: an optional M2-owned append-only JSONL manifest (one record per attempted URL) records the
requested URL, final URL, ticker, outcome and retrieval date. It is separate from the document store
and contains no credentials or document bodies. A manifest-write failure is reported separately in
the run result and never affects a document that was already stored.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

try:
    import fcntl
except ImportError:  # pragma: no cover - native Windows has no fcntl
    fcntl = None  # type: ignore[assignment]

from .docstore import Document, DocumentStore, new_document
from .fetch import FetchedDocument, Fetcher, FetchError, PolicyViolation
from .sanitize import SANITIZER_VERSION, SanitizerError, sanitize
from .sources import held_tickers

# Indirection so tests can simulate a short `os.write`/`os.read` without patching `os` globally
# (which would also intercept the docstore's own file I/O).
_os_write = os.write
_os_read = os.read


def manifest_locking_supported() -> bool:
    """True iff the manifest's POSIX `flock` serialization is available on this platform.

    Ingestion is supported on POSIX systems (Linux/macOS) only: the append-and-recovery lock uses
    `fcntl.flock`, which does not exist on native Windows. The CLI checks this and fails clearly.
    """
    return fcntl is not None

MAX_DOCUMENTS_PER_RUN = 10
DEFAULT_KIND = "filing"
MANIFEST_NAME = "manifest.jsonl"

# Fields that define a document's CONTENT identity. `retrieved_at`, `title` and `published_at` are
# deliberately excluded: they may change between runs without the stored content having changed.
_CONTENT_IDENTITY_FIELDS = (
    "doc_id", "source_url", "ticker", "kind", "approved_domain",
    "raw_hash", "content_hash", "text", "sanitizer_version",
)


def _same_content(a: Document, b: Document) -> bool:
    """True iff two documents share the same content identity (see `_CONTENT_IDENTITY_FIELDS`)."""
    return all(getattr(a, name) == getattr(b, name) for name in _CONTENT_IDENTITY_FIELDS)


@dataclass
class IngestResult:
    """Structured outcome of an ingestion run (distinguishes all four outcomes)."""

    stored: list[str] = field(default_factory=list)            # doc_ids newly stored
    duplicates: list[str] = field(default_factory=list)        # doc_ids already present (same content)
    tickers_skipped: list[str] = field(default_factory=list)   # held tickers with no seed URL
    failures: list[dict] = field(default_factory=list)         # {ticker, url, reason}
    requested_to_final: dict[str, str] = field(default_factory=dict)
    manifest_failures: list[dict] = field(default_factory=list)  # {url, reason}; manifest write errors
    incomplete: bool = False

    def summary(self) -> str:
        return (f"stored={len(self.stored)} duplicates={len(self.duplicates)} "
                f"tickers_skipped={len(self.tickers_skipped)} failures={len(self.failures)} "
                f"manifest_failures={len(self.manifest_failures)} incomplete={self.incomplete}")


def ingest(ledger, allowlist, seed_urls: dict[str, list[str]], store: DocumentStore, *,
           fetcher: Fetcher, today: str | None = None, kind: str = DEFAULT_KIND,
           budget: int = MAX_DOCUMENTS_PER_RUN, manifest: Path | str | None = None) -> IngestResult:
    """Ingest seed URLs for HELD tickers only, returning an `IngestResult`.

    `seed_urls` maps a ticker to operator-provided `https` URLs (D4: no discovery). Tickers not held
    (no position, zero, or closed) are ignored entirely — no fetch is attempted. The ledger is only
    read.

    When `manifest` is given, one append-only JSONL record is written per attempted URL. A manifest
    write failure is recorded in `result.manifest_failures` and never affects a stored document.
    """
    if budget < 0:
        raise ValueError("budget must be >= 0")
    retrieved_at = today or datetime.now(UTC).date().isoformat()
    result = IngestResult()
    held = held_tickers(ledger)

    for ticker in held:
        urls = seed_urls.get(ticker, [])
        if not urls:
            result.tickers_skipped.append(ticker)
            continue
        for url in urls:
            if len(result.stored) >= budget:
                result.incomplete = True
                return result
            _ingest_one(ticker, url, allowlist, store, fetcher, retrieved_at, kind, result,
                        manifest=manifest)
    return result


def _ingest_one(ticker: str, url: str, allowlist, store: DocumentStore, fetcher: Fetcher,
                retrieved_at: str, kind: str, result: IngestResult,
                manifest: Path | str | None = None) -> None:
    try:
        fetched: FetchedDocument = fetcher.fetch(url, ticker)
    except FetchError as e:
        outcome = "policy_refused" if isinstance(e, PolicyViolation) else "fetch_error"
        result.failures.append({"ticker": ticker, "url": url, "reason": f"{type(e).__name__}: {e}"})
        _write_manifest(manifest, url, None, ticker, outcome, retrieved_at, result)
        return
    result.requested_to_final[fetched.requested_url] = fetched.final_url

    try:
        text = sanitize(fetched.raw, content_type=fetched.content_type)
    except SanitizerError as e:
        result.failures.append({"ticker": ticker, "url": url, "reason": f"sanitize: {e}"})
        _write_manifest(manifest, url, fetched.final_url, ticker, "sanitize_error", retrieved_at, result)
        return

    doc = new_document(
        ticker=ticker, kind=kind, source_url=fetched.final_url,
        approved_domain=_host_of(fetched.final_url), retrieved_at=retrieved_at,
        raw=fetched.raw, text=text, sanitizer_version=SANITIZER_VERSION,
        title=_title_for(fetched.final_url), published_at=None,
    )
    existing = store.get(doc.doc_id)
    if existing is not None:
        if _same_content(existing, doc):
            result.duplicates.append(doc.doc_id)
            _write_manifest(manifest, url, fetched.final_url, ticker, "duplicate", retrieved_at, result)
        else:
            result.failures.append(
                {"ticker": ticker, "url": url,
                 "reason": f"doc_id {doc.doc_id} exists with different content (append-only)"})
            _write_manifest(manifest, url, fetched.final_url, ticker, "conflict", retrieved_at, result)
        return
    store.put(doc)
    result.stored.append(doc.doc_id)
    _write_manifest(manifest, url, fetched.final_url, ticker, "stored", retrieved_at, result)


def _write_manifest(manifest: Path | str | None, requested_url: str, final_url: str | None,
                    ticker: str, outcome: str, retrieved_at: str, result: IngestResult) -> None:
    """Append one JSONL record under an exclusive lock, with short-write and tail recovery.

    Never raises: any failure is recorded in `result.manifest_failures`. The whole append is
    serialized with `flock` so a concurrent writer cannot interleave a tail recovery with an append,
    and the file is opened `O_RDWR|O_APPEND` so the same lock can repair a partial tail.

    A short or interrupted write is NOT silently accepted: if the full line (or its terminating
    newline) is not written, the fd is truncated back to its pre-write offset and a failure is
    recorded — the manifest is left exactly as it was before the call. The same rollback runs if the
    write raises (e.g. ENOSPC) part-way through, so a write error cannot leave a partial record.
    """
    if manifest is None:
        return
    record = {
        "requested_url": requested_url,
        "final_url": final_url,
        "ticker": ticker,
        "outcome": outcome,
        "retrieved_at": retrieved_at,
    }
    line = (json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
    try:
        if fcntl is None:
            raise OSError(
                "manifest locking is unavailable on this platform (requires POSIX fcntl.flock)")
        path = Path(manifest)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_APPEND, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)          # serialize the whole append + recovery
            try:
                _repair_partial_tail(fd)            # under the same lock (no bytes written yet)
                offset = os.lseek(fd, 0, os.SEEK_END)   # post-repair size = rollback target
                try:
                    written = _os_write(fd, line)
                    if written != len(line):
                        raise OSError(f"short write: wrote {written} of {len(line)} bytes")
                except OSError:
                    os.ftruncate(fd, offset)        # roll back any partial bytes
                    raise
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
    except OSError as e:
        result.manifest_failures.append({"url": requested_url, "reason": f"{type(e).__name__}: {e}"})


def _repair_partial_tail(fd: int) -> None:
    """Truncate an incomplete final line (one without a trailing newline). Caller holds the lock.

    Scans BACKWARD from the end in fixed-size chunks and stops at the first newline, so memory use is
    bounded regardless of how large the manifest has grown (append-only, potentially unbounded).

    A block is read to COMPLETION before it is searched: POSIX does not guarantee a read fills the
    requested length, and searching a short buffer would skip the unread tail of that block and risk
    truncating to the wrong newline. If a read cannot fill the requested block (EOF reached early,
    e.g. the file was shortened concurrently), recovery FAILS with an OSError rather than truncating:
    we do not know where the last valid record ends, so we must not guess. The caller reports the
    failure and skips the append, leaving the file untouched.
    """
    end = os.lseek(fd, 0, os.SEEK_END)
    if end == 0:
        return
    block = 65536
    pos = end
    while pos > 0:
        start = max(0, pos - block)
        os.lseek(fd, start, os.SEEK_SET)
        data = _read_exactly(fd, pos - start)        # read the whole block (bounded memory)
        if data is None:
            raise OSError(
                "manifest recovery failed: could not read the full tail block (file changed?)")
        nl = data.rfind(b"\n")
        if nl != -1:
            # Blocks are scanned end-first, so the first block with any newline holds the LAST one.
            os.ftruncate(fd, start + nl + 1)
            return
        pos = start
    os.ftruncate(fd, 0)                             # no newline anywhere: the whole file is one partial line


def _read_exactly(fd: int, count: int) -> bytes | None:
    """Read exactly `count` bytes, looping over short reads. Returns None if the fd ends early."""
    chunks: list[bytes] = []
    remaining = count
    while remaining > 0:
        chunk = _os_read(fd, remaining)
        if not chunk:
            return None
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def read_manifest(manifest: Path | str) -> tuple[list[dict], list[str]]:
    """Read a manifest, returning `(records, problems)`.

    A truncated final line (no trailing newline) is reported as a problem and skipped, since it is
    an incomplete append rather than a durable record. Corrupt interior lines are REPORTED in
    `problems` rather than silently discarded — an append-only audit manifest must surface corruption,
    not hide it. A missing file yields `([], [])`.
    """
    path = Path(manifest)
    if not path.exists():
        return [], []
    raw = path.read_text(encoding="utf-8")
    lines = raw.split("\n")
    truncated_tail = bool(raw) and not raw.endswith("\n")
    if truncated_tail:
        lines = lines[:-1]                          # drop the incomplete final line
    records: list[dict] = []
    problems: list[str] = []
    for index, line in enumerate(lines):
        if not line.strip():
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            problems.append(f"corrupt record at line {index + 1}")
    if truncated_tail:
        problems.append(f"truncated final record at line {len(lines) + 1}")
    return records, problems


def _host_of(url: str) -> str:
    return urlsplit(url).hostname or ""


def _title_for(url: str) -> str:
    """A minimal, non-guessed title: the last path segment, or the host. Never invents content."""
    parts = urlsplit(url)
    segment = parts.path.rstrip("/").rsplit("/", 1)[-1]
    return segment or (parts.hostname or "")
