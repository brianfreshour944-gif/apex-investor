"""M1 tests: document store — dual-hash provenance, identity, dates, integrity, fail-closed."""
from __future__ import annotations

import json

import pytest

from apex_investor.docstore import (
    DEFAULT_DOCS_DIR,
    KNOWN_SANITIZER_VERSIONS,
    Document,
    DocumentIntegrityError,
    DocumentStore,
    _hash_text,
    is_valid_doc_id,
    make_doc_id,
    new_document,
)
from apex_investor.sanitize import SANITIZER_VERSION


def mkdoc(**over):
    """A valid document; override fields per test."""
    kw = dict(
        ticker="AAPL", kind="filing", source_url="https://www.sec.gov/Archives/aapl-8k.htm",
        approved_domain="sec.gov", retrieved_at="2026-11-07", raw=b"hello filing",
        text="hello filing", title="8-K", published_at="2026-11-06",
        sanitizer_version=SANITIZER_VERSION,
    )
    kw.update(over)
    return new_document(**kw)


@pytest.fixture()
def store(tmp_path):
    return DocumentStore(tmp_path / "docs")


# ---- basic store/retrieve -------------------------------------------------

def test_put_get_roundtrip(store):
    doc = mkdoc()
    doc_id = store.put(doc)
    assert doc_id == doc.doc_id
    got = store.get(doc_id)
    assert got == doc


def test_unknown_doc_id_returns_none(store):
    assert store.get("0" * 64) is None


def test_doc_id_is_stable_and_content_addressed():
    a = mkdoc()
    b = mkdoc()  # same source + same raw bytes
    assert a.doc_id == b.doc_id == make_doc_id(a.source_url, a.raw_hash)


def test_doc_id_changes_with_raw_bytes():
    a = mkdoc(raw=b"one")
    b = mkdoc(raw=b"two")
    assert a.doc_id != b.doc_id


# ---- dual-hash provenance -------------------------------------------------

def test_unchanged_content_has_equal_hashes():
    doc = mkdoc(raw=b"same", text="same")
    assert doc.raw_hash == doc.content_hash


def test_sanitized_text_yields_distinct_hashes():
    # M1b will do this; M1 must already record both hashes independently.
    doc = mkdoc(raw=b"<script>x</script>clean", text="clean")
    assert doc.raw_hash != doc.content_hash
    assert doc.content_hash == _hash_text("clean")


def test_content_hash_must_match_text():
    with pytest.raises(ValueError, match="content_hash does not match text"):
        Document(
            doc_id="a" * 64, ticker="X", kind="filing", source_url="u", approved_domain="d",
            retrieved_at="2026-11-07", published_at=None, raw_hash="a" * 64,
            content_hash="b" * 64, sanitizer_version=SANITIZER_VERSION, title="t", text="real",
        )


# ---- dates: publication vs retrieval --------------------------------------

def test_publication_and_retrieval_dates_are_distinct(store):
    doc = mkdoc(retrieved_at="2026-11-07", published_at="2015-01-02")
    store.put(doc)
    got = store.get(doc.doc_id)
    assert got.retrieved_at == "2026-11-07"
    assert got.published_at == "2015-01-02"


def test_unknown_publication_date_is_none_not_backfilled(store):
    doc = mkdoc(published_at=None)
    store.put(doc)
    got = store.get(doc.doc_id)
    assert got.published_at is None
    assert got.retrieved_at == "2026-11-07"  # retrieval never substitutes for publication


# ---- validation / fail-closed on construction -----------------------------

def test_bad_kind_rejected():
    with pytest.raises(ValueError, match="kind must be one of"):
        mkdoc(kind="tweet")


@pytest.mark.parametrize("field", ["retrieved_at", "published_at"])
def test_bad_date_rejected(field):
    with pytest.raises(ValueError, match="ISO date"):
        mkdoc(**{field: "11/07/2026"})


def test_empty_required_fields_rejected():
    for field in ("ticker", "source_url", "approved_domain"):
        with pytest.raises(ValueError):
            mkdoc(**{field: ""})


def test_non_bytes_raw_rejected():
    with pytest.raises(ValueError, match="raw must be bytes"):
        mkdoc(raw="not-bytes")


# ---- append-only semantics -------------------------------------------------

def test_put_is_idempotent_for_identical_content(store):
    doc = mkdoc()
    assert store.put(doc) == doc.doc_id
    assert store.put(doc) == doc.doc_id  # no error, no duplicate
    assert store.list_doc_ids() == [doc.doc_id]


def test_put_refuses_different_content_under_same_id(store):
    doc = mkdoc()
    store.put(doc)
    # Same doc_id (same source+raw) but a different downstream text -> must refuse, not overwrite.
    tampered = mkdoc(text="different text")
    assert tampered.doc_id == doc.doc_id
    with pytest.raises(FileExistsError, match="append-only"):
        store.put(tampered)


# ---- integrity on read -----------------------------------------------------

def test_corrupt_json_raises_integrity_error(store):
    doc = mkdoc()
    store.put(doc)
    (store.root / f"{doc.doc_id}.json").write_text("{not valid json", encoding="utf-8")
    with pytest.raises(DocumentIntegrityError):
        store.get(doc.doc_id)


def test_tampered_text_detected_on_read(store):
    doc = mkdoc(raw=b"clean", text="clean")
    store.put(doc)
    path = store.root / f"{doc.doc_id}.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["text"] = "clean but altered"  # content_hash no longer matches
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(DocumentIntegrityError):
        store.get(doc.doc_id)


