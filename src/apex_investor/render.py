"""G2 renderer (version 2): verified Documents + an answer-free question -> deterministic JSON message.

Responsibility (narrow): turn an ordered list of ALREADY-VERIFIED `Document` records and one
per-scenario QUESTION into
  (a) the approved system message, obtained ONLY through the verified prompt loader, and
  (b) one deterministic JSON user message containing the question and the documents as DATA.
It does not fetch, sanitize, store, or interpret documents, and it does not call a model.

Boundaries:
  - Downstream of M1/M1b: it consumes `Document.text` (sanitized text) and never raw source bytes.
  - Upstream of the adapter: the adapter receives the rendered `system`/`user` strings.
  - It writes nothing and mutates no store.

The system prompt is NOT caller-supplied. It is loaded by `load_approved_prompt()` against the one
approved SHA-256, so a caller cannot designate an arbitrary prompt (or an arbitrary approval hash)
as approved.

Serialization contract (exact, v2):
  payload = {"question": <str>, "documents": [{"doc_id","source_url","ticker","published_at",
             "text"}, ...]} in the input document order.
  json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
  -> pure ASCII, stable key order, no incidental whitespace, byte-deterministic.

v2 change (operator-approved): the model-facing message carries a dedicated answer-free `question`
field and NO scenario `instruction` field. All evaluator metadata (`expectations`, `must_abstain`,
`measure_definition`, `evidence_description`, `reference_value`) is excluded from the payload.

Answer-free guard: the question is checked against the scenario's reference answer before it is
serialized. The guard rejects any literal or normalized form of the reference value (comma/sign/
whitespace/accounting-parentheses variants) and any embedded `evidence_description`, and FAILS CLOSED
if it cannot complete the check (e.g. an answerable scenario with no reference value). The guard reads
expectations ONLY to check the question; it never places them in the payload. The guard is a BACKSTOP:
the primary protection is that no expectations field is ever serialized.

Fail closed: a non-`Document` entry, a duplicate `doc_id`, an oversized document, a non-string
question, a question that fails the answer-free guard, or a serialization error raises `RenderError`;
nothing partial is returned.

Limitations (must not be overstated):
  - Structural containment only. Escaping guarantees the document text cannot escape its JSON string
    value when the message is parsed as JSON. It does NOT guarantee a model will treat that text as
    untrusted evidence rather than instructions. That is a model-behavior question for evaluation.
  - `ensure_ascii=True` yields deterministic ASCII; it is NOT a claim that a model interprets
    `\\uXXXX`-escaped text as effectively as readable Unicode.
  - Sanitization quality is inherited from `sanitize.py`; the renderer adds none.
  - The answer-free guard is lexical: it catches numeric/`evidence_description` leakage, not a
    paraphrased answer. It is a backstop, not a semantic guarantee.
  - The per-document size limit default is `DEFAULT_MAX_DOCUMENT_BYTES` (operator-retained).
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass

from .docstore import Document, DocumentIntegrityError, DocumentStore
from .prompt_loader import load_approved_prompt

RENDERER_VERSION = "2"

# Operator-retained per-document cap on the UTF-8 length of `Document.text`, not a total.
DEFAULT_MAX_DOCUMENT_BYTES = 2_000_000

# Canonical description of the user template (v2: question + documents). Its hash identifies the
# serialization contract and is recorded per render so a template change is detectable.
TEMPLATE_DESCRIPTOR = (
    '{"documents": [{"doc_id": <json:str>, "published_at": <json:str|null>, "source_url": '
    '<json:str>, "text": <json:str>, "ticker": <json:str>}], "question": <json:str>}'
)
TEMPLATE_SHA256 = hashlib.sha256(TEMPLATE_DESCRIPTOR.encode("utf-8")).hexdigest()


class RenderError(Exception):
    """Raised when a scenario cannot be rendered safely (fail closed)."""


@dataclass(frozen=True)
class RenderRecord:
    """Provenance for one render. Hashes are over exact bytes/strings; nothing is normalized."""

    renderer_version: str
    template_sha256: str
    system_sha256: str
    user_sha256: str
    document_ids: tuple[str, ...]
    scenario_id: str | None = None


@dataclass(frozen=True)
class RenderResult:
    """A rendered scenario. `system_bytes` are the exact approved prompt bytes, unmodified."""

    system_bytes: bytes
    user: str
    record: RenderRecord

    @property
    def system(self) -> str:
        """Adapter-facing system message. Decoding is lossless because the bytes are valid UTF-8."""
        return self.system_bytes.decode("utf-8")


def resolve_documents(store: DocumentStore, doc_ids: Iterable[str]) -> tuple[Document, ...]:
    """Load verified documents from the store, in the given order, failing closed.

    Uses the M1 store's own validating `get` (id validation + content-hash check). A missing or
    corrupt document raises `RenderError` rather than yielding a partial list.
    """
    out: list[Document] = []
    for doc_id in doc_ids:
        try:
            doc = store.get(doc_id)
        except DocumentIntegrityError as exc:
            raise RenderError(f"corrupt document {doc_id}: {exc}") from exc
        if doc is None:
            raise RenderError(f"document {doc_id} not found in store")
        out.append(doc)
    return tuple(out)


def _validate_documents(documents: tuple[Document, ...], max_document_bytes: int) -> None:
    if max_document_bytes is None or not isinstance(max_document_bytes, int) \
            or isinstance(max_document_bytes, bool) or max_document_bytes <= 0:
        raise RenderError("max_document_bytes must be a positive integer")
    seen: set[str] = set()
    for doc in documents:
        if not isinstance(doc, Document):
            raise RenderError(f"documents must contain Document instances, got {type(doc).__name__}")
        if not isinstance(doc.text, str):
            raise RenderError(f"document {doc.doc_id} text must be a string")
        if doc.doc_id in seen:
            raise RenderError(f"duplicate doc_id in documents: {doc.doc_id}")
        seen.add(doc.doc_id)
        size = len(doc.text.encode("utf-8"))
        if size > max_document_bytes:
            raise RenderError(
                f"document {doc.doc_id} is {size} bytes, exceeds limit {max_document_bytes} "
                "(refusing rather than truncating)")


def serialize_user(question: str, documents: tuple[Document, ...]) -> str:
    """Serialize the question and documents into the exact JSON user message (v2)."""
    if not isinstance(question, str):
        raise RenderError(f"question must be a string, got {type(question).__name__}")
    payload = {
        "question": question,
        "documents": [
            {"doc_id": d.doc_id, "source_url": d.source_url, "ticker": d.ticker,
             "published_at": d.published_at, "text": d.text}
            for d in documents
        ],
    }
    try:
        return json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError) as exc:  # pragma: no cover - defensive; inputs are validated
        raise RenderError(f"serialization failed: {exc}") from exc


def _reference_value_forms(value: object) -> frozenset[str]:
    """Every textual form of a reference answer that a question must not contain.

    Covers comma/sign/whitespace/accounting-parentheses variants. Raises `RenderError` (fail closed)
    if the value is not an integer-valued number, so the guard never silently passes.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RenderError(f"reference_value must be a number, got {type(value).__name__}")
    if isinstance(value, float):
        if not value.is_integer():
            raise RenderError(f"reference_value must be integer-valued, got {value!r}")
        value = int(value)
    n = abs(value)
    forms = {
        f"{n}", f"{n:,}",           # 281724 / 281,724
        f"-{n}", f"-{n:,}",         # -8162 / -8,162
        f"+{n}",                    # +281724
        f"({n})", f"({n:,})",       # (8162) / (8,162)
    }
    return frozenset(forms)


