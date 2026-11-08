"""Document store: content-addressed, append-only, dual-hash provenance (Phase 2 / M1).

Scope (M1): STORAGE AND PROVENANCE ONLY. This module stores document bytes and text with
verifiable identity. It does NOT fetch (no network), does NOT sanitize (the sanitizer is M1b),
does NOT interpret, and never touches the portfolio or ledger.

Design rules:
  - Documents are DATA. The store holds bytes/text and metadata; it never executes or follows
    anything inside a document.
  - DUAL IDENTITY: `raw_hash` hashes the bytes as retrieved (the original document's identity);
    `content_hash` hashes the `text` that would be supplied downstream. When nothing has altered
    the bytes the two are equal; when they differ, both are kept so the original and the
    downstream-facing text are each independently identifiable. Raw bytes are hashed and
    discarded — only the sanitized/downstream `text` is persisted, so active content is never
    stored.
  - DATES ARE DISTINCT: `published_at` is the document's OWN date (may be unknown -> None) and is
    never conflated with `retrieved_at` (when we fetched it).
  - APPEND-ONLY: a `doc_id` is immutable. Re-putting identical content is idempotent; putting
    DIFFERENT content under an existing id is refused (never silently overwrite).
  - FAIL CLOSED: a malformed document is rejected on construction; a corrupt or tampered stored
    file raises on read; an unknown `doc_id` returns None (no phantom document). A `doc_id` is
    validated (exactly 64 lowercase-hex chars) BEFORE it is used to build a filesystem path, so a
    path separator, `..`, or any other traversal string can never escape the store root.

KNOWN LIMITATION — raw bytes are NOT retained (accepted for M1):
  `raw_hash` records the identity of the bytes as retrieved, but those bytes are discarded after
  hashing. Consequently the store CANNOT later re-verify that the original bytes still hash to
  `raw_hash`, and CANNOT re-run a different sanitizer against the original document. Re-fetching
  the source may not reproduce the exact original bytes (the page may have changed). Retaining
  raw bytes is a future, explicit decision (extra disk; stores active content).
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass
from datetime import date as _date
from pathlib import Path

from .sanitize import SANITIZER_VERSION

DEFAULT_DOCS_DIR = Path("data/docs")

# Allowed document kinds (Phase 2 §3). Kept deliberately small.
KINDS = frozenset({"filing", "earnings", "transcript", "other"})

# Sanitizer versions this store recognizes. Every document MUST carry one of these — "none",
# empty, and unknown values are rejected, so sanitizer provenance can never be silently absent.
# Bumping SANITIZER_VERSION is therefore a DELIBERATE act: a new version is only accepted once it
# is added here (and, if previously-stored documents must stay readable, the old version is kept
# in the set rather than dropped).
KNOWN_SANITIZER_VERSIONS = frozenset({SANITIZER_VERSION})

_HEX_LEN = 64  # sha256 hex digest length; the full digest is used as the document id
_DOC_ID_LEN = _HEX_LEN  # no truncation: a doc_id is the full sha256 hex digest


class DocumentStoreError(Exception):
    """Base class for document-store failures."""


class DocumentIntegrityError(DocumentStoreError):
    """A stored document is corrupt, tampered with, or unreadable.

    Raised on READ, so a damaged store file is never silently treated as absent or valid.
    """


def _hash_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _hash_text(t: str) -> str:
    return hashlib.sha256(t.encode("utf-8")).hexdigest()


def _valid_iso_date(s: object) -> bool:
    if not isinstance(s, str):
        return False
    try:
        _date.fromisoformat(s)
    except ValueError:
        return False
    return True


def _is_hex(s: object, n: int = _HEX_LEN) -> bool:
    return isinstance(s, str) and len(s) == n and all(c in "0123456789abcdef" for c in s)


def is_valid_doc_id(doc_id: object) -> bool:
    """True only for a full 64-char lowercase-hex sha256 digest.

    Deliberately strict: the id is used to build a filesystem path, so anything that is not a
    bare hex digest — path separators, `..`, spaces, uppercase, wrong length — is rejected.
    """
    return _is_hex(doc_id, _HEX_LEN)


def _require_valid_doc_id(doc_id: object) -> str:
    if not is_valid_doc_id(doc_id):
        raise ValueError(
            f"invalid doc_id {doc_id!r}: must be exactly {_HEX_LEN} lowercase-hex characters")
    return doc_id  # type: ignore[return-value]


def make_doc_id(source_url: str, raw_hash: str) -> str:
    """A stable, content-addressed id: same source + same raw bytes -> same id.

    Derived from the source URL and the RAW hash, so a document's identity is reproducible and
    independent of any later sanitization. The FULL sha256 hex digest is used (no truncation).
    """
    digest = hashlib.sha256(f"{source_url}\n{raw_hash}".encode()).hexdigest()
    return digest[:_DOC_ID_LEN]


@dataclass(frozen=True)
class Document:
    """An immutable stored document with dual-hash provenance."""

    doc_id: str
    ticker: str
    kind: str                 # one of KINDS
    source_url: str
    approved_domain: str
    retrieved_at: str         # ISO date we fetched it
    published_at: str | None  # the document's own ISO date, or None if unknown
    raw_hash: str             # sha256 of the bytes as retrieved
    content_hash: str         # sha256 of `text`
    sanitizer_version: str
    title: str
    text: str                 # the downstream-facing (sanitized) text

    def __post_init__(self) -> None:
        if not is_valid_doc_id(self.doc_id):
            raise ValueError(
                f"doc_id must be exactly {_HEX_LEN} lowercase-hex characters, got {self.doc_id!r}")
        if not isinstance(self.ticker, str) or not self.ticker:
            raise ValueError("ticker must be a non-empty string")
        if self.kind not in KINDS:
            raise ValueError(f"kind must be one of {sorted(KINDS)}, got {self.kind!r}")
        if not isinstance(self.source_url, str) or not self.source_url:
            raise ValueError("source_url must be a non-empty string")
        if not isinstance(self.approved_domain, str) or not self.approved_domain:
            raise ValueError("approved_domain must be a non-empty string")
        if not _valid_iso_date(self.retrieved_at):
            raise ValueError(f"retrieved_at must be an ISO date, got {self.retrieved_at!r}")
        if self.published_at is not None and not _valid_iso_date(self.published_at):
            raise ValueError(f"published_at must be an ISO date or None, got {self.published_at!r}")
        if not _is_hex(self.raw_hash):
            raise ValueError("raw_hash must be a 64-char sha256 hex digest")
        if not _is_hex(self.content_hash):
            raise ValueError("content_hash must be a 64-char sha256 hex digest")
        if self.sanitizer_version not in KNOWN_SANITIZER_VERSIONS:
            raise ValueError(
                f"sanitizer_version must be one of {sorted(KNOWN_SANITIZER_VERSIONS)}, "
                f"got {self.sanitizer_version!r} (missing, 'none', and unknown versions are "
                f"rejected so sanitizer provenance is always recorded)")
        if not isinstance(self.title, str):
            raise ValueError("title must be a string")
        if not isinstance(self.text, str):
            raise ValueError("text must be a string")
        if self.content_hash != _hash_text(self.text):
            raise ValueError("content_hash does not match text")


def new_document(*, ticker: str, kind: str, source_url: str, approved_domain: str,
                 retrieved_at: str, raw: bytes, text: str, sanitizer_version: str,
                 title: str = "", published_at: str | None = None) -> Document:
    """Build a Document, computing both hashes and the doc_id from the inputs.

    `raw` is the bytes as retrieved; `text` is the downstream-facing (sanitized) text.
    `sanitizer_version` is REQUIRED and must be a recognized version (see
    KNOWN_SANITIZER_VERSIONS): there is no default, so a caller cannot create a document without
    recording which sanitizer produced its text. Missing, "none", or unknown values are rejected.
    """
    if not isinstance(raw, (bytes, bytearray)):
        raise ValueError("raw must be bytes")
    raw_hash = _hash_bytes(bytes(raw))
    content_hash = _hash_text(text)
    doc_id = make_doc_id(source_url, raw_hash)
    return Document(
        doc_id=doc_id, ticker=ticker, kind=kind, source_url=source_url,
        approved_domain=approved_domain, retrieved_at=retrieved_at,
        published_at=published_at, raw_hash=raw_hash, content_hash=content_hash,
        sanitizer_version=sanitizer_version, title=title, text=text,
    )


def _atomic_write_text(path: Path, content: str) -> None:
    """Write `content` to `path` atomically (temp file + os.replace), never a partial file."""
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)


class DocumentStore:
    """Filesystem-backed, append-only store of Documents under `data/docs/` (git-ignored)."""

    def __init__(self, root: Path | str = DEFAULT_DOCS_DIR) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, doc_id: str) -> Path:
        """Build the store path for a VALIDATED doc_id (callers validate first)."""
        return self.root / f"{doc_id}.json"

    def put(self, doc: Document) -> str:
        """Store `doc`; return its doc_id. Idempotent for identical content.

        Raises FileExistsError if a DIFFERENT document already exists under the same doc_id
        (append-only: never silently overwrite history).
        """
        if not isinstance(doc, Document):
            raise ValueError("doc must be a Document")
        _require_valid_doc_id(doc.doc_id)
        path = self._path(doc.doc_id)
        if path.exists():
            existing = self.get(doc.doc_id)
            if existing == doc:
                return doc.doc_id
            raise FileExistsError(
                f"doc_id {doc.doc_id} already exists with different content; store is append-only")
        _atomic_write_text(path, json.dumps(asdict(doc), ensure_ascii=False, sort_keys=True))
        return doc.doc_id

    def get(self, doc_id: str) -> Document | None:
        """Return the stored Document, or None if unknown. Raise if the stored file is corrupt.

        The id is validated BEFORE a path is built, so a malformed id (path separators, `..`,
        non-hex, wrong length) is rejected and can never address a file outside the store root.
        """
        _require_valid_doc_id(doc_id)
        path = self._path(doc_id)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise DocumentIntegrityError(f"unreadable document {doc_id}: {exc}") from exc
        if not isinstance(data, dict):
            raise DocumentIntegrityError(f"document {doc_id} is not a JSON object")
        try:
            doc = Document(**data)
        except (TypeError, ValueError) as exc:
            raise DocumentIntegrityError(f"invalid document {doc_id}: {exc}") from exc
        if doc.doc_id != doc_id:
            raise DocumentIntegrityError(
                f"document id mismatch: file {doc_id} holds doc_id {doc.doc_id}")
        return doc

    def verify(self, doc_id: str) -> dict:
        """Integrity report for one document. `ok` is True only if it is present and valid."""
        if not is_valid_doc_id(doc_id):
            return {"ok": False, "doc_id": doc_id,
                    "reasons": [f"invalid doc_id {doc_id!r} (must be {_HEX_LEN} lowercase-hex)"]}
        try:
            doc = self.get(doc_id)
        except DocumentIntegrityError as exc:
            return {"ok": False, "doc_id": doc_id, "reasons": [str(exc)]}
        if doc is None:
            return {"ok": False, "doc_id": doc_id, "reasons": ["document not found"]}
        reasons: list[str] = []
        if doc.content_hash != _hash_text(doc.text):
            reasons.append("content_hash does not match stored text")
        return {"ok": not reasons, "doc_id": doc_id, "reasons": reasons,
                "raw_hash": doc.raw_hash, "content_hash": doc.content_hash}

    def list_doc_ids(self) -> list[str]:
        """All stored doc_ids, sorted, for deterministic iteration (valid ids only)."""
        return sorted(p.stem for p in self.root.glob("*.json") if is_valid_doc_id(p.stem))