def test_id_mismatch_detected_on_read(store):
    doc = mkdoc()
    store.put(doc)
    path = store.root / f"{doc.doc_id}.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["doc_id"] = "c" * 64  # file name and stored id disagree
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(DocumentIntegrityError):
        store.get(doc.doc_id)


# ---- verify() --------------------------------------------------------------

def test_verify_ok_for_valid_document(store):
    doc = mkdoc()
    store.put(doc)
    report = store.verify(doc.doc_id)
    assert report["ok"] is True
    assert report["raw_hash"] == doc.raw_hash
    assert report["content_hash"] == doc.content_hash


def test_verify_missing_document_is_not_ok(store):
    report = store.verify("0" * 64)
    assert report["ok"] is False
    assert "not found" in report["reasons"][0]


def test_verify_corrupt_document_is_not_ok(store):
    doc = mkdoc()
    store.put(doc)
    (store.root / f"{doc.doc_id}.json").write_text("garbage", encoding="utf-8")
    report = store.verify(doc.doc_id)
    assert report["ok"] is False
    assert report["reasons"]


# ---- misc ------------------------------------------------------------------

def test_list_doc_ids_sorted(store):
    ids = [store.put(mkdoc(source_url=f"https://www.sec.gov/{i}", raw=f"body-{i}".encode()))
           for i in range(3)]
    assert store.list_doc_ids() == sorted(ids)


def test_default_docs_dir_is_data_docs():
    assert str(DEFAULT_DOCS_DIR) == "data/docs"


# ---- full-length id + path-traversal safety --------------------------------

def test_doc_id_is_full_sha256_hex():
    doc = mkdoc()
    assert len(doc.doc_id) == 64
    assert is_valid_doc_id(doc.doc_id)


@pytest.mark.parametrize("bad", [
    "",                          # empty
    "abc",                       # too short
    "A" * 64,                    # uppercase hex
    "g" * 64,                    # non-hex
    "0" * 63,                    # one short
    "0" * 65,                    # one long
    "../" + "0" * 61,            # traversal
    "..%2f..%2fetc",             # encoded traversal
    "0" * 31 + "/etc/passwd",    # path separator
    "0" * 31 + "\\..\\x",        # backslash
    "0" * 30 + "  " + "0" * 32,  # embedded spaces
    "0" * 30 + "\x00" + "0" * 33,  # NUL byte
])
def test_invalid_doc_id_rejected_by_get_and_verify(store, bad):
    assert is_valid_doc_id(bad) is False
    with pytest.raises(ValueError, match="invalid doc_id"):
        store.get(bad)
    report = store.verify(bad)
    assert report["ok"] is False


def test_traversal_id_cannot_escape_store_root(store, tmp_path):
    # A file OUTSIDE the store root that a traversal id would target if paths were built blindly.
    outside = tmp_path / "secret.json"
    outside.write_text('{"doc_id": "pwned"}', encoding="utf-8")
    # Every traversal-shaped id is rejected before any path is built.
    for bad in ("../secret", "..", "../secret.json", "../../etc/passwd"):
        with pytest.raises(ValueError, match="invalid doc_id"):
            store.get(bad)
    # The outside file is untouched and never addressable through the store.
    assert outside.read_text(encoding="utf-8") == '{"doc_id": "pwned"}'


def test_document_construction_rejects_malformed_doc_id():
    with pytest.raises(ValueError, match="64 lowercase-hex"):
        Document(
            doc_id="../escape", ticker="X", kind="filing", source_url="u", approved_domain="d",
            retrieved_at="2026-11-07", published_at=None, raw_hash="a" * 64,
            content_hash=_hash_text("real"), sanitizer_version=SANITIZER_VERSION, title="t", text="real",
        )


def test_list_doc_ids_ignores_non_id_files(store):
    doc = mkdoc()
    store.put(doc)
    (store.root / "not-a-valid-id.json").write_text("{}", encoding="utf-8")
    assert store.list_doc_ids() == [doc.doc_id]


# ---- sanitizer-version provenance enforcement ------------------------------

def test_known_sanitizer_versions_includes_current():
    assert SANITIZER_VERSION in KNOWN_SANITIZER_VERSIONS


def test_new_document_requires_sanitizer_version_keyword():
    # No default: omitting it is a TypeError, so provenance cannot be silently absent.
    with pytest.raises(TypeError):
        new_document(
            ticker="AAPL", kind="filing", source_url="https://www.sec.gov/x",
            approved_domain="sec.gov", retrieved_at="2026-11-07",
            raw=b"hello filing", text="hello filing",
        )


@pytest.mark.parametrize("bad", ["none", "", "0", "2", "latest", None, 1])
def test_new_document_rejects_unrecognized_sanitizer_version(bad):
    with pytest.raises(ValueError, match="sanitizer_version"):
        new_document(
            ticker="AAPL", kind="filing", source_url="https://www.sec.gov/x",
            approved_domain="sec.gov", retrieved_at="2026-11-07",
            raw=b"hello filing", text="hello filing", sanitizer_version=bad,
        )


def test_document_construction_rejects_unrecognized_sanitizer_version():
    with pytest.raises(ValueError, match="sanitizer_version"):
        Document(
            doc_id="a" * 64, ticker="X", kind="filing", source_url="u", approved_domain="d",
            retrieved_at="2026-11-07", published_at=None, raw_hash="a" * 64,
            content_hash=_hash_text("real"), sanitizer_version="none", title="t", text="real",
        )


def test_valid_document_records_sanitizer_version():
    doc = mkdoc()
    assert doc.sanitizer_version == SANITIZER_VERSION