def assert_question_is_answer_free(question: str, expectations: object) -> None:
    """Backstop guard: refuse a question that leaks the scenario's answer. Fails closed.

    Reads `expectations` ONLY to check the question; it never returns or serializes any of it.
    Raises `RenderError` when the question contains a literal or normalized form of the reference
    value, or the scenario's `evidence_description` text, or when the guard cannot complete (missing
    expectations, missing required answerable-scenario reference data, or a malformed value).
    """
    if not isinstance(question, str):
        raise RenderError(f"question must be a string, got {type(question).__name__}")
    if not isinstance(expectations, dict):
        raise RenderError(
            "expectations must be a dict for the answer-free guard (fail closed: cannot complete)")

    must_abstain = bool(expectations.get("must_abstain", False))
    reference_value = expectations.get("reference_value")

    if not must_abstain:
        if reference_value is None:
            raise RenderError(
                "answerable scenario has no reference_value; cannot run the answer-free guard "
                "(fail closed)")
        forms = _reference_value_forms(reference_value)
        collapsed = "".join(question.split())  # catch whitespace-separated digits, e.g. "281 724"
        for form in forms:
            if form in question or form in collapsed:
                raise RenderError("question leaks the reference value; refusing (fail closed)")

    evidence_description = expectations.get("evidence_description")
    if isinstance(evidence_description, str) and evidence_description:
        if evidence_description in question:
            raise RenderError(
                "question contains the scenario evidence_description; refusing (fail closed)")


def render_scenario(question: str, documents: Iterable[Document], *,
                    scenario_id: str | None = None,
                    expectations: dict | None = None,
                    max_document_bytes: int = DEFAULT_MAX_DOCUMENT_BYTES) -> RenderResult:
    """Render one scenario: approved system prompt + deterministic JSON user message (v2).

    The system prompt is obtained through the verified loader (never supplied by the caller).
    `expectations` is used ONLY by the answer-free guard and is never serialized.
    Raises `RenderError` on any fail-closed condition, or `PromptIntegrityError` if the approved
    prompt artifact does not verify.
    """
    if scenario_id is not None and not isinstance(scenario_id, str):
        raise RenderError("scenario_id must be a string or None")
    assert_question_is_answer_free(question, expectations)
    system_bytes = load_approved_prompt()  # verified against the one approved hash
    docs = tuple(documents)
    _validate_documents(docs, max_document_bytes)
    user = serialize_user(question, docs)
    record = RenderRecord(
        renderer_version=RENDERER_VERSION,
        template_sha256=TEMPLATE_SHA256,
        system_sha256=hashlib.sha256(system_bytes).hexdigest(),
        user_sha256=hashlib.sha256(user.encode("utf-8")).hexdigest(),
        document_ids=tuple(d.doc_id for d in docs),
        scenario_id=scenario_id,
    )
    return RenderResult(system_bytes=system_bytes, user=user, record=record)
